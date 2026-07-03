"""Netcode on-prem runner — a pure outbound client that executes lab jobs next to the devices.

Design (Phase 0):
- Outbound-only: dials the control plane over HTTP(S); never listens on a port.
- Two-phase enrollment: single-use join token -> per-runner token + HMAC secret,
  stored locally in ~/.netcode-runner/identity.json.
- Credentials never come from the cloud: the runner resolves device credentials
  from its OWN local inventory (~/.netcode-runner/inventory.yaml) by device id.
- Second safety gate: re-runs the fail-closed policy checks locally before any
  device is touched, so a compromised control plane cannot push forbidden config.
- Signs results with HMAC-SHA256 so the control plane can prove they came from
  this runner.

Stdlib only (urllib) so it can run anywhere Python 3.10+ exists.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

IDENTITY_DIR = Path.home() / ".netcode-runner"
IDENTITY_FILE = IDENTITY_DIR / "identity.json"
INVENTORY_FILE = IDENTITY_DIR / "inventory.yaml"
POLICY_FILE = IDENTITY_DIR / "policy.yaml"
VERSION = "0.1.0-phase0"

_stop = False


def _handle_sigterm(signum, frame):  # noqa: ANN001
    global _stop
    _stop = True
    print("\n[runner] SIGTERM received — will exit after the current job drains.", flush=True)


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _post(server: str, path: str, body: dict[str, Any], token: str | None = None, timeout: float = 40.0):
    url = server.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from exc


def enroll(args: argparse.Namespace) -> int:
    resp = _post(args.server, "/api/runner/enroll", {"join_token": args.join_token, "name": args.name})
    if not resp or not resp.get("ok"):
        print(f"[runner] Enrollment failed: {(resp or {}).get('message', 'unknown error')}", file=sys.stderr)
        return 1
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    identity = {
        "server": args.server,
        "runner_id": resp["runner_id"],
        "runner_token": resp["runner_token"],
        "hmac_secret": resp["hmac_secret"],
        "pool": resp["pool"],
        "name": args.name,
    }
    IDENTITY_FILE.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    IDENTITY_FILE.chmod(0o600)
    print(f"[runner] Enrolled '{args.name}' into pool '{resp['pool']}'. Identity saved to {IDENTITY_FILE}")
    if not INVENTORY_FILE.exists():
        print(f"[runner] NOTE: put your device inventory (with credentials) at {INVENTORY_FILE}")
    return 0


def _load_identity() -> dict[str, Any]:
    if not IDENTITY_FILE.exists():
        raise SystemExit(f"[runner] Not enrolled. Run: netcode-runner enroll --server ... --join-token ...")
    return json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))


def _execute_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run one lab job locally: re-validate (fail-closed), resolve local creds, execute via the shared adapter."""
    # Imports are local so `enroll` works even without the full netcode package installed.
    import tempfile
    from netcode.inventory import Inventory
    from netcode.lab import AristaEOSLabAdapter
    from netcode.models import load_intent
    from netcode.rendering import render_intent
    from netcode.runner_checks import local_policy_gate

    payload = job.get("payload") or {}
    action = payload.get("action")
    device_spec = payload.get("device") or {}
    device_id = device_spec.get("id")

    # Render workspace: the runner uses ITS OWN templates (never the control
    # plane's rendered output) so it fully controls what gets pushed. Defaults to
    # the runner's working directory, which ships the templates/ tree.
    ws_root = Path(os.environ.get("NETCODE_RUNNER_WORKSPACE", "") or Path.cwd()).resolve()
    workdir = Path(tempfile.mkdtemp(prefix="netcode-runner-"))
    intent_path = workdir / "intent.yaml"
    intent_path.write_text(payload.get("intent_yaml", ""), encoding="utf-8")
    intent = load_intent(intent_path)
    render = render_intent(intent, _RunnerPaths(ws_root))

    # Second safety gate: local fail-closed policy re-check. A compromised control
    # plane cannot make the runner push forbidden config.
    gate = local_policy_gate(intent, render, payload.get("policy_yaml", ""))
    if not gate["ok"]:
        return {
            "status": "fail",
            "action": action,
            "device_id": device_id,
            "message": f"Blocked by local runner policy: {gate['message']}",
            "evidence": {"local_policy": gate},
        }

    # Credentials come ONLY from the runner's local inventory, never from the cloud payload.
    if not INVENTORY_FILE.exists():
        return {"status": "fail", "action": action, "device_id": device_id,
                "message": f"No local inventory at {INVENTORY_FILE}; cannot resolve credentials."}
    inventory = Inventory(INVENTORY_FILE)
    device = inventory.by_id.get(device_id)
    if device is None:
        return {"status": "fail", "action": action, "device_id": device_id,
                "message": f"Device {device_id} not in local runner inventory."}

    adapter = AristaEOSLabAdapter(device)
    if action == "dry-run":
        lab = adapter.dry_run(intent, render)
    elif action == "apply":
        lab = adapter.apply(intent, render)
    elif action == "rollback":
        lab = adapter.rollback(intent, render)
    else:
        return {"status": "fail", "action": action, "device_id": device_id, "message": f"Unknown action {action}."}
    result = lab.__dict__ if hasattr(lab, "__dict__") else dict(lab)
    result.setdefault("action", action)
    result.setdefault("device_id", device_id)
    result["runner_version"] = VERSION
    return result


