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


PACKAGE_VERSION = "0.3.1-community-preview"


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
          [switch]$AllowInsecureHttpForLab,
          [switch]$RegisterStartupTask,
          [switch]$StartNow,
          [switch]$NoOpenControl
        )

        $ErrorActionPreference = "Stop"
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
        if ($ExistingTask) {{ Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }}
        Get-Process -Name "RezonanceLocalConnector" -ErrorAction SilentlyContinue | ForEach-Object {{
          try {{
            if ($_.Path -and $_.Path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {{
              Stop-Process -Id $_.Id -Force
              $_.WaitForExit(15000)
            }}
          }} catch {{
            throw "Unable to stop the installed Local Connector process: $($_.Exception.Message)"
          }}
        }}

        foreach ($Script in @("start-runner.ps1", "open-connector.ps1", "diagnose-runner.ps1", "uninstall-runner.ps1")) {{
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
          Write-Host "Enrollment is required. The Local Connector window will request the one-time join token."
        }}

        # SYSTEM runs the startup task and DPAPI uses machine scope. Restrict the
        # ProgramData tree to SYSTEM, administrators, and the installing user.
        $CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $Root /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" "${{CurrentUser}}:(OI)(CI)M" | Out-Null
        if ($LASTEXITCODE -ne 0) {{ throw "Unable to protect Local Connector data permissions." }}

        $InstalledStart = Join-Path $ScriptsRoot "start-runner.ps1"
        if ($RegisterStartupTask) {{
          $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstalledStart`""
          $Trigger = New-ScheduledTaskTrigger -AtStartup
          $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
          Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Description "Rezonance outbound-only Local Connector" -Force | Out-Null
          Write-Host "Registered startup task: $TaskName"
        }}
        if ($StartNow) {{
          if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {{
            Start-ScheduledTask -TaskName $TaskName
          }} else {{
            Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$InstalledStart`"" -WindowStyle Hidden
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

        Write-Host $(if ($IdentityExists -or $JoinToken) {{ "Rezonance Local Connector installed and enrolled." }} else {{ "Rezonance Local Connector installed. Complete enrollment in the control window." }})
        Write-Host "Next: discover local inventory in the Rezonance Local Connector window."
        if (-not $NoOpenControl) {{
          & (Join-Path $ScriptsRoot "open-connector.ps1")
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
        $LogFile = Join-Path $LogDir ("connector-" + (Get-Date -Format "yyyyMMdd") + ".log")
        if (Test-Path $Executable) {
          & $Executable run *>> $LogFile
        } elseif (Test-Path $Python) {
          & $Python -m netcode.runner_agent run *>> $LogFile
        } else {
          throw "Local Connector runtime not found. Run install-runner.ps1 first."
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


def _uninstall_runner_ps1() -> str:
    return dedent(
        r"""
        param([switch]$PurgeLocalData)
        $ErrorActionPreference = "Stop"
        $Root = Join-Path $env:ProgramData "Rezonance\LocalConnector"
        $TaskName = "RezonanceLocalConnector"
        $ShortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Rezonance Local Connector.lnk"
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
          Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
          Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        if ($PurgeLocalData) {
          Remove-Item -Recurse -Force $Root -ErrorAction SilentlyContinue
          Write-Host "Local Connector and all local identity, inventory, and logs were removed."
        } else {
          foreach ($Name in @(".venv", "bin", "rez-runtime", "scripts", "connector-settings.json")) {
            Remove-Item -Recurse -Force (Join-Path $Root $Name) -ErrorAction SilentlyContinue
          }
          Write-Host "Runtime removed. Protected data and logs remain at $Root. Use -PurgeLocalData to remove them."
        }
        Remove-Item -Force $ShortcutPath -ErrorAction SilentlyContinue
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
            --windows-console-mode=attach `
            --include-package=netcode `
            --include-package=netmiko `
            --include-package=paramiko `
            --include-package=ntc_templates `
            --include-package=textfsm `
            --include-package=yaml `
            --include-package=websockets `
            --include-package-data=certifi `
            --include-package-data=ntc_templates `
            --include-package-data=tzdata `
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

        ## Pilot install

        Open PowerShell as Administrator:

        ```powershell
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
        .\preflight.ps1 -ControlPlaneUrl "{control_plane_url}"
        .\install-runner.ps1 -JoinToken "<single-use-token>" -RunnerName "windows-gns3-connector" -RegisterStartupTask -StartNow
        .\diagnose-runner.ps1
        ```

        The installer opens the Local Connector control application. Enter a
        bounded seed IP, range, or CIDR and local device credentials there.
        Community discovery is limited to 25 devices. Only devices successfully
        collected by Rez become local inventory records.

        To point a Windows/GNS3 pilot at a Mac control plane, pass the Mac LAN
        URL explicitly and permit HTTP only on that private test network:

        ```powershell
        .\install-runner.ps1 -JoinToken "<single-use-token>" -ControlPlaneUrl "http://MAC-LAN-IP:8095" -AllowInsecureHttpForLab -RegisterStartupTask -StartNow
        ```

        ## Security model

        - Outbound HTTPS/WSS only in production; no inbound listener is opened.
        - Device access uses SSH/API from this connector to the local network.
        - Windows identity and inventory files use machine-scoped DPAPI.
        - NTFS access is restricted to SYSTEM and local administrators.
        - The control plane receives public inventory facts and signed job results, never credentials.
        - Rez jobs are read-only. Netcode writes remain plan-, approval-, and verification-gated.
        - Proxy and custom enterprise CA paths can be supplied during install.

        ## Clean-machine executable

        Release engineers build with the licensed compiler environment:

        ```powershell
        .\build-windows-executable.ps1 -CommercialPython "C:\path\to\commercial-venv\Scripts\python.exe" -Clean
        ```

        The generated `bin` runtime removes the Python prerequisite. Production
        distribution still requires Authenticode code signing.

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
            "open-connector.ps1", "diagnose-runner.ps1", "uninstall-runner.ps1",
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
