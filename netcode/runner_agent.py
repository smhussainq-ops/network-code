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
import ipaddress
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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


def import_inventory(args: argparse.Namespace) -> int:
    """Install a credentialed device inventory on the local runner only."""
    from netcode.yamlio import read_yaml

    source = Path(args.file).expanduser()
    if not source.exists():
        print(f"[runner] Inventory file not found: {source}", file=sys.stderr)
        return 1
    try:
        data = read_yaml(source)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] Failed to parse inventory YAML: {exc}", file=sys.stderr)
        return 1
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, list) or not devices:
        print("[runner] Inventory must contain a non-empty 'devices' list.", file=sys.stderr)
        return 1

    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    INVENTORY_FILE.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    INVENTORY_FILE.chmod(0o600)
    print(f"[runner] Imported {len(devices)} device(s) into {INVENTORY_FILE}")
    print("[runner] Credentials stay on this runner. They are not sent to the control plane.")
    return 0


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
    device = inventory.find_device(device_id)
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
MAX_READ_TIMEOUT_SECONDS = 120


def _collapse_command(command: str) -> str:
    return " ".join(str(command or "").strip().split())


_POST_PIPE_ALLOWED_FILTERS = frozenset(
    (
        # Arista EOS/Cisco-style read filters.
        "include",
        "exclude",
        "section",
        "begin",
        "count",
        "json",
        "no-more",
        "nz",
        "last",
        "natural",
        # Junos-style read filters.
        "match",
        "except",
        "display",
        "trim",
    )
)


def _pipe_segments_allowed(command: str) -> tuple[bool, str]:
    if "|" not in command:
        return True, "allowed"
    for segment in command.split("|")[1:]:
        lowered = segment.strip().lower()
        if not lowered:
            return False, "blocked empty post-pipe segment"
        keyword = lowered.split(None, 1)[0]
        if keyword not in _POST_PIPE_ALLOWED_FILTERS:
            return False, f"blocked post-pipe command '| {keyword}'"
    return True, "allowed"


def _rez_read_command_allowed(command: str) -> tuple[bool, str]:
    """Deny-by-default guard for Rez runner SSH reads.

    This intentionally does not reuse the interactive shell guard, which is
    good-faith allow-by-default for humans. Rez runner reads are machine-issued
    RCA commands, so the safer floor is a small set of read verbs plus no
    chaining/redirection.
    """
    cleaned = _collapse_command(command)
    lowered = cleaned.lower()
    if not lowered:
        return False, "empty command"
    for sep in (";", "&", "`", "$(", ">", "<", "\x00", "\n", "\r"):
        if sep in lowered:
            return False, f"blocked command separator {sep!r}"
    pipe_ok, pipe_reason = _pipe_segments_allowed(cleaned)
    if not pipe_ok:
        return False, pipe_reason
    first = lowered.split(" ", 1)[0]
    allowed = {
        "show",
        "get",
        "display",
        "ping",
        "traceroute",
        "traceroute6",
        "nslookup",
    }
    if first not in allowed:
        return False, f"verb {first!r} is not in the read-only allowlist"
    return True, "allowed"


def _netmiko_device_type(platform: str) -> str:
    normalized = str(platform or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "arista": "arista_eos",
        "arista_eos": "arista_eos",
        "eos": "arista_eos",
        "cisco_ios": "cisco_ios",
        "ios": "cisco_ios",
        "iosxe": "cisco_xe",
        "cisco_iosxe": "cisco_xe",
        "cisco_xe": "cisco_xe",
        "nxos": "cisco_nxos",
        "cisco_nxos": "cisco_nxos",
        "fortigate": "fortinet",
        "fortinet": "fortinet",
        "fortios": "fortinet",
        "palo_alto": "paloalto_panos",
        "panos": "paloalto_panos",
        "paloalto_panos": "paloalto_panos",
        "junos": "juniper_junos",
        "juniper_junos": "juniper_junos",
    }
    return aliases.get(normalized, normalized or "arista_eos")


