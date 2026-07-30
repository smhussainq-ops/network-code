"""Build the Windows Local Connector pilot package.

The archive never contains enrollment tokens or device credentials.  It can use
the bundled source on a pilot machine, or build a self-contained Windows binary
on Windows for the clean-machine certification gate.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from textwrap import dedent
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from netcode.shell_desktop import build_desktop_shell_profile


PACKAGE_VERSION = "0.3.5-community-preview"


def _rez_runtime_files() -> dict[str, bytes]:
    """Return the device-driver-only Rez runtime for the Local Connector."""
    try:
        from netcode.adapters.rez import RezAdapterBridge

        root = RezAdapterBridge().root
    except Exception:
        return {}
    required = [
        root / "device_state_model.py",
        root / "utils" / "__init__.py",
        root / "utils" / "policy_matcher.py",
    ]
    driver_files = sorted((root / "drivers").glob("*.py")) if (root / "drivers").is_dir() else []
    if not driver_files or any(not path.is_file() for path in required):
        return {}
    files: dict[str, bytes] = {}
    for path in [*driver_files, *required]:
        relative = path.relative_to(root)
        files[f"rez-runtime/{relative.as_posix()}"] = path.read_bytes()
    return files


def _runner_source_files() -> dict[str, bytes]:
    """Bundle the exact Local Connector source so pilots do not depend on PyPI."""
    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parent
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    files = {"runner-source/pyproject.toml": pyproject.read_bytes()}
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(project_root)
        files[f"runner-source/{relative.as_posix()}"] = path.read_bytes()
    template_dir = project_root / "templates"
    for path in sorted(template_dir.rglob("*.j2")):
        relative = path.relative_to(project_root)
        files[f"runner-source/{relative.as_posix()}"] = path.read_bytes()
    return files


def _preflight_ps1(control_plane_url: str) -> str:
    return dedent(
        fr"""
        param(
          [string]$ControlPlaneUrl = "{control_plane_url}",
          [switch]$AllowInsecureHttpForLab
        )

        $ErrorActionPreference = "Stop"
        $Failures = @()
        if (-not [Environment]::Is64BitOperatingSystem) {{ $Failures += "64-bit Windows is required." }}
        try {{ $Uri = [Uri]$ControlPlaneUrl }} catch {{ $Failures += "ControlPlaneUrl is invalid." }}
        if ($Uri -and $Uri.Scheme -ne "https" -and -not $AllowInsecureHttpForLab) {{
          $Failures += "HTTPS is required. Use -AllowInsecureHttpForLab only for a private GNS3 pilot."
        }}
        $BundledExe = Join-Path $PSScriptRoot "bin\RezonanceLocalConnector\RezonanceLocalConnector.exe"
        $HasPython = [bool](Get-Command py -ErrorAction SilentlyContinue)
        if (-not (Test-Path $BundledExe) -and -not $HasPython) {{
          $Failures += "This pilot archive needs Python 3.10+ or a Windows-built connector binary."
        }}
        if ($Failures.Count -gt 0) {{
          $Failures | ForEach-Object {{ Write-Error $_ }}
          exit 1
        }}
        Write-Host "Preflight passed: Windows $([Environment]::OSVersion.Version), control plane $ControlPlaneUrl"
        """
    ).strip() + "\n"


def _install_runner_ps1(control_plane_url: str) -> str:
    return dedent(
        fr"""
        param(
          [string]$JoinToken = "",
          [string]$RunnerName = $env:COMPUTERNAME,
          [string]$ControlPlaneUrl = "{control_plane_url}",
          [string]$PackageSpec = "",
          [string]$ProxyUrl = "",
          [string]$CaBundle = "",
          [string]$OperatorAccount = "",
          [switch]$AllowInsecureHttpForLab,
          [switch]$RegisterStartupTask,
          [switch]$StartNow,
          [switch]$NoOpenControl
        )

        $ErrorActionPreference = "Stop"
        $Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
          throw "Run install-runner.ps1 from PowerShell opened as Administrator."
        }}
        $ElevatedIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $OperatorUserSid = $null
        if ($OperatorAccount) {{
          try {{
            $OperatorUserSid = (New-Object Security.Principal.NTAccount($OperatorAccount)).Translate(
              [Security.Principal.SecurityIdentifier]
            )
          }} catch {{
            throw "OperatorAccount '$OperatorAccount' could not be resolved to a Windows SID."
          }}
        }} else {{
          $InstallerSessionId = (Get-Process -Id $PID).SessionId
          $Explorer = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue |
            Where-Object {{ $_.SessionId -eq $InstallerSessionId }} |
            Select-Object -First 1
          if ($Explorer) {{
            try {{
              $Owner = Invoke-CimMethod -InputObject $Explorer -MethodName GetOwner -ErrorAction Stop
              if ($Owner.User) {{
                $ResolvedAccount = if ($Owner.Domain) {{ "$($Owner.Domain)\$($Owner.User)" }} else {{ $Owner.User }}
                $OperatorUserSid = (New-Object Security.Principal.NTAccount($ResolvedAccount)).Translate(
                  [Security.Principal.SecurityIdentifier]
                )
              }}
            }} catch {{}}
          }}
          if (-not $OperatorUserSid) {{
            $OperatorUserSid = $ElevatedIdentity.User
          }}
        }}
        if (-not $OperatorUserSid -or $OperatorUserSid.Value -eq "S-1-5-18") {{
          throw "Unable to resolve the signed-in Windows operator. Pass -OperatorAccount 'DOMAIN\User'."
        }}
        & (Join-Path $PSScriptRoot "preflight.ps1") -ControlPlaneUrl $ControlPlaneUrl -AllowInsecureHttpForLab:$AllowInsecureHttpForLab

        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $DataRoot = Join-Path $Root "data"
        $ScriptsRoot = Join-Path $Root "scripts"
        $Venv = Join-Path $Root ".venv"
        $Python = Join-Path $Venv "Scripts\python.exe"
        $RezSource = Join-Path $PSScriptRoot "rez-runtime"
        $RezRoot = Join-Path $Root "rez-runtime"
        $BundledSource = Join-Path $PSScriptRoot "runner-source"
        $BundledExeRoot = Join-Path $PSScriptRoot "bin\RezonanceLocalConnector"
        $InstalledExeRoot = Join-Path $Root "bin\RezonanceLocalConnector"
        New-Item -ItemType Directory -Force -Path $Root,$DataRoot,$ScriptsRoot | Out-Null

        $TaskName = "RezonanceLocalConnector"
        $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($ExistingTask) {{
          Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
          $TaskStopDeadline = (Get-Date).AddSeconds(30)
          do {{
            Start-Sleep -Milliseconds 250
            $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
          }} while ($TaskState -eq "Running" -and (Get-Date) -lt $TaskStopDeadline)
          if ($TaskState -eq "Running") {{
            throw "Unable to stop the Local Connector startup task."
          }}
        }}

        # Task Scheduler shutdown is asynchronous. Re-enumerate both the
        # supervisor and connector until late child processes have drained.
        $ConnectorStopDeadline = (Get-Date).AddSeconds(30)
        do {{
          $ConnectorProcesses = @(Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue)
          $SupervisorIds = @()
          foreach ($ConnectorProcess in $ConnectorProcesses) {{
            try {{
              if ($ConnectorProcess.SessionId -eq 0) {{
                $CimProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($ConnectorProcess.Id)" -ErrorAction SilentlyContinue
                $ParentProcess = if ($CimProcess) {{ Get-Process -Id $CimProcess.ParentProcessId -ErrorAction SilentlyContinue }} else {{ $null }}
                if ($ParentProcess -and $ParentProcess.SessionId -eq 0 -and $ParentProcess.ProcessName -in @("powershell", "pwsh")) {{
                  $SupervisorIds += $ParentProcess.Id
                }}
              }}
            }} catch {{}}
          }}
          $SupervisorIds | Select-Object -Unique | ForEach-Object {{
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
          }}
          $ConnectorProcesses | ForEach-Object {{
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
          }}
          Start-Sleep -Milliseconds 250
        }} while (
          (Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue) -and
          (Get-Date) -lt $ConnectorStopDeadline
        )
        if (Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue) {{
          throw "Unable to stop the installed Local Connector process."
        }}

        foreach ($Script in @("start-runner.ps1", "open-connector.ps1", "diagnose-runner.ps1", "repair-connector.ps1", "uninstall-runner.ps1")) {{
          Copy-Item -Force (Join-Path $PSScriptRoot $Script) (Join-Path $ScriptsRoot $Script)
        }}
        if (-not (Test-Path (Join-Path $RezSource "drivers\collector.py"))) {{
          throw "The Rez multi-vendor adapter bundle is missing. Download a fresh package."
        }}
        New-Item -ItemType Directory -Force -Path $RezRoot | Out-Null
        Copy-Item -Path (Join-Path $RezSource "*") -Destination $RezRoot -Recurse -Force

        $Executable = Join-Path $InstalledExeRoot "RezonanceLocalConnector.exe"
        if (Test-Path (Join-Path $BundledExeRoot "RezonanceLocalConnector.exe")) {{
          New-Item -ItemType Directory -Force -Path (Split-Path $InstalledExeRoot) | Out-Null
          Copy-Item -Path $BundledExeRoot -Destination (Split-Path $InstalledExeRoot) -Recurse -Force
        }} else {{
          if (-not (Test-Path $Python)) {{ py -3 -m venv $Venv }}
          & $Python -m pip install --disable-pip-version-check --upgrade pip
          if ($PackageSpec) {{
            & $Python -m pip install --upgrade $PackageSpec
          }} else {{
            if (-not (Test-Path (Join-Path $BundledSource "pyproject.toml"))) {{
              throw "The bundled Local Connector source is missing."
            }}
            & $Python -m pip install --upgrade $BundledSource
          }}
        }}

        if ($CaBundle -and -not (Test-Path $CaBundle)) {{ throw "CA bundle not found: $CaBundle" }}
        @{{
          control_plane_url = $ControlPlaneUrl
          proxy_url = $ProxyUrl
          ca_bundle = $CaBundle
          package_version = "{PACKAGE_VERSION}"
        }} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Root "connector-settings.json")

        $env:NETCODE_RUNNER_HOME = $DataRoot
        $env:NETCODE_REZ_ROOT = $RezRoot
        if ($ProxyUrl) {{ $env:HTTPS_PROXY = $ProxyUrl; $env:HTTP_PROXY = $ProxyUrl; $env:WSS_PROXY = $ProxyUrl }}
        if ($CaBundle) {{ $env:SSL_CERT_FILE = $CaBundle; $env:REQUESTS_CA_BUNDLE = $CaBundle }}
        $IdentityPath = Join-Path $DataRoot "identity.dpapi"
        $IdentityExists = Test-Path $IdentityPath
        if ($IdentityExists) {{
          Write-Host "Preserved existing protected connector identity."
        }} elseif ($JoinToken) {{
          if (Test-Path $Executable) {{
            & $Executable enroll --server $ControlPlaneUrl --join-token $JoinToken --name $RunnerName
          }} else {{
            & $Python -m netcode.runner_agent enroll --server $ControlPlaneUrl --join-token $JoinToken --name $RunnerName
          }}
          if ($LASTEXITCODE -ne 0) {{ throw "Connector enrollment failed." }}
        }} else {{
          Write-Host "Enrollment is required. The Local Connector window will request the one-time pairing code."
        }}
        $IdentityExists = Test-Path $IdentityPath

        # SYSTEM runs the startup task and DPAPI uses machine scope. The task's
        # executable, scripts, settings, and Rez runtime must never be writable
        # by the desktop operator. Only the protected data directory needs
        # operator write access for enrollment and discovery.
        $OperatorReadExecuteAcl = "*$($OperatorUserSid.Value):(OI)(CI)RX"
        $OperatorModifyAcl = "*$($OperatorUserSid.Value):(OI)(CI)M"
        & icacls.exe $Root /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" $OperatorReadExecuteAcl | Out-Null
        if ($LASTEXITCODE -ne 0) {{ throw "Unable to protect Local Connector runtime permissions." }}
        & icacls.exe $DataRoot /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" $OperatorModifyAcl | Out-Null
        if ($LASTEXITCODE -ne 0) {{ throw "Unable to protect Local Connector data permissions." }}

        $InstalledStart = Join-Path $ScriptsRoot "start-runner.ps1"
        if ($RegisterStartupTask) {{
          $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstalledStart`""
          $Trigger = New-ScheduledTaskTrigger -AtStartup
          $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
          $TaskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
          Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $TaskSettings -Description "Rezonance outbound-only Local Connector" -Force | Out-Null
          if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {{
            throw "Local Connector startup task registration failed."
          }}
          # The task runs as SYSTEM, but the desktop control app runs as the
          # installing operator. Grant that exact SID read/run rights on this
          # task only; task modification and deletion remain administrator-only.
          $TaskService = New-Object -ComObject "Schedule.Service"
          $TaskService.Connect()
          $TaskFolder = $TaskService.GetFolder("\")
          $RegisteredTask = $TaskFolder.GetTask($TaskName)
          $TaskSecurity = New-Object Security.AccessControl.RawSecurityDescriptor(
            $RegisteredTask.GetSecurityDescriptor(4)
          )
          $TaskUserSid = $OperatorUserSid
          $BuiltinUsersSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-545")
          $TaskReadExecuteMask = [int]0xA0000000
          $TaskStoredReadExecuteMask = [int]0x001200A9
          for ($AceIndex = $TaskSecurity.DiscretionaryAcl.Count - 1; $AceIndex -ge 0; $AceIndex--) {{
            $Ace = $TaskSecurity.DiscretionaryAcl[$AceIndex]
            if (
              $Ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
              $Ace.SecurityIdentifier -and
              (
                $Ace.SecurityIdentifier.Value -eq $BuiltinUsersSid.Value -or
                $Ace.SecurityIdentifier.Value -eq $TaskUserSid.Value
              )
            ) {{
              $TaskSecurity.DiscretionaryAcl.RemoveAce($AceIndex)
            }}
          }}
          $TaskAce = New-Object Security.AccessControl.CommonAce(
            [Security.AccessControl.AceFlags]::None,
            [Security.AccessControl.AceQualifier]::AccessAllowed,
            $TaskReadExecuteMask,
            $TaskUserSid,
            $false,
            $null
          )
          $TaskSecurity.DiscretionaryAcl.InsertAce($TaskSecurity.DiscretionaryAcl.Count, $TaskAce)
          $RegisteredTask.SetSecurityDescriptor(
            $TaskSecurity.GetSddlForm([Security.AccessControl.AccessControlSections]::Access),
            0x10
          )
          $VerifiedTask = $TaskFolder.GetTask($TaskName)
          $VerifiedSecurity = New-Object Security.AccessControl.RawSecurityDescriptor(
            $VerifiedTask.GetSecurityDescriptor(4)
          )
          $VerifiedTaskReadExecute = $false
          $VerifiedBroadUsersAce = $false
          foreach ($Ace in $VerifiedSecurity.DiscretionaryAcl) {{
            if (
              $Ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
              $Ace.SecurityIdentifier
            ) {{
              if (
                $Ace.SecurityIdentifier.Value -eq $TaskUserSid.Value -and
                $Ace.AccessMask -in @($TaskReadExecuteMask, $TaskStoredReadExecuteMask)
              ) {{
                $VerifiedTaskReadExecute = $true
              }}
              if ($Ace.SecurityIdentifier.Value -eq $BuiltinUsersSid.Value) {{
                $VerifiedBroadUsersAce = $true
              }}
            }}
          }}
          if (-not $VerifiedTaskReadExecute -or $VerifiedBroadUsersAce) {{
            throw "Local Connector startup-task permissions could not be verified."
          }}
          Write-Host "Registered startup task: $TaskName"
        }}
        if ($StartNow) {{
          if (-not $IdentityExists) {{
            Write-Host "Runner start deferred until enrollment is complete."
          }} else {{
            if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {{
              Start-ScheduledTask -TaskName $TaskName
            }} else {{
              Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$InstalledStart`"" -WindowStyle Hidden
            }}
            $StartDeadline = (Get-Date).AddSeconds(20)
            do {{
              Start-Sleep -Milliseconds 500
              $InstalledProcess = Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue |
                Where-Object {{ $_.Path -and [string]::Equals($_.Path, $Executable, [StringComparison]::OrdinalIgnoreCase) }} |
                Select-Object -First 1
            }} until ($InstalledProcess -or (Get-Date) -ge $StartDeadline)
            if (-not $InstalledProcess) {{ throw "Installed Local Connector did not remain running after startup." }}
            if ($RegisterStartupTask) {{
              $RunningTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
              if (-not $RunningTask -or $RunningTask.State -ne "Running") {{
                throw "Local Connector startup task is not supervising the running process."
              }}
            }}
          }}
        }}

        $ShortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Rezonance Local Connector.lnk"
        $Shell = New-Object -ComObject WScript.Shell
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = "powershell.exe"
        $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptsRoot 'open-connector.ps1')`""
        $Shortcut.WorkingDirectory = $Root
        if (Test-Path $Executable) {{ $Shortcut.IconLocation = "$Executable,0" }}
        $Shortcut.Save()
        $RepairShortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Rezonance Local Connector Repair.lnk"
        $RepairShortcut = $Shell.CreateShortcut($RepairShortcutPath)
        $RepairShortcut.TargetPath = "powershell.exe"
        $RepairShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptsRoot 'repair-connector.ps1')`""
        $RepairShortcut.WorkingDirectory = $Root
        if (Test-Path $Executable) {{ $RepairShortcut.IconLocation = "$Executable,0" }}
        $RepairShortcut.Save()

        Write-Host $(if ($IdentityExists -or $JoinToken) {{ "Rezonance Local Connector installed and enrolled." }} else {{ "Rezonance Local Connector installed. Complete enrollment in the control window." }})
        Write-Host "Next: discover local inventory in the Rezonance Local Connector window."
        if (-not $NoOpenControl) {{
          if ($ElevatedIdentity.User.Value -eq $OperatorUserSid.Value) {{
            & (Join-Path $ScriptsRoot "open-connector.ps1")
          }} else {{
            Write-Host "Open Rezonance Local Connector from the signed-in user's Start menu to complete pairing."
          }}
        }}
        """
    ).strip() + "\n"


def _start_runner_ps1() -> str:
    return dedent(
        r"""
        $ErrorActionPreference = "Stop"
        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $DataRoot = Join-Path $Root "data"
        $SettingsPath = Join-Path $Root "connector-settings.json"
        $Python = Join-Path $Root ".venv\Scripts\python.exe"
        $Executable = Join-Path $Root "bin\RezonanceLocalConnector\RezonanceLocalConnector.exe"
        $env:NETCODE_RUNNER_HOME = $DataRoot
        $env:NETCODE_REZ_ROOT = Join-Path $Root "rez-runtime"
        if (Test-Path $SettingsPath) {
          $Settings = Get-Content -Raw $SettingsPath | ConvertFrom-Json
          if ($Settings.proxy_url) { $env:HTTPS_PROXY = $Settings.proxy_url; $env:HTTP_PROXY = $Settings.proxy_url; $env:WSS_PROXY = $Settings.proxy_url }
          if ($Settings.ca_bundle) { $env:SSL_CERT_FILE = $Settings.ca_bundle; $env:REQUESTS_CA_BUNDLE = $Settings.ca_bundle }
        }
        $LogDir = Join-Path $Root "logs"
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        $LogStem = Join-Path $LogDir ("connector-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff"))
        $StdoutLog = $LogStem + ".out.log"
        $StderrLog = $LogStem + ".err.log"
        if (Test-Path $Executable) {
          $Runtime = $Executable
          $RuntimeArguments = @("run")
        } elseif (Test-Path $Python) {
          $Runtime = $Python
          $RuntimeArguments = @("-m", "netcode.runner_agent", "run")
        } else {
          throw "Local Connector runtime not found. Run install-runner.ps1 first."
        }
        $RestartAttempt = 0
        $RestartLimit = 3
        $RestartDelaySeconds = 60
        while ($true) {
          # Windows PowerShell can promote native stderr to ErrorRecord. The
          # connector logs transient network errors to stderr while continuing,
          # so do not let the wrapper's Stop policy terminate the child process.
          $PreviousErrorActionPreference = $ErrorActionPreference
          $ErrorActionPreference = "Continue"
          try {
            & $Runtime @RuntimeArguments 1>> $StdoutLog 2>> $StderrLog
            $ExitCode = $LASTEXITCODE
          } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
          }
          if ($RestartAttempt -ge $RestartLimit) {
            throw "Local Connector exited with code $ExitCode after $RestartLimit restart attempts. See $StderrLog."
          }
          $RestartAttempt += 1
          Add-Content -Encoding UTF8 -LiteralPath $StderrLog -Value "Local Connector exited with code $ExitCode; restart attempt $RestartAttempt of $RestartLimit in $RestartDelaySeconds seconds."
          Start-Sleep -Seconds $RestartDelaySeconds
        }
        """
    ).strip() + "\n"


def _open_connector_ps1() -> str:
    return dedent(
        r"""
        $ErrorActionPreference = "Stop"
        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $Python = Join-Path $Root ".venv\Scripts\python.exe"
        $Executable = Join-Path $Root "bin\RezonanceLocalConnector\RezonanceLocalConnector.exe"
        $env:NETCODE_RUNNER_HOME = Join-Path $Root "data"
        $env:NETCODE_REZ_ROOT = Join-Path $Root "rez-runtime"
        $SettingsPath = Join-Path $Root "connector-settings.json"
        if (Test-Path $SettingsPath) {
          $Settings = Get-Content -Raw $SettingsPath | ConvertFrom-Json
          if ($Settings.control_plane_url) { $env:NETCODE_CONTROL_PLANE_URL = $Settings.control_plane_url }
          if ($Settings.proxy_url) { $env:HTTPS_PROXY = $Settings.proxy_url; $env:HTTP_PROXY = $Settings.proxy_url; $env:WSS_PROXY = $Settings.proxy_url }
          if ($Settings.ca_bundle) { $env:SSL_CERT_FILE = $Settings.ca_bundle; $env:REQUESTS_CA_BUNDLE = $Settings.ca_bundle }
        }
        if (Test-Path $Executable) {
          Start-Process -FilePath $Executable -ArgumentList "control"
        } elseif (Test-Path $Python) {
          Start-Process -FilePath $Python -ArgumentList "-m", "netcode.runner_agent", "control" -WindowStyle Hidden
        } else {
          throw "Local Connector runtime not found. Run install-runner.ps1 first."
        }
        """
    ).strip() + "\n"


def _diagnose_runner_ps1() -> str:
    return dedent(
        r"""
        $ErrorActionPreference = "Continue"
        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $Python = Join-Path $Root ".venv\Scripts\python.exe"
        $Executable = Join-Path $Root "bin\RezonanceLocalConnector\RezonanceLocalConnector.exe"
        $env:NETCODE_RUNNER_HOME = Join-Path $Root "data"
        $env:NETCODE_REZ_ROOT = Join-Path $Root "rez-runtime"
        $Task = Get-ScheduledTask -TaskName "RezonanceLocalConnector" -ErrorAction SilentlyContinue
        if ($Task) { Write-Host "Startup task: $($Task.State)" } else { Write-Warning "Startup task is not registered." }
        if (Test-Path $Executable) {
          & $Executable doctor
        } elseif (Test-Path $Python) {
          & $Python -m netcode.runner_agent doctor
        } else {
          Write-Error "Local Connector runtime not found."
          exit 1
        }
        $DoctorExit = $LASTEXITCODE
        $LatestLog = Get-ChildItem (Join-Path $Root "logs\connector-*.log") -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($LatestLog) { Write-Host "Latest log: $($LatestLog.FullName)" }
        exit $DoctorExit
        """
    ).strip() + "\n"


def _repair_runner_ps1() -> str:
    return dedent(
        r"""
        param([string]$OperatorSid = "")
        $ErrorActionPreference = "Stop"
        $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
        if (-not $OperatorSid) {
          $OperatorSid = $Identity.User.Value
        }
        if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
          $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -OperatorSid `"$OperatorSid`""
          $Elevated = Start-Process powershell.exe -Verb RunAs -ArgumentList $Arguments -Wait -PassThru
          exit $Elevated.ExitCode
        }

        $ResolvedOperatorSid = New-Object Security.Principal.SecurityIdentifier($OperatorSid)
        if ($ResolvedOperatorSid.Value -eq "S-1-5-18") {
          throw "The Windows SYSTEM account cannot be the desktop operator."
        }
        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $DataRoot = Join-Path $Root "data"
        if (-not (Test-Path $Root) -or -not (Test-Path $DataRoot)) {
          throw "Rezonance Local Connector is not installed."
        }
        $OperatorReadExecuteAcl = "*$($ResolvedOperatorSid.Value):(OI)(CI)RX"
        $OperatorModifyAcl = "*$($ResolvedOperatorSid.Value):(OI)(CI)M"
        & icacls.exe $Root /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" $OperatorReadExecuteAcl | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Unable to repair Local Connector runtime permissions." }
        & icacls.exe $DataRoot /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" $OperatorModifyAcl | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Unable to repair Local Connector data permissions." }

        $TaskName = "RezonanceLocalConnector"
        $TaskService = New-Object -ComObject "Schedule.Service"
        $TaskService.Connect()
        $TaskFolder = $TaskService.GetFolder("\")
        try {
          $RegisteredTask = $TaskFolder.GetTask($TaskName)
        } catch {
          throw "The Local Connector startup task is not installed."
        }
        $TaskSecurity = New-Object Security.AccessControl.RawSecurityDescriptor(
          $RegisteredTask.GetSecurityDescriptor(4)
        )
        $BuiltinUsersSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-545")
        $TaskReadExecuteMask = [int]0xA0000000
        $TaskStoredReadExecuteMask = [int]0x001200A9
        for ($AceIndex = $TaskSecurity.DiscretionaryAcl.Count - 1; $AceIndex -ge 0; $AceIndex--) {
          $Ace = $TaskSecurity.DiscretionaryAcl[$AceIndex]
          if (
            $Ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            $Ace.SecurityIdentifier -and
            (
              $Ace.SecurityIdentifier.Value -eq $BuiltinUsersSid.Value -or
              $Ace.SecurityIdentifier.Value -eq $ResolvedOperatorSid.Value
            )
          ) {
            $TaskSecurity.DiscretionaryAcl.RemoveAce($AceIndex)
          }
        }
        $TaskAce = New-Object Security.AccessControl.CommonAce(
          [Security.AccessControl.AceFlags]::None,
          [Security.AccessControl.AceQualifier]::AccessAllowed,
          $TaskReadExecuteMask,
          $ResolvedOperatorSid,
          $false,
          $null
        )
        $TaskSecurity.DiscretionaryAcl.InsertAce($TaskSecurity.DiscretionaryAcl.Count, $TaskAce)
        $RegisteredTask.SetSecurityDescriptor(
          $TaskSecurity.GetSddlForm([Security.AccessControl.AccessControlSections]::Access),
          0x10
        )

        $VerifiedTask = $TaskFolder.GetTask($TaskName)
        $VerifiedSecurity = New-Object Security.AccessControl.RawSecurityDescriptor(
          $VerifiedTask.GetSecurityDescriptor(4)
        )
        $VerifiedOperatorAce = $false
        $VerifiedBroadUsersAce = $false
        foreach ($Ace in $VerifiedSecurity.DiscretionaryAcl) {
          if (
            $Ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            $Ace.SecurityIdentifier
          ) {
            if (
              $Ace.SecurityIdentifier.Value -eq $ResolvedOperatorSid.Value -and
              $Ace.AccessMask -in @($TaskReadExecuteMask, $TaskStoredReadExecuteMask)
            ) {
              $VerifiedOperatorAce = $true
            }
            if ($Ace.SecurityIdentifier.Value -eq $BuiltinUsersSid.Value) {
              $VerifiedBroadUsersAce = $true
            }
          }
        }
        if (-not $VerifiedOperatorAce -or $VerifiedBroadUsersAce) {
          throw "Local Connector permissions could not be verified after repair."
        }
        Write-Host "Rezonance Local Connector permissions repaired for $($ResolvedOperatorSid.Value)."
        """
    ).strip() + "\n"


def _uninstall_runner_ps1() -> str:
    return dedent(
        r"""
        param([switch]$PurgeLocalData)
        $ErrorActionPreference = "Stop"
        $Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
          throw "Run uninstall-runner.ps1 from PowerShell opened as Administrator."
        }
        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $TaskName = "RezonanceLocalConnector"
        $ShortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Rezonance Local Connector.lnk"
        $RepairShortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Rezonance Local Connector Repair.lnk"
        $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($ExistingTask) {
          Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
          $TaskStopDeadline = (Get-Date).AddSeconds(30)
          do {
            Start-Sleep -Milliseconds 250
            $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
          } while ($TaskState -eq "Running" -and (Get-Date) -lt $TaskStopDeadline)
          if ($TaskState -eq "Running") {
            throw "Unable to stop the Local Connector startup task."
          }
          Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }

        # Task Scheduler shutdown is asynchronous. Re-enumerate both the
        # supervisor and connector until late child processes have drained.
        $ConnectorStopDeadline = (Get-Date).AddSeconds(30)
        do {
          $ConnectorProcesses = @(Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue)
          $SupervisorIds = @()
          foreach ($ConnectorProcess in $ConnectorProcesses) {
            try {
              if ($ConnectorProcess.SessionId -eq 0) {
                $CimProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($ConnectorProcess.Id)" -ErrorAction SilentlyContinue
                $ParentProcess = if ($CimProcess) { Get-Process -Id $CimProcess.ParentProcessId -ErrorAction SilentlyContinue } else { $null }
                if ($ParentProcess -and $ParentProcess.SessionId -eq 0 -and $ParentProcess.ProcessName -in @("powershell", "pwsh")) {
                  $SupervisorIds += $ParentProcess.Id
                }
              }
            } catch {}
          }
          $SupervisorIds | Select-Object -Unique | ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
          }
          $ConnectorProcesses | ForEach-Object {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
          }
          Start-Sleep -Milliseconds 250
        } while (
          (Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue) -and
          (Get-Date) -lt $ConnectorStopDeadline
        )
        if (Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue) {
          throw "Unable to stop the installed Local Connector process."
        }
        if ($PurgeLocalData) {
          if (Test-Path $Root) { Remove-Item -Recurse -Force $Root -ErrorAction Stop }
          if (Test-Path $Root) { throw "Local Connector data purge did not complete." }
          Write-Host "Local Connector and all local identity, inventory, and logs were removed."
        } else {
          $RuntimeTargets = @()
          foreach ($Name in @(".venv", "bin", "rez-runtime", "scripts", "connector-settings.json")) {
            $Target = Join-Path $Root $Name
            $RuntimeTargets += $Target
            if (Test-Path $Target) { Remove-Item -Recurse -Force $Target -ErrorAction Stop }
          }
          $RemainingRuntime = @($RuntimeTargets | Where-Object { Test-Path $_ })
          if ($RemainingRuntime) { throw "Runtime removal did not complete: $($RemainingRuntime -join ', ')" }
          Write-Host "Runtime removed. Protected data and logs remain at $Root. Use -PurgeLocalData to remove them."
        }
        Remove-Item -Force $ShortcutPath -ErrorAction SilentlyContinue
        Remove-Item -Force $RepairShortcutPath -ErrorAction SilentlyContinue
        """
    ).strip() + "\n"


def _build_executable_ps1() -> str:
    return dedent(
        r"""
        param(
          [Parameter(Mandatory=$true)][string]$CommercialPython,
          [switch]$Clean
        )
        $ErrorActionPreference = "Stop"
        if (-not (Test-Path $CommercialPython)) { throw "Commercial Nuitka Python not found: $CommercialPython" }
        $NuitkaVersion = (& $CommercialPython -m nuitka --version 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0 -or $NuitkaVersion -notmatch "Commercial:") {
          throw "The selected Python environment does not contain Nuitka Commercial."
        }
        $BuildRoot = Join-Path $env:TEMP "rezonance-local-connector-build"
        if ($Clean) { Remove-Item -Recurse -Force $BuildRoot -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
        $Report = Join-Path $BuildRoot "nuitka-report.xml"
        & $CommercialPython -m pip install --disable-pip-version-check --upgrade (Join-Path $PSScriptRoot "runner-source")
        if ($LASTEXITCODE -ne 0) { throw "Local Connector source installation failed." }
        Push-Location $BuildRoot
        try {
          & $CommercialPython -m nuitka `
            --mode=standalone `
            --output-dir=$BuildRoot `
            --output-filename=RezonanceLocalConnector.exe `
            --jobs=2 `
            --lto=no `
            --enable-plugin=anti-bloat `
            --enable-plugin=tk-inter `
            --assume-yes-for-downloads `
            --windows-console-mode=hide `
            --include-package=netcode `
            --include-package=pydantic `
            --include-package=tenacity `
            --include-package=netmiko `
            --include-package=paramiko `
            --include-package=ntc_templates `
            --include-package=textfsm `
            --include-package=yaml `
            --include-package=websockets `
            --include-package-data=certifi `
            --include-package-data=ntc_templates `
            --include-package-data=tzdata `
            --include-data-dir="$(Join-Path $PSScriptRoot 'runner-source\templates')=templates" `
            --report=$Report `
            (Join-Path $PSScriptRoot "windows-entrypoint.py")
          if ($LASTEXITCODE -ne 0) { throw "Nuitka Commercial build failed." }
          $BuiltExe = Get-ChildItem -Path $BuildRoot -Recurse -Filter "RezonanceLocalConnector.exe" |
            Where-Object { $_.DirectoryName -like "*.dist" } | Select-Object -First 1
          if (-not $BuiltExe) { throw "Nuitka output executable was not found." }
          $Destination = Join-Path $PSScriptRoot "bin\RezonanceLocalConnector"
          Remove-Item -Recurse -Force $Destination -ErrorAction SilentlyContinue
          New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
          Copy-Item -Recurse -Force $BuiltExe.Directory.FullName $Destination
          $Exe = Join-Path $Destination "RezonanceLocalConnector.exe"
          $SmokeRoot = Join-Path $BuildRoot "doctor-home"
          $DoctorOut = Join-Path $BuildRoot "compiled-doctor.json"
          $DoctorErr = Join-Path $BuildRoot "compiled-doctor.err.txt"
          Remove-Item -Recurse -Force $SmokeRoot -ErrorAction SilentlyContinue
          Remove-Item -Force $DoctorOut,$DoctorErr -ErrorAction SilentlyContinue
          New-Item -ItemType Directory -Force -Path $SmokeRoot | Out-Null
          $PreviousRunnerHome = [Environment]::GetEnvironmentVariable("NETCODE_RUNNER_HOME", "Process")
          $PreviousRezRoot = [Environment]::GetEnvironmentVariable("NETCODE_REZ_ROOT", "Process")
          try {
            $env:NETCODE_RUNNER_HOME = $SmokeRoot
            $env:NETCODE_REZ_ROOT = Join-Path $PSScriptRoot "rez-runtime"
            $DoctorProcess = Start-Process -FilePath $Exe -ArgumentList @("doctor", "--timeout", "1") -RedirectStandardOutput $DoctorOut -RedirectStandardError $DoctorErr -PassThru -Wait
            $DoctorText = Get-Content -Raw $DoctorOut -ErrorAction SilentlyContinue
            if ([string]::IsNullOrWhiteSpace($DoctorText)) {
              $DoctorErrorText = Get-Content -Raw $DoctorErr -ErrorAction SilentlyContinue
              throw "Compiled doctor produced no JSON. stderr: $DoctorErrorText"
            }
            try { $Doctor = $DoctorText | ConvertFrom-Json } catch { throw "Compiled doctor returned invalid JSON: $($_.Exception.Message)" }
            $RezCheck = $Doctor.checks | Where-Object { $_.id -eq "rez_runtime" } | Select-Object -First 1
            if (-not $RezCheck -or $RezCheck.status -ne "pass") {
              $Detail = if ($RezCheck) { $RezCheck.message } else { "rez_runtime check is missing" }
              throw "Compiled Rez runtime smoke check failed: $Detail"
            }
            $TemplateCheck = $Doctor.checks | Where-Object { $_.id -eq "governed_templates" } | Select-Object -First 1
            if (-not $TemplateCheck -or $TemplateCheck.status -ne "pass") {
              $Detail = if ($TemplateCheck) { $TemplateCheck.message } else { "governed_templates check is missing" }
              throw "Compiled governed-template smoke check failed: $Detail"
            }
            if ($Doctor.security.credentials_returned -ne $false -or $Doctor.security.inbound_listener -ne $false) {
              throw "Compiled doctor violated the connector security contract."
            }
            Write-Host "Compiled Rez runtime and governed-template smoke checks passed."
          } finally {
            if ($null -eq $PreviousRunnerHome) { Remove-Item Env:\NETCODE_RUNNER_HOME -ErrorAction SilentlyContinue } else { $env:NETCODE_RUNNER_HOME = $PreviousRunnerHome }
            if ($null -eq $PreviousRezRoot) { Remove-Item Env:\NETCODE_REZ_ROOT -ErrorAction SilentlyContinue } else { $env:NETCODE_REZ_ROOT = $PreviousRezRoot }
          }
          $Hash = (Get-FileHash -Algorithm SHA256 $Exe).Hash.ToLowerInvariant()
          Set-Content -Encoding ASCII (Join-Path $PSScriptRoot "WINDOWS-EXE-SHA256.txt") "$Hash  RezonanceLocalConnector.exe"
          Write-Host "Built $Exe"
        } finally { Pop-Location }
        """
    ).strip() + "\n"


def _windows_entrypoint() -> str:
    return "from netcode.runner_agent import main\n\nraise SystemExit(main())\n"


def _readme(control_plane_url: str) -> str:
    return dedent(
        fr"""
        # Rezonance Local Connector for Windows

        This pilot package installs the same outbound-only connector used by
        Netcode Automation, Rez Diagnostics, Digital Twin discovery, and Shell.
        No LLM or MCP server runs on the Windows connector.

        ## Install

        Extract the ZIP, then open PowerShell as Administrator in the extracted
        folder:

        ```powershell
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
        .\install-runner.ps1 -RegisterStartupTask
        ```

        The installer opens the Local Connector window. Enter the one-time
        pairing code supplied by Rezonance. Confirm the exact organization,
        Community login, and connector name, then select **Connect this device**.
        The connector starts automatically after pairing. On **Discovery**, enter
        a bounded seed IP, range, or CIDR and local device credentials. Community
        discovery is limited to 25 devices; only successfully collected devices
        become protected inventory records.

        **Overview** always shows the server-verified organization, Community login email,
        connector name, and cloud address. If identity verification
        needs attention, select **Repair identity** and use the replacement
        pairing code issued for that connector. Repair preserves the protected
        local inventory and restarts only the connector process.

        Run `.\diagnose-runner.ps1` from an Administrator PowerShell window when
        support asks for the connector readiness report. If diagnostics report
        Windows access denied, select **Rezonance Local Connector Repair** from
        the Start menu. Repair preserves enrollment, inventory, and runtime state.

        ## Security model

        - Outbound HTTPS/WSS only in production; no inbound listener is opened.
        - Device access uses SSH/API from this connector to the local network.
        - Windows identity and inventory files use machine-scoped DPAPI.
        - Runtime access is restricted to SYSTEM, local administrators, and the
          exact signed-in operator; only that operator receives data-directory
          modify access.
        - The control plane receives public inventory facts and signed job results, never credentials.
        - Rez jobs are read-only. Netcode writes remain plan-, approval-, and verification-gated.
        - Proxy and custom enterprise CA paths can be supplied during install.

        Logs are under `C:\ProgramData\Rezonance\LocalConnector\logs`.
        """
    ).strip() + "\n"


def build_windows_runner_package(control_plane_url: str, *, runner_pool: str = "default") -> bytes:
    """Return a secret-free Windows Local Connector pilot ZIP."""
    profile = build_desktop_shell_profile(control_plane_url, runner_pool=runner_pool)
    files: dict[str, str | bytes] = {
        "README.md": _readme(control_plane_url),
        "preflight.ps1": _preflight_ps1(control_plane_url),
        "install-runner.ps1": _install_runner_ps1(control_plane_url),
        "start-runner.ps1": _start_runner_ps1(),
        "open-connector.ps1": _open_connector_ps1(),
        "diagnose-runner.ps1": _diagnose_runner_ps1(),
        "repair-connector.ps1": _repair_runner_ps1(),
        "uninstall-runner.ps1": _uninstall_runner_ps1(),
        "build-windows-executable.ps1": _build_executable_ps1(),
        "windows-entrypoint.py": _windows_entrypoint(),
        "netcode-shell-profile.json": json.dumps(profile, indent=2) + "\n",
        "package-info.json": json.dumps({
            "product": "Rezonance Local Connector",
            "version": PACKAGE_VERSION,
            "platform": "windows-x64",
            "runner_pool": runner_pool,
            "control_plane_url": control_plane_url.rstrip("/"),
            "contains_secrets": False,
        }, indent=2) + "\n",
    }
    files.update(_rez_runtime_files())
    files.update(_runner_source_files())
    checksums = []
    for name, content in sorted(files.items()):
        raw = content.encode("utf-8") if isinstance(content, str) else content
        checksums.append(f"{hashlib.sha256(raw).hexdigest()}  {name}")
    files["SHA256SUMS.txt"] = "\n".join(checksums) + "\n"

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def package_manifest(control_plane_url: str, *, runner_pool: str = "default") -> dict[str, Any]:
    rez_files = _rez_runtime_files()
    runner_files = _runner_source_files()
    return {
        "ok": True,
        "product": "Rezonance Local Connector",
        "version": PACKAGE_VERSION,
        "platform": "windows-x64",
        "artifact_kind": "pilot_zip",
        "control_plane_url": control_plane_url.rstrip("/"),
        "runner_pool": runner_pool,
        "network": "outbound_https_wss_only",
        "credentials": "windows_dpapi_machine_scope_and_restricted_acl",
        "startup": "system_scheduled_task",
        "python_required": True,
        "standalone_executable_build_script": True,
        "production_code_signing_complete": False,
        "files": [
            "README.md", "preflight.ps1", "install-runner.ps1", "start-runner.ps1",
            "open-connector.ps1", "diagnose-runner.ps1", "repair-connector.ps1",
            "uninstall-runner.ps1",
            "build-windows-executable.ps1", "SHA256SUMS.txt",
        ],
        "rez_adapter_bundle": {
            "included": bool(rez_files),
            "file_count": len(rez_files),
            "scope": "device drivers and normalized state model only",
        },
        "runner_source_bundle": {
            "included": bool(runner_files),
            "file_count": len(runner_files),
            "scope": "outbound connector and local execution modules",
        },
    }
