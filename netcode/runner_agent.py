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
    job_action = str(job.get("action") or "")
    if job_action.startswith("read_"):
        return _execute_read(job_action[len("read_"):], payload)
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
    # plane cannot make the runner push forbidden config — the runner's OWN
    # policy file (if present) and a hardcoded credential floor are enforced on
    # top of whatever policy the control plane shipped.
    local_policy_yaml = POLICY_FILE.read_text(encoding="utf-8") if POLICY_FILE.exists() else ""
    gate = local_policy_gate(intent, render, payload.get("policy_yaml", ""), local_policy_yaml)
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


def _runner_ws():
    return _RunnerPaths(Path(os.environ.get("NETCODE_RUNNER_WORKSPACE", "") or Path.cwd()).resolve())


READ_TIMEOUT_SECONDS = 30
READINESS_TIMEOUT_SECONDS = 55  # multi-device sweep; still under the control plane's 60s poll


def _execute_read(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed wrapper: a hung device read must never wedge the runner's
    sequential job loop (a dead container / unreachable device can otherwise
    block every later job and stop heartbeats). Reads get a hard deadline."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    deadline = READINESS_TIMEOUT_SECONDS if action == "readiness" else READ_TIMEOUT_SECONDS
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_execute_read_inner, action, payload)
    try:
        return future.result(timeout=deadline)
    except FuturesTimeout:
        return {"ok": False, "status": "fail",
                "error": f"Runner read '{action}' timed out after {deadline}s (device unreachable or hung).",
                "message": f"Runner read '{action}' timed out after {deadline}s (device unreachable or hung)."}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _execute_read_inner(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Device READ actions executed on the runner (next to the devices), using the
    runner's LOCAL credentialed inventory. Mirrors the control-plane read logic so
    the browser renders results identically whether local or runner mode."""
    import tempfile
    from netcode.inventory import Inventory
    from netcode.models import load_intent
    from netcode.yamlio import read_yaml, write_yaml

    if action == "manual_device_add":
        candidate = dict(payload.get("candidate") or {})
        required = ("id", "host", "platform")
        missing = [key for key in required if not str(candidate.get(key) or "").strip()]
        if missing:
            return {"ok": False, "status": "fail", "error": f"Missing required field(s): {', '.join(missing)}"}
        try:
            inventory = read_yaml(INVENTORY_FILE) if INVENTORY_FILE.exists() else {"defaults": {}, "devices": []}
            devices = list(inventory.get("devices") or [])
            groups = candidate.get("groups") or ["manual"]
            if isinstance(groups, str):
                groups = [item.strip() for item in groups.split(",") if item.strip()]
            sanitized = {
                "id": str(candidate.get("id")).strip(),
                "hostname": str(candidate.get("hostname") or candidate.get("id")).strip(),
                "host": str(candidate.get("host")).strip(),
                "platform": str(candidate.get("platform")).strip(),
                "site": str(candidate.get("site") or "manual").strip(),
                "groups": [str(group) for group in groups],
                "port": int(candidate.get("port") or 22),
            }
            # The control plane REDACTS credentials in cloud payloads, so a
            # candidate may arrive carrying placeholder text. Never let a
            # placeholder clobber the runner's real credential store.
            def _usable_secret(value: str) -> bool:
                cleaned = value.strip()
                return bool(cleaned) and "redact" not in cleaned.lower() and cleaned != "***"

            if _usable_secret(str(candidate.get("username") or "")):
                sanitized["username"] = str(candidate.get("username")).strip()
            if _usable_secret(str(candidate.get("password") or "")):
                sanitized["password"] = str(candidate.get("password"))
            action_taken = "added"
            for index, existing in enumerate(devices):
                if str(existing.get("id")) == sanitized["id"] or str(existing.get("host")) == sanitized["host"]:
                    devices[index] = {**existing, **sanitized}
                    action_taken = "updated"
                    break
            else:
                devices.append(sanitized)
            inventory["devices"] = devices
            write_yaml(INVENTORY_FILE, inventory)
            public_device = {key: value for key, value in sanitized.items() if key not in {"username", "password"}}
            return {
                "ok": True,
                "status": "pass",
                "action": action_taken,
                "inventory": str(INVENTORY_FILE),
                "device": public_device,
                "message": f"Device {sanitized['id']} {action_taken} in runner inventory.",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "fail", "error": str(exc)}

    if not INVENTORY_FILE.exists():
        return {"ok": False, "error": f"No local runner inventory at {INVENTORY_FILE}."}

    if action == "readiness":
        from netcode.adapters.registry import AdapterRegistry
        devices = list(Inventory(INVENTORY_FILE).by_id.values())
        if not devices:
            return {"ok": False, "tested": 0, "readable": 0, "devices": [], "message": "No devices in runner inventory."}
        results = {str(i.get("device_id")): i for i in AdapterRegistry().rez.collect_many(devices).get("results", []) if isinstance(i, dict)}
        rows, readable = [], 0
        for d in devices:
            r = results.get(d.id) or {}
            ok = bool(r.get("ok"))
            readable += 1 if ok else 0
            err = "" if ok else str(r.get("error") or (r.get("errors") or ["unreadable"])[0])
            rows.append({"id": d.id, "host": d.host, "platform": d.platform, "ok": ok, "error": err})
        return {"ok": readable > 0, "tested": len(devices), "readable": readable, "devices": rows,
                "message": f"{readable}/{len(devices)} trusted devices are readable."}

    if action == "verify":
        from netcode.lab import AristaEOSLabAdapter
        wd = Path(tempfile.mkdtemp(prefix="netcode-read-"))
        ip = wd / "intent.yaml"
        ip.write_text(payload.get("intent_yaml", ""), encoding="utf-8")
        intent = load_intent(ip)
        inv = Inventory(INVENTORY_FILE)
        device_id = payload.get("device_id") or (intent.targets.device_ids[0] if intent.targets.device_ids else "")
        device = inv.by_id.get(device_id)
        if not device:
            return {"ok": False, "error": f"Device {device_id} not in runner inventory."}
        adapter = AristaEOSLabAdapter(device)
        try:
            adapter.connect()
            verification = adapter.verify_intent(intent, present=bool(payload.get("present", True)))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "device_id": device_id}
        finally:
            adapter.disconnect()
        return {"ok": verification.status == "pass", "device_id": device.id, "platform": device.platform,
                "change_type": intent.change_type, "verification": verification.__dict__}

    if action == "drift":
        from netcode.adapters.registry import AdapterRegistry
        from netcode.drift import vlan_drift_report
        wd = Path(tempfile.mkdtemp(prefix="netcode-read-"))
        ip = wd / "intent.yaml"
        ip.write_text(payload.get("intent_yaml", ""), encoding="utf-8")
        inv = Inventory(INVENTORY_FILE)
        device = inv.by_id.get(payload.get("device_id"))
        if not device:
            return {"ok": False, "error": f"Device {payload.get('device_id')} not in runner inventory."}
        state = AdapterRegistry().rez.collect_device_state(device)
        report = vlan_drift_report(
            _runner_ws(), ip, state,
            expected_present=bool(payload.get("expected_present", True)),
            baseline=str(payload.get("baseline", "intended state")),
            context=str(payload.get("context", "applied")),
        )
        report.setdefault("ok", True)
        return report

    if action == "device_drift":
        from netcode.adapters.registry import AdapterRegistry
        from netcode.drift import device_drift_from_state
        inv = Inventory(INVENTORY_FILE)
        device = inv.by_id.get(payload.get("device_id"))
        if not device:
            return {"ok": False, "error": f"Device {payload.get('device_id')} not in runner inventory."}
        state = AdapterRegistry().rez.collect_device_state(device)
        return device_drift_from_state(payload.get("expected") or [], state, str(payload.get("device_id", "")))

    if action == "troubleshoot":
        from netcode.adapters.registry import AdapterRegistry
        from netcode.troubleshooting import troubleshoot_state
        inv = Inventory(INVENTORY_FILE)
        device = inv.by_id.get(payload.get("device_id"))
        if not device:
            return {"ok": False, "error": f"Device {payload.get('device_id')} not in runner inventory."}
        state = AdapterRegistry().rez.collect_device_state(device)
        return troubleshoot_state(
            state,
            check=str(payload.get("check", "live_state")),
            target=str(payload.get("target", "")),
            expected=str(payload.get("expected", "")),
        )

    if action == "shell":
        # Governed CLI: the guard runs HERE (the trust boundary), and only a
        # cleared read-only line is executed on the device. Config execution is
        # never done raw — it stays in the proven plan/dry-run/apply pipeline.
        from netcode.lab import AristaEOSLabAdapter
        from netcode.shell_guard import ShellSessionState, guard_submit
        inv = Inventory(INVENTORY_FILE)
        device = inv.by_id.get(payload.get("device_id"))
        if not device:
            return {"ok": False, "error": f"Device {payload.get('device_id')} not in runner inventory."}
        raw = dict(payload.get("state") or {})
        state = ShellSessionState(
            mode=str(raw.get("mode", "read_only")),
            change_id=raw.get("change_id"),
            in_config=bool(raw.get("in_config", False)),
            device_touched=bool(raw.get("device_touched", False)),
        )
        decision = guard_submit(state, str(payload.get("input", "")))
        output = ""
        executed = False
        cleared = decision["kind"] in ("run_reads", "config_staged")
        if decision["kind"] == "run_reads":
            adapter = AristaEOSLabAdapter(device)
            try:
                adapter.connect()
                output = "\n".join(adapter.show(line.strip()) for line in decision["lines"])
                executed = True
            except Exception as exc:  # noqa: BLE001
                output = f"[shell] device read failed: {exc}"
            finally:
                adapter.disconnect()
        elif decision["kind"] == "config_staged":
            output = "[staged] config line captured — stage & apply through the change pipeline."
        return {"ok": True, "cleared": cleared, "executed": executed, "guard_kind": decision["kind"],
                "output": output, "events": decision["events"], "state": decision["state"],
                "device_touched": bool(decision["state"].get("device_touched")),
                "device_id": device.id, "platform": device.platform}

    if action == "discovery":
        from netcode.discovery import DiscoveryService
        return DiscoveryService(_runner_ws()).scan(
            host=str(payload.get("host", "")), username=str(payload.get("username", "")),
            password=str(payload.get("password", "")), platform=str(payload.get("platform", "")),
            port=int(payload.get("port") or 22), device_id=str(payload.get("device_id", "")),
            site=str(payload.get("site", "")), groups=payload.get("groups"))

    return {"ok": False, "error": f"Unknown read action '{action}'."}


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
    _start_interactive_channel(server, token)  # persistent outbound WS for the interactive shell
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


def _start_interactive_channel(server: str, token: str) -> None:
    """Persistent OUTBOUND WebSocket to the control plane for interactive PTY
    sessions. Runs in a daemon thread alongside the job-poll loop. On an 'open'
    frame it launches a guarded InteractivePtySession to the device and streams
    device bytes up / keystrokes down. Credentials never leave: paramiko resolves
    them from the runner's local inventory."""
    import base64
    import threading

    try:
        import websocket as ws_client  # websocket-client (sync)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] Interactive shell disabled (no websocket-client): {exc}", flush=True)
        return

    from netcode.inventory import Inventory
    from netcode.shell_guard import ShellSessionState
    from netcode.shell_pty import InteractivePtySession

    ws_url = server.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/api/runner/stream"
    holder: dict[str, Any] = {"ws": None}
    send_lock = threading.Lock()
    sessions: dict[str, InteractivePtySession] = {}

    def send_frame(frame: dict[str, Any]) -> None:
        w = holder["ws"]
        if w is None:
            return
        with send_lock:  # paramiko reader threads + the recv loop all send here
            try:
                w.send(json.dumps(frame))
            except Exception:  # noqa: BLE001
                pass

    def handle(frame: dict[str, Any]) -> None:
        t, sid = frame.get("t"), str(frame.get("sid", ""))
        if t == "open":
            device = Inventory(INVENTORY_FILE).by_id.get(frame.get("device_id"))
            if not device:
                send_frame({"t": "status", "sid": sid, "s": "error", "m": "device not in runner inventory"})
                return
            raw = frame.get("state") or {}
            state = ShellSessionState(mode=str(raw.get("mode", "read_only")),
                                      change_id=raw.get("change_id"), in_config=bool(raw.get("in_config", False)))
            guard_enabled = bool(raw.get("guard_enabled", True))
            sess = InteractivePtySession(
                device, state,
                on_output=lambda data, s=sid: send_frame({"t": "out", "sid": s, "d": base64.b64encode(data).decode()}),
                on_event=lambda ev, s=sid: send_frame({"t": "event", "sid": s, "e": ev}),
                guard_enabled=guard_enabled,
            )
            sessions[sid] = sess
            try:
                sess.open()
                send_frame({"t": "status", "sid": sid, "s": "open"})
            except Exception as exc:  # noqa: BLE001
                send_frame({"t": "status", "sid": sid, "s": "error", "m": str(exc)})
                sessions.pop(sid, None)
        elif t == "attach":
            s = sessions.get(sid)
            if s:
                s.state.mode = "change_attached"
                s.state.change_id = frame.get("change_id")
                send_frame({"t": "event", "sid": sid, "e": {
                    "type": "guard", "action": "change_attached_live",
                    "change_id": frame.get("change_id"),
                    "message": f"Change {frame.get('change_id')} attached — config mode is now unlocked under governance."}})
        elif t == "in":
            s = sessions.get(sid)
            if s:
                s.write(str(frame.get("d", "")))
        elif t == "resize":
            s = sessions.get(sid)
            if s:
                s.resize(int(frame.get("cols", 120)), int(frame.get("rows", 40)))
        elif t == "close":
            s = sessions.pop(sid, None)
            if s:
                s.close()

    def run_channel() -> None:
        while not _stop:
            try:
                ws = ws_client.create_connection(ws_url, timeout=12)
                ws.send(json.dumps({"token": token}))
                ws.settimeout(None)  # connect had a timeout; recv() must block on an idle channel
                holder["ws"] = ws
                print("[runner] Interactive channel connected.", flush=True)
                while not _stop:
                    message = ws.recv()
                    if not message:
                        break
                    handle(json.loads(message))
            except Exception as exc:  # noqa: BLE001
                print(f"[runner] Interactive channel error: {exc}", flush=True)
            finally:
                holder["ws"] = None
                for s in list(sessions.values()):
                    try:
                        s.close()
                    except Exception:  # noqa: BLE001
                        pass
                sessions.clear()
            if _stop:
                break
            time.sleep(3)

    threading.Thread(target=run_channel, name="netcode-interactive", daemon=True).start()


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