def _execute_rez_ssh_command(payload: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from netcode.inventory import Inventory

    device_id = str(payload.get("device") or payload.get("device_id") or "").strip()
    command = _collapse_command(str(payload.get("command") or ""))
    if not device_id:
        return {"ok": False, "status": "fail", "error": "device_id is required"}
    ok, reason = _rez_read_command_allowed(command)
    if not ok:
        return {
            "ok": False,
            "status": "blocked",
            "device": device_id,
            "command": command,
            "error": f"Command blocked by runner read-only policy: {reason}",
        }
    inv = Inventory(INVENTORY_FILE)
    device = inv.find_device(device_id)
    if not device:
        return {"ok": False, "status": "fail", "device": device_id, "command": command, "error": f"Device {device_id} not in local runner inventory."}
    started = _time.monotonic()
    try:
        from netmiko import ConnectHandler
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "fail", "device": device.id, "command": command, "error": f"netmiko is required for Rez SSH reads: {exc}"}

    conn = None
    try:
        conn = ConnectHandler(
            device_type=_netmiko_device_type(device.platform),
            host=device.host,
            username=device.username,
            password=device.password,
            port=device.port,
            fast_cli=False,
            conn_timeout=20,
            auth_timeout=20,
            banner_timeout=20,
        )
        try:
            conn.enable()
        except Exception:
            pass
        if _netmiko_device_type(device.platform) in {"arista_eos", "cisco_ios", "cisco_xe", "cisco_nxos"}:
            try:
                conn.send_command_timing("terminal length 0", strip_prompt=False, strip_command=False, read_timeout=10)
            except Exception:
                pass
        try:
            output = conn.send_command(command, strip_prompt=False, strip_command=False, read_timeout=30)
        except Exception:
            output = conn.send_command_timing(command, strip_prompt=False, strip_command=False, read_timeout=30)
        return {
            "ok": True,
            "status": "pass",
            "device": device.id,
            "hostname": device.hostname,
            "platform": device.platform,
            "host": device.host,
            "port": device.port,
            "command": command,
            "stdout": output,
            "stderr": "",
            "duration_ms": int((_time.monotonic() - started) * 1000),
            "runner_version": VERSION,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "fail",
            "device": device.id,
            "platform": device.platform,
            "command": command,
            "stdout": "",
            "stderr": str(exc),
            "error": str(exc),
            "duration_ms": int((_time.monotonic() - started) * 1000),
            "runner_version": VERSION,
        }
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def _public_state_metadata(state: dict[str, Any], device: Any) -> dict[str, Any]:
    nested_device = state.get("device") if isinstance(state.get("device"), dict) else {}
    meta: dict[str, Any] = {
        "node_id": state.get("node_id") or state.get("device_id") or getattr(device, "id", ""),
        "hostname": state.get("hostname") or nested_device.get("hostname") or getattr(device, "hostname", ""),
        "vendor": state.get("vendor") or nested_device.get("vendor") or "",
        "platform": state.get("platform") or getattr(device, "platform", ""),
    }
    return {k: v for k, v in meta.items() if v not in (None, "")}


def _filter_state_sections(state: dict[str, Any], sections: list[str] | None, device: Any) -> tuple[dict[str, Any], list[str]]:
    available = sorted(k for k, v in state.items() if isinstance(v, (dict, list)))
    if not sections:
        return state, available
    wanted = {str(section).strip() for section in sections if str(section).strip()}
    filtered = _public_state_metadata(state, device)
    for section in wanted:
        if section in state:
            filtered[section] = state[section]
    return filtered, available