class _RunnerPaths:
    """Minimal WorkspacePaths shim: rendering only needs the template dir, which
    ships with the installed netcode package, plus a rendered/ dir."""

    def __init__(self, root: Path):
        from netcode.paths import WorkspacePaths
        self._real = WorkspacePaths(root)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


def run(args: argparse.Namespace) -> int:
    identity = _load_identity()
    server, token, secret, pool = identity["server"], identity["runner_token"], identity["hmac_secret"], identity["pool"]
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    print(f"[runner] Polling {server} for pool '{pool}' as {identity['name']} (v{VERSION}). Ctrl-C to stop.")
    try:
        _post(server, "/api/runner/heartbeat", {"version": VERSION}, token=token)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] Heartbeat failed (continuing): {exc}", file=sys.stderr)
    while not _stop:
        try:
            resp = _post(server, "/api/runner/poll", {"wait_seconds": 20}, token=token, timeout=40)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] Poll error: {exc}; retrying in 5s.", file=sys.stderr)
            time.sleep(5)
            continue
        if not resp:
            continue  # 204: no job, poll again
        job = resp.get("job") or {}
        job_id = job.get("id")
        action = (job.get("payload") or {}).get("action")
        print(f"[runner] Claimed job {job_id} ({action}).")
        try:
            result = _execute_job(job)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "fail", "message": f"Runner execution error: {type(exc).__name__}: {exc}"}
        signature = hmac.new(secret.encode("utf-8"), _canonical(result).encode("utf-8"), hashlib.sha256).hexdigest()
        try:
            ack = _post(server, f"/api/runner/jobs/{job_id}/result", {"result": result, "signature": signature}, token=token)
            print(f"[runner] Reported job {job_id}: {result.get('status')} — {(ack or {}).get('message', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] Failed to report job {job_id}: {exc}", file=sys.stderr)
    print("[runner] Stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netcode-runner", description="Netcode on-prem runner (outbound-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    p_enroll = sub.add_parser("enroll", help="Enroll this runner with a single-use join token.")
    p_enroll.add_argument("--server", required=True, help="Control-plane base URL, e.g. http://host.orb.internal:8088")
    p_enroll.add_argument("--join-token", required=True)
    p_enroll.add_argument("--name", default="runner")
    p_enroll.set_defaults(func=enroll)
    p_run = sub.add_parser("run", help="Poll for and execute jobs.")
    p_run.set_defaults(func=run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
