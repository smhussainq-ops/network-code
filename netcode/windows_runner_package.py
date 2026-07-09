"""Windows runner package generation.

The package is intentionally thin: it installs the same Python runner, enrolls
it to the control plane, imports runner-local inventory, and optionally registers
an auto-start scheduled task. It never contains tokens or device credentials.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from textwrap import dedent
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from netcode.shell_desktop import build_desktop_shell_profile


def _rez_runtime_files() -> dict[str, bytes]:
    """Return the device-driver-only Rez runtime for the local runner."""
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
    """Bundle the exact runner source so pilot installs do not depend on PyPI."""
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


def _install_runner_ps1(control_plane_url: str) -> str:
    return dedent(
        f"""
        param(
          [Parameter(Mandatory=$true)][string]$JoinToken,
          [string]$RunnerName = $env:COMPUTERNAME,
          [string]$PackageSpec = "",
          [switch]$RegisterStartupTask
        )

        $ErrorActionPreference = "Stop"
        $Root = Join-Path $env:ProgramData "NetcodeRunner"
        $Venv = Join-Path $Root ".venv"
        $Python = Join-Path $Venv "Scripts\\python.exe"
        $RezSource = Join-Path $PSScriptRoot "rez-runtime"
        $RezRoot = Join-Path $Root "rez-runtime"
        New-Item -ItemType Directory -Force -Path $Root | Out-Null

        if (-not (Get-Command py -ErrorAction SilentlyContinue)) {{
          throw "Python launcher 'py' was not found. Install Python 3.10+ for Windows first."
        }}

        if (-not (Test-Path $Python)) {{
          py -3 -m venv $Venv
        }}

        & $Python -m pip install --upgrade pip
        if ($PackageSpec) {{
          & $Python -m pip install --upgrade $PackageSpec
        }} else {{
          $BundledSource = Join-Path $PSScriptRoot "runner-source"
          if (-not (Test-Path (Join-Path $BundledSource "pyproject.toml"))) {{
            throw "The bundled Netcode runner source is missing. Download a fresh runner package."
          }}
          & $Python -m pip install --upgrade $BundledSource
        }}
        if (-not (Test-Path (Join-Path $RezSource "drivers\\collector.py"))) {{
          throw "The Rez multi-vendor adapter bundle is missing. Download a fresh runner package."
        }}
        New-Item -ItemType Directory -Force -Path $RezRoot | Out-Null
        Copy-Item -Path (Join-Path $RezSource "*") -Destination $RezRoot -Recurse -Force
        $env:NETCODE_REZ_ROOT = $RezRoot
        & $Python -m netcode.runner_agent enroll --server "{control_plane_url}" --join-token $JoinToken --name $RunnerName

        if ($RegisterStartupTask) {{
          $StartScript = Join-Path $PSScriptRoot "start-runner.ps1"
          $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
          $Trigger = New-ScheduledTaskTrigger -AtStartup
          Register-ScheduledTask -TaskName "NetcodeRunner" -Action $Action -Trigger $Trigger -Description "Netcode outbound local runner" -RunLevel Highest -Force | Out-Null
          Write-Host "Registered startup task: NetcodeRunner"
        }}

        Write-Host "Netcode runner installed and enrolled."
        Write-Host "Next: .\\import-inventory.ps1 -InventoryPath .\\sample-inventory.yaml"
        """
    ).strip() + "\n"


def _start_runner_ps1() -> str:
    return dedent(
        """
        $ErrorActionPreference = "Stop"
        $Root = Join-Path $env:ProgramData "NetcodeRunner"
        $Python = Join-Path $Root ".venv\\Scripts\\python.exe"
        $env:NETCODE_REZ_ROOT = Join-Path $Root "rez-runtime"
        $LogDir = Join-Path $Root "logs"
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        $LogFile = Join-Path $LogDir ("runner-" + (Get-Date -Format "yyyyMMdd") + ".log")

        if (-not (Test-Path $Python)) {
          throw "Runner virtual environment not found. Run install-runner.ps1 first."
        }

        & $Python -m netcode.runner_agent run *>> $LogFile
        """
    ).strip() + "\n"


def _import_inventory_ps1() -> str:
    return dedent(
        """
        param(
          [Parameter(Mandatory=$true)][string]$InventoryPath
        )

        $ErrorActionPreference = "Stop"
        $Root = Join-Path $env:ProgramData "NetcodeRunner"
        $Python = Join-Path $Root ".venv\\Scripts\\python.exe"

        if (-not (Test-Path $Python)) {
          throw "Runner virtual environment not found. Run install-runner.ps1 first."
        }
        if (-not (Test-Path $InventoryPath)) {
          throw "Inventory file not found: $InventoryPath"
        }

        & $Python -m netcode.runner_agent inventory-import $InventoryPath
        Write-Host "Inventory imported into the runner-local credential store."
        """
    ).strip() + "\n"


def _sample_inventory_yaml() -> str:
    return dedent(
        """
        defaults:
          platform: arista_eos
          username: admin
          password: replace-me
          port: 22
        devices:
          - id: gns3-core-01
            hostname: gns3-core-01
            host: 192.0.2.10
            platform: arista_eos
            site: gns3-lab
            groups:
              - lab
              - core

        # Hybrid SSH + API example. These values remain on the local runner:
        # - id: edge-fw-01
        #   hostname: edge-fw-01
        #   host: 192.0.2.20
        #   platform: fortinet
        #   username: admin
        #   password: replace-me
        #   port: 22
        #   connection:
        #     transport: api
        #     ssh_port: 22
        #     api_port: 443
        #     api_token: replace-me
        #     verify_ssl: false
        """
    ).strip() + "\n"


def _readme(control_plane_url: str) -> str:
    return dedent(
        f"""
        # Netcode Windows Runner

        This package installs the outbound-only Netcode local runner for Windows.
        It is the component that can reach your lab or enterprise devices. The
        control plane at `{control_plane_url}` does not receive SSH/API
        credentials and does not open inbound connections to this machine.

        ## Install

        Open PowerShell as Administrator:

        ```powershell
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
        .\\install-runner.ps1 -JoinToken "<single-use-token>" -RunnerName "windows-gns3-runner" -RegisterStartupTask
        .\\import-inventory.ps1 -InventoryPath .\\sample-inventory.yaml
        .\\start-runner.ps1
        ```

        ## Network model

        - Outbound HTTPS/WSS only from the runner to the control plane.
        - No inbound listener is opened by the runner.
        - Device credentials are stored in the runner-local inventory.
        - The package includes the lightweight Rez driver runtime for 11 active
          platforms; no LLM, MCP server, Chat-v2 backend, or math engine runs on
          the customer runner.
        - SSH-capable devices can use Netcode Shell. API-only controllers use
          discovery, live state, diagnostics, and verification without a Shell.
        - Rez Diagnostics uses read-only runner jobs.
        - Netcode writes require plan, dry-run/canary, human approval, apply,
          and verification gates.

        ## Logs

        Logs are written to:

        ```text
        C:\\ProgramData\\NetcodeRunner\\logs
        ```
        """
    ).strip() + "\n"


def build_windows_runner_package(control_plane_url: str, *, runner_pool: str = "default") -> bytes:
    """Return a ZIP package suitable for download from the control plane."""
    profile = build_desktop_shell_profile(control_plane_url, runner_pool=runner_pool)
    files: dict[str, str | bytes] = {
        "README.md": _readme(control_plane_url),
        "install-runner.ps1": _install_runner_ps1(control_plane_url),
        "start-runner.ps1": _start_runner_ps1(),
        "import-inventory.ps1": _import_inventory_ps1(),
        "sample-inventory.yaml": _sample_inventory_yaml(),
        "netcode-shell-profile.json": json.dumps(profile, indent=2) + "\n",
    }
    files.update(_rez_runtime_files())
    files.update(_runner_source_files())

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
        "platform": "windows",
        "control_plane_url": control_plane_url.rstrip("/"),
        "runner_pool": runner_pool,
        "files": [
            "README.md",
            "install-runner.ps1",
            "start-runner.ps1",
            "import-inventory.ps1",
            "sample-inventory.yaml",
            "netcode-shell-profile.json",
        ],
        "network": "outbound_https_wss_only",
        "credentials": "runner_local_only",
        "rez_adapter_bundle": {
            "included": bool(rez_files),
            "file_count": len(rez_files),
            "scope": "device drivers and normalized state model only",
        },
        "runner_source_bundle": {
            "included": bool(runner_files),
            "file_count": len(runner_files),
            "scope": "outbound runner and local execution modules",
        },
    }