_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "system_status": ("device", "system", "system_status", "identity"),
    "device_info": ("device", "system", "system_status", "identity"),
    "interfaces": ("interfaces", "interface_status", "ports"),
    "routing": ("routing", "routes", "route_table"),
    "bgp_summary": ("bgp", "bgp_summary"),
    "bgp_neighbors": ("bgp", "bgp_neighbors"),
    "ospf_neighbors": ("ospf", "ospf_neighbors"),
    "lldp_neighbors": ("lldp", "lldp_neighbors", "neighbors"),
    "arp_table": ("arp_table", "arp", "arp_entries"),
    "vlans": ("vlans", "layer2"),
    "system_resources": ("system_resources", "resources"),
    "firewall_sessions": ("firewall_sessions", "sessions"),
    "firewall_policies": ("firewall_policies", "security_policies", "policies"),
    "address_objects": ("address_objects",),
    "service_objects": ("service_objects",),
    "vip_objects": ("vip_objects",),
    "nat_rules": ("nat_rules",),
    "ha_status": ("ha_status", "ha"),
    "ha_config": ("ha_config",),
    "vpn_ipsec": ("vpn_ipsec", "tunnels"),
    "vpn_phase1": ("vpn_phase1",),
    "vpn_phase2": ("vpn_phase2",),
    "sdwan_health": ("sdwan_health", "health_checks"),
    "sdwan_members": ("sdwan_members", "members"),
    "sdwan_sla": ("sdwan_sla", "sla"),
    "sdwan_services": ("sdwan_services", "services"),
}


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any] | None:
    for key in keys:
        if key in mapping and mapping[key] not in (None, "", [], {}):
            return key, mapping[key]
    return None


def _extract_category(state: dict[str, Any], category: str) -> tuple[str, Any] | None:
    normalized = str(category or "").strip().lower()
    if not normalized:
        return None
    direct = _first_present(state, (normalized,))
    if direct:
        return direct
    aliases = _CATEGORY_ALIASES.get(normalized, ())
    found = _first_present(state, aliases)
    if found:
        return found
    # Common Rez state shapes keep security-related facts under state["security"].
    security = state.get("security")
    if isinstance(security, dict):
        found = _first_present(security, aliases + (normalized,))
        if found:
            return found
    # SD-WAN drivers may keep facts under a nested sdwan block.
    sdwan = state.get("sdwan")
    if isinstance(sdwan, dict):
        found = _first_present(sdwan, aliases + (normalized,))
        if found:
            return found
    return None


def _collect_rez_runner_state(device_id: str) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    from netcode.adapters.registry import AdapterRegistry
    from netcode.inventory import Inventory

    inv = Inventory(INVENTORY_FILE)
    device = inv.find_device(device_id)
    if not device:
        return None, None, {"ok": False, "status": "fail", "device": device_id, "error": f"Device {device_id} not in local runner inventory."}
    result = AdapterRegistry().rez.collect_device_state(device)
    if not result.get("ok"):
        return device, None, {
            "ok": False,
            "status": "fail",
            "device": device.id,
            "platform": device.platform,
            "error": str(result.get("error") or (result.get("errors") or ["state collection failed"])[0]),
            "errors": result.get("errors") or [],
            "warnings": result.get("warnings") or [],
        }
    state = result.get("state")
    if not isinstance(state, dict):
        return device, None, {"ok": False, "status": "fail", "device": device.id, "platform": device.platform, "error": "Rez driver returned non-object state."}
    return device, state, None


def _resolve_inventory_device(identifier: str):
    from netcode.inventory import Inventory

    target = str(identifier or "").strip()
    if not target:
        return None
    inv = Inventory(INVENTORY_FILE)
    return inv.find_device(target)


def _http_status_from_probe_output(output: str) -> int | None:
    matches = re.findall(r"HTTP/(?:1\.[01]|2)\s+(\d{3})\b", str(output or ""), flags=re.IGNORECASE)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except Exception:
        return None


def _probe_timeout_seconds(value: Any, *, default: float, maximum: float) -> int:
    try:
        requested = float(value or default)
    except Exception:
        requested = default
    return max(1, min(int(requested), int(maximum)))


