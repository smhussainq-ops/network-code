"""Netcode Shell desktop-client bootstrap contract.

The desktop client is a native local app target, not the browser terminal. This
module publishes the minimal non-secret profile a desktop client needs to talk
to the control plane while the runner remains the only component with device
credentials and device network reachability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


def _websocket_base_url(control_plane_url: str) -> str:
    parsed = urlparse(control_plane_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def build_desktop_shell_profile(control_plane_url: str, *, runner_pool: str = "default") -> dict[str, Any]:
    base = control_plane_url.rstrip("/")
    ws_base = _websocket_base_url(base)
    return {
        "ok": True,
        "profile_version": "netcode-shell-desktop.v1",
        "client": {
            "name": "Netcode Shell Desktop",
            "kind": "native-desktop",
            "browser_based": False,
            "targets": ["windows", "macos", "linux"],
        },
        "control_plane": {
            "base_url": base,
            "runner_pool": runner_pool,
            "device_credentials": "never_stored",
            "device_network_access": "none",
        },
        "transport": {
            "api_base_url": base,
            "shell_websocket_base_url": ws_base,
            "open_session": f"{base}/api/shell/open",
            "session_websocket": f"{ws_base}/api/shell/session/{{session_id}}",
            "attach_change": f"{base}/api/shell/attach",
            "quick_change": f"{base}/api/shell/quick-change",
            "transcript": f"{base}/api/shell/{{session_id}}/transcript",
        },
        "capabilities": {
            "interactive_ssh": True,
            "full_human_cli": True,
            "change_attachment": True,
            "quick_change_record": True,
            "session_transcript": True,
            "runner_side_guard": True,
        },
        "boundaries": {
            "netcode_shell": "full-capability human SSH through governed runner sessions",
            "rez_diagnostics": "read-only runner actions only",
            "credentials": "runner-local inventory only",
            "writes": "human shell or approved Netcode workflow only; never Rez diagnostics",
        },
    }


def write_profile(path: Path, profile: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netcode-shell-desktop", description="Generate a Netcode Shell Desktop bootstrap profile.")
    parser.add_argument("--server", required=True, help="Control-plane URL, for example https://netcode.example.com")
    parser.add_argument("--runner-pool", default="default")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args(argv)

    profile = build_desktop_shell_profile(args.server, runner_pool=args.runner_pool)
    if args.output:
        write_profile(Path(args.output), profile)
    else:
        print(json.dumps(profile, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