def _safe_listener_probe_ip(value: str) -> str:
    ip = ipaddress.ip_address(str(value).strip())
    if ip.version != 4:
        raise ValueError("only IPv4 listener probes are supported")
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local:
        raise ValueError("listener probe target must not be loopback, link-local, multicast, or unspecified")
    if not ip.is_private:
        raise ValueError("listener probe target must be an in-lab/private IPv4 address")
    return str(ip)


def _safe_http_probe_ip(value: str) -> str:
    ip = ipaddress.ip_address(str(value).strip())
    if ip.version != 4:
        raise ValueError("only IPv4 HTTP flow probes are supported")
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local:
        raise ValueError("HTTP flow probe target must not be loopback, link-local, multicast, or unspecified")
    return str(ip)


def _execute_source_probe_command(
    *,
    source_device: str,
    command: str,
    timeout_seconds: float,
    max_chars: int,
) -> tuple[Any | None, str, dict[str, Any] | None]:
    device = _resolve_inventory_device(source_device)
    if not device:
        return None, "", {"ok": False, "status": "fail", "error": f"Source device {source_device} not in local runner inventory."}
    platform = str(getattr(device, "platform", "") or "").lower()
    if "arista" not in platform and "eos" not in platform:
        return device, "", {"ok": False, "status": "fail", "error": "Source-side probes currently require an Arista/EOS source device."}
    try:
        from netmiko import ConnectHandler
    except Exception as exc:  # noqa: BLE001
        return device, "", {"ok": False, "status": "fail", "error": f"netmiko is required for source probes: {exc}"}

    conn = None
    started = time.monotonic()
    try:
        conn = ConnectHandler(
            device_type=_netmiko_device_type(device.platform),
            host=device.host,
            username=device.username,
            password=device.password,
            port=device.port,
            fast_cli=False,
            conn_timeout=max(5, int(timeout_seconds) + 5),
            auth_timeout=20,
            banner_timeout=20,
        )
        try:
            conn.enable()
        except Exception:
            pass
        try:
            output = conn.send_command(command, strip_prompt=False, strip_command=False, read_timeout=max(10, int(timeout_seconds) + 5))
        except Exception:
            output = conn.send_command_timing(command, strip_prompt=False, strip_command=False, read_timeout=max(10, int(timeout_seconds) + 5))
        return device, str(output or "")[:max_chars], None
    except Exception as exc:  # noqa: BLE001
        return device, "", {
            "ok": False,
            "status": "fail",
            "error": str(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def _execute_rez_server_listener_probe(payload: dict[str, Any]) -> dict[str, Any]:
    source_device = str(payload.get("source_device") or "").strip()
    src_ip = str(payload.get("src_ip") or "").strip()
    dst_ip = str(payload.get("dst_ip") or "").strip()
    try:
        dst_port = int(payload.get("dst_port") or 0)
    except Exception:
        dst_port = 0
    if not source_device or not dst_ip or dst_port < 1 or dst_port > 65535:
        return {"ok": False, "status": "fail", "error": "source_device, dst_ip, and dst_port are required"}
    try:
        safe_dst_ip = _safe_listener_probe_ip(dst_ip)
    except ValueError as exc:
        return {"ok": False, "status": "fail", "error": str(exc)}
    timeout_i = _probe_timeout_seconds(payload.get("timeout_seconds"), default=2.0, maximum=3.0)
    command = f"bash timeout {timeout_i} nc -vz {safe_dst_ip} {dst_port}"
    device, output, error = _execute_source_probe_command(
        source_device=source_device,
        command=command,
        timeout_seconds=timeout_i,
        max_chars=2000,
    )
    if error:
        return {
            **error,
            "source": "server_listener_probe",
            "probe_source": source_device,
            "source_matches_flow": True,
            "src_ip": src_ip,
            "dst_ip": safe_dst_ip,
            "dst_port": dst_port,
            "protocol": "tcp",
            "fresh": True,
            "server_reachable": False,
            "listener_present": None,
            "rootable": False,
            "runner_version": VERSION,
        }
    text = str(output or "")
    connected = "Connected to" in text
    refused = "Connection refused" in text
    timed_out = "timed out" in text.lower() or "Killed" in text
    return {
        "ok": True,
        "status": "pass",
        "source": "server_listener_probe",
        "probe_source": getattr(device, "id", source_device),
        "device": getattr(device, "id", source_device),
        "source_matches_flow": True,
        "src_ip": src_ip,
        "dst_ip": safe_dst_ip,
        "dst_port": dst_port,
        "protocol": "tcp",
        "fresh": True,
        "server_reachable": bool(connected or refused),
        "listener_present": True if connected else False if refused else None,
        "rootable": bool(refused),
        "reason": (
            "source_equivalent_tcp_refused"
            if refused
            else "source_equivalent_tcp_connected"
            if connected
            else "source_equivalent_tcp_timeout"
            if timed_out
            else "source_equivalent_tcp_unknown"
        ),
        "command": command,
        "output_preview": text[:1200],
        "runner_version": VERSION,
    }


def _execute_rez_http_flow_probe(payload: dict[str, Any]) -> dict[str, Any]:
    source_device = str(payload.get("source_device") or "").strip()
    src_ip = str(payload.get("src_ip") or "").strip()
    dst_ip = str(payload.get("dst_ip") or "").strip()
    try:
        dst_port = int(payload.get("dst_port") or 0)
    except Exception:
        dst_port = 0
    if not source_device or not dst_ip or dst_port not in {80, 8080}:
        return {"ok": False, "status": "fail", "error": "source_device, dst_ip, and HTTP dst_port are required"}
    try:
        safe_dst_ip = _safe_http_probe_ip(dst_ip)
    except ValueError as exc:
        return {"ok": False, "status": "fail", "error": str(exc)}
    timeout_i = _probe_timeout_seconds(payload.get("timeout_seconds"), default=3.0, maximum=5.0)
    url = f"http://{safe_dst_ip}/" if dst_port == 80 else f"http://{safe_dst_ip}:{dst_port}/"
    command = f"bash timeout {timeout_i} curl -v -m {timeout_i} {url} 2>&1 | head -120"
    device, output, error = _execute_source_probe_command(
        source_device=source_device,
        command=command,
        timeout_seconds=timeout_i,
        max_chars=4000,
    )
    if error:
        return {
            **error,
            "source": "observed_http_profile_verdict",
            "probe_source": source_device,
            "source_matches_flow": True,
            "src_ip": src_ip,
            "dst_ip": safe_dst_ip,
            "dst_port": dst_port,
            "protocol": "tcp",
            "fresh": True,
            "rootable": False,
            "runner_version": VERSION,
        }
    text = str(output or "")
    status = _http_status_from_probe_output(text)
    blocked = status in {403, 451}
    return {
        "ok": True,
        "status": "pass",
        "source": "observed_http_profile_verdict",
        "probe_source": getattr(device, "id", source_device),
        "device": getattr(device, "id", source_device),
        "source_matches_flow": True,
        "src_ip": src_ip,
        "dst_ip": safe_dst_ip,
        "dst_port": dst_port,
        "protocol": "tcp",
        "fresh": True,
        "rootable": bool(blocked),
        "root_atom": "FW_URL_FILTER_BLOCK" if blocked else None,
        "subtype": "url_filter_block",
        "action": "blocked" if blocked else "allowed" if status and status < 400 else "not_observed",
        "http_status": status,
        "reason": "source_equivalent_http_block_status" if blocked else "source_equivalent_http_no_block",
        "command": command,
        "output_preview": text[:1200],
        "runner_version": VERSION,
    }


def _execute_rez_api_get_state(payload: dict[str, Any]) -> dict[str, Any]:
    device_id = str(payload.get("device") or payload.get("device_id") or "").strip()
    if not device_id:
        return {"ok": False, "status": "fail", "error": "device_id is required"}
    raw_sections = payload.get("sections")
    sections = [str(item) for item in raw_sections] if isinstance(raw_sections, list) else None
    device, state, error = _collect_rez_runner_state(device_id)
    if error:
        return error
    assert device is not None and state is not None
    filtered, available = _filter_state_sections(state, sections, device)
    return {
        "ok": True,
        "status": "pass",
        "device": device.id,
        "platform": device.platform,
        "state": filtered,
        "available_sections": available,
        "runner_version": VERSION,
    }


def _execute_rez_api_query(payload: dict[str, Any]) -> dict[str, Any]:
    device_id = str(payload.get("device") or payload.get("device_id") or "").strip()
    category = str(payload.get("category") or "").strip().lower()
    if not device_id:
        return {"ok": False, "status": "fail", "error": "device_id is required"}
    if not category:
        return {"ok": False, "status": "fail", "device": device_id, "error": "category is required"}
    device, state, error = _collect_rez_runner_state(device_id)
    if error:
        return error
    assert device is not None and state is not None
    found = _extract_category(state, category)
    if not found:
        return {
            "ok": False,
            "status": "fail",
            "device": device.id,
            "platform": device.platform,
            "category": category,
            "error": f"Category {category!r} was not present in collected runner state.",
            "available_sections": sorted(k for k, v in state.items() if isinstance(v, (dict, list))),
        }
    source_key, data = found
    return {
        "ok": True,
        "status": "pass",
        "device": device.id,
        "platform": device.platform,
        "category": category,
        "source_section": source_key,
        "data": data,
        "runner_version": VERSION,
    }


def _execute_rez_refresh_targeted(payload: dict[str, Any]) -> dict[str, Any]:
    raw_devices = payload.get("devices")
    if not isinstance(raw_devices, list):
        return {"ok": False, "status": "fail", "error": "devices must be a list[str]"}
    device_ids = [str(item).strip() for item in raw_devices if str(item).strip()]
    if not device_ids:
        return {"ok": False, "status": "fail", "error": "devices must be a non-empty list[str]"}

    started = time.monotonic()
    refreshed_states: dict[str, dict[str, Any]] = {}
    refreshed: list[str] = []
    failed: list[list[str]] = []

    for device_id in device_ids:
        device, state, error = _collect_rez_runner_state(device_id)
        if error:
            failed.append([device_id, str(error.get("error") or "state collection failed")])
            continue
        assert device is not None and state is not None
        state = dict(state)
        state["_refreshed"] = True
        state["_collected_at"] = datetime.now(timezone.utc).isoformat()
        refreshed_states[device.id] = state
        refreshed.append(device.id)

    return {
        "ok": True,
        "status": "pass" if refreshed else "fail",
        "device_states": refreshed_states,
        "refreshed": refreshed,
        "failed": failed,
        "skipped": [],
        "elapsed_sec": round(time.monotonic() - started, 3),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runner_version": VERSION,
    }


def _execute_rez_scan_device(payload: dict[str, Any]) -> dict[str, Any]:
    from netcode.adapters.registry import AdapterRegistry
    from netcode.discovery import SSH_AUTODETECT_ORDER, _extract_state_summary, _safe_device_id
    from netcode.inventory import Device, Inventory
    from netcode.yamlio import write_yaml

    host = str(payload.get("host") or "").strip()
    if not host:
        return {"ok": False, "status": "fail", "error": "host is required"}

    device_id = str(payload.get("device_id") or "").strip()
    requested_platform = str(payload.get("platform") or "").strip()
    site = str(payload.get("site") or "").strip()
    groups_raw = payload.get("groups")
    groups = [str(group) for group in groups_raw] if isinstance(groups_raw, list) else []
    try:
        requested_port = int(payload.get("port") or 0)
    except Exception:
        requested_port = 0

    inventory = Inventory(INVENTORY_FILE)
    existing = inventory.find_device(device_id) if device_id else inventory.find_device(host)

    defaults = inventory.defaults
    username = existing.username if existing else str(defaults.get("username") or "")
    password = existing.password if existing else str(defaults.get("password") or "")
    port = requested_port or (existing.port if existing else int(defaults.get("port") or 22))

    rez = AdapterRegistry().rez
    driver_map = rez.driver_map()
    if not driver_map:
        return {"ok": False, "status": "fail", "host": host, "error": rez.summary().get("error") or "Rez drivers unavailable"}
    normalized_platform = rez.normalize_platform(requested_platform) or (existing.platform if existing else "")
    if normalized_platform and normalized_platform not in driver_map:
        return {
            "ok": False,
            "status": "fail",
            "host": host,
            "requested_platform": normalized_platform,
            "error": f"Rez has no driver for platform {normalized_platform}",
            "supported_platforms": sorted(driver_map.keys()),
        }
    if normalized_platform:
        platforms = [normalized_platform]
    else:
        ordered = [platform for platform in SSH_AUTODETECT_ORDER if platform in driver_map]
        platforms = ordered + sorted(set(driver_map) - set(ordered))

    attempts: list[dict[str, Any]] = []
    for platform in platforms:
        probe_id = device_id or (existing.id if existing else _safe_device_id(host))
        probe = Device(
            id=probe_id,
            hostname=device_id or (existing.hostname if existing else _safe_device_id(host)),
            host=host,
            platform=platform,
            username=username,
            password=password,
            port=port,
            site=site or (existing.site if existing else None),
            groups=tuple(groups or (list(existing.groups) if existing else [])),
        )
        result = rez.collect_device_state(probe)
        attempts.append(
            {
                "platform": platform,
                "ok": bool(result.get("ok")),
                "adapter": result.get("adapter"),
                "error": result.get("error"),
                "warnings": result.get("warnings", []),
                "collection_time": result.get("collection_time"),
            }
        )
        if not result.get("ok"):
            if normalized_platform:
                break
            continue
        state = result.get("state")
        if not isinstance(state, dict):
            return {"ok": False, "status": "fail", "host": host, "platform": platform, "error": "Rez driver returned non-object state."}
        state_summary = _extract_state_summary(state, device_id or host, platform)
        hostname = str(state_summary.get("hostname") or device_id or _safe_device_id(host))
        candidate = {
            "id": _safe_device_id(device_id or hostname),
            "hostname": hostname,
            "host": host,
            "platform": platform,
            "site": site or (existing.site if existing else "unassigned"),
            "groups": groups or (list(existing.groups) if existing else ["discovered"]),
            "port": port,
        }
        inventory_data = dict(inventory.raw or {})
        devices = list(inventory_data.get("devices") or [])
        persisted = dict(candidate)
        action_taken = "added"
        for index, raw_device in enumerate(devices):
            if not isinstance(raw_device, dict):
                continue
            if str(raw_device.get("id") or "") == str(candidate["id"]) or str(raw_device.get("host") or "") == host:
                # Preserve any device-specific secrets already stored on the runner.
                devices[index] = {**raw_device, **persisted}
                action_taken = "updated"
                break
        else:
            devices.append(persisted)
        inventory_data["devices"] = devices
        write_yaml(INVENTORY_FILE, inventory_data)
        return {
            "ok": True,
            "status": "pass",
            "found": True,
            "provider": "rez-runner",
            "host": host,
            "platform": platform,
            "adapter": result.get("adapter"),
            "driver": result.get("driver"),
            "existing_device_id": existing.id if existing else None,
            "state": state,
            "state_summary": state_summary,
            "source_of_truth_candidate": candidate,
            "runner_inventory": {
                "action": action_taken,
                "device": candidate,
                "inventory": str(INVENTORY_FILE),
            },
            "tried_platforms": attempts,
            "supported_platforms": sorted(driver_map.keys()),
            "warnings": result.get("warnings", []),
            "errors": result.get("errors", []),
            "safety": {
                "device_writes": "none",
                "source_of_truth_written": False,
                "message": "Discovery used runner-local Rez read/state collection only. Review before importing.",
            },
            "runner_version": VERSION,
        }

    return {
        "ok": False,
        "status": "fail",
        "found": False,
        "host": host,
        "provider": "rez-runner",
        "requested_platform": normalized_platform or "auto",
        "tried_platforms": attempts,
        "supported_platforms": sorted(driver_map.keys()),
        "safety": {
            "device_writes": "none",
            "source_of_truth_written": False,
            "message": "Discovery failed or the device did not accept the tried Rez driver.",
        },
    }


def _read_deadline_seconds(action: str, payload: dict[str, Any]) -> float:
    if action == "readiness":
        return float(READINESS_TIMEOUT_SECONDS)
    if action == "rez_refresh_targeted":
        try:
            requested = float(payload.get("_runner_timeout_seconds") or READINESS_TIMEOUT_SECONDS)
        except Exception:
            requested = float(READINESS_TIMEOUT_SECONDS)
        return max(1.0, min(requested, float(MAX_READ_TIMEOUT_SECONDS)))
    if action == "rez_scan_device":
        try:
            requested = float(payload.get("_runner_timeout_seconds") or READINESS_TIMEOUT_SECONDS)
        except Exception:
            requested = float(READINESS_TIMEOUT_SECONDS)
        return max(1.0, min(requested, float(MAX_READ_TIMEOUT_SECONDS)))
    return float(READ_TIMEOUT_SECONDS)


def _execute_read(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed wrapper: a hung device read must never wedge the runner's
    sequential job loop (a dead container / unreachable device can otherwise
    block every later job and stop heartbeats). Reads get a hard deadline."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    deadline = _read_deadline_seconds(action, payload)
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

    if action == "rez_ssh_command":
        return _execute_rez_ssh_command(payload)

    if action == "rez_api_get_state":
        return _execute_rez_api_get_state(payload)

    if action == "rez_api_query":
        return _execute_rez_api_query(payload)

    if action == "rez_refresh_targeted":
        return _execute_rez_refresh_targeted(payload)

    if action == "rez_scan_device":
        return _execute_rez_scan_device(payload)

    if action == "rez_server_listener_probe":
        return _execute_rez_server_listener_probe(payload)

    if action == "rez_http_flow_probe":
        return _execute_rez_http_flow_probe(payload)

    if action == "verify":
        from netcode.lab import AristaEOSLabAdapter
        wd = Path(tempfile.mkdtemp(prefix="netcode-read-"))
        ip = wd / "intent.yaml"
        ip.write_text(payload.get("intent_yaml", ""), encoding="utf-8")
        intent = load_intent(ip)
        inv = Inventory(INVENTORY_FILE)
        device_id = payload.get("device_id") or (intent.targets.device_ids[0] if intent.targets.device_ids else "")
        device = inv.find_device(str(device_id or ""))
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
        device = inv.find_device(str(payload.get("device_id") or ""))
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
        device = inv.find_device(str(payload.get("device_id") or ""))
        if not device:
            return {"ok": False, "error": f"Device {payload.get('device_id')} not in runner inventory."}
        state = AdapterRegistry().rez.collect_device_state(device)
        return device_drift_from_state(payload.get("expected") or [], state, str(payload.get("device_id", "")))

    if action == "troubleshoot":
        from netcode.adapters.registry import AdapterRegistry
        from netcode.troubleshooting import troubleshoot_state
        inv = Inventory(INVENTORY_FILE)
        device = inv.find_device(str(payload.get("device_id") or ""))
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
        device = inv.find_device(str(payload.get("device_id") or ""))
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
            device = Inventory(INVENTORY_FILE).find_device(str(frame.get("device_id") or ""))
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
    p_import = sub.add_parser("inventory-import", help="Install credentialed device inventory on this runner.")
    p_import.add_argument("file", help="Path to inventory YAML with local device credentials.")
    p_import.set_defaults(func=import_inventory)
    p_run = sub.add_parser("run", help="Poll for and execute jobs.")
    p_run.set_defaults(func=run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
