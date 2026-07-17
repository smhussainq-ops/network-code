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
import platform
import re
import shutil
import signal
import socket
import subprocess
import site
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

IDENTITY_DIR = Path(os.getenv("NETCODE_RUNNER_HOME") or (Path.home() / ".netcode-runner")).expanduser()
_WINDOWS_DPAPI = os.name == "nt" and os.getenv("NETCODE_DISABLE_DPAPI", "").strip() != "1"
IDENTITY_FILE = IDENTITY_DIR / ("identity.dpapi" if _WINDOWS_DPAPI else "identity.json")
INVENTORY_FILE = IDENTITY_DIR / ("inventory.dpapi" if _WINDOWS_DPAPI else "inventory.yaml")
POLICY_FILE = IDENTITY_DIR / "policy.yaml"
MANAGER_LEDGER_FILE = IDENTITY_DIR / "manager-operations.json"
OPERATION_LEDGER_FILE = IDENTITY_DIR / "device-operations.db"
VERSION = "0.7.0-token-lifecycle"
COMMUNITY_MAX_DEVICES = 25

_stop = False
_SHELL_ADAPTERS: dict[str, dict[str, Any]] = {}
_SHELL_ADAPTER_LOCK = threading.Lock()
_SHELL_ADAPTER_IDLE_SECONDS = 300.0


def _handle_sigterm(signum, frame):  # noqa: ANN001
    global _stop
    _stop = True
    print("\n[runner] SIGTERM received — will exit after the current job drains.", flush=True)


def _shell_adapter_key(payload: dict[str, Any], device_id: str) -> str:
    session_id = str(payload.get("session_id") or "").strip()
    return session_id or f"device:{device_id}"


def _shell_adapter_for(key: str, device):  # noqa: ANN001
    """Return a persistent CLI adapter for one REST shell session.

    The browser sends `/api/shell/input` one line at a time. Reusing the same
    adapter is what preserves device CLI mode across `conf t`, `interface ...`,
    and later lines while keeping concurrent sessions isolated by session id.
    """
    from netcode.adapters.shell import NetmikoShellAdapter

    now = time.monotonic()
    with _SHELL_ADAPTER_LOCK:
        for existing_key, entry in list(_SHELL_ADAPTERS.items()):
            if now - float(entry.get("last_used") or 0.0) <= _SHELL_ADAPTER_IDLE_SECONDS:
                continue
            adapter = entry.get("adapter")
            try:
                if adapter is not None:
                    adapter.disconnect()
            except Exception:
                pass
            _SHELL_ADAPTERS.pop(existing_key, None)

        entry = _SHELL_ADAPTERS.get(key)
        if entry and str(entry.get("device_id")) == str(device.id):
            entry["last_used"] = now
            return entry["adapter"]

        if entry:
            try:
                entry.get("adapter").disconnect()
            except Exception:
                pass

        adapter = NetmikoShellAdapter(device)
        adapter.connect()
        _SHELL_ADAPTERS[key] = {"adapter": adapter, "device_id": str(device.id), "last_used": now}
        return adapter


def _shell_adapter_drop(key: str) -> None:
    with _SHELL_ADAPTER_LOCK:
        entry = _SHELL_ADAPTERS.pop(key, None)
    adapter = (entry or {}).get("adapter")
    if adapter is not None:
        try:
            adapter.disconnect()
        except Exception:
            pass


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


def _get(server: str, path: str, timeout: float = 10.0) -> dict[str, Any]:
    url = server.rstrip("/") + path
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
        "token_expires_at": resp.get("token_expires_at"),
        "token_rotate_after": resp.get("token_rotate_after"),
        "token_pending": False,
    }
    _write_identity(identity)
    print(f"[runner] Enrolled '{args.name}' into pool '{resp['pool']}'. Identity saved to {IDENTITY_FILE}")
    if not INVENTORY_FILE.exists():
        print("[runner] Next: open the Local Connector control application and run bounded discovery.")
    return 0


def _write_identity(identity: dict[str, Any]) -> None:
    """Atomically replace the machine identity; a failed write leaves the old token usable."""
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(identity, indent=2).encode("utf-8")
    if IDENTITY_FILE.suffix.lower() == ".dpapi":
        from netcode.windows_security import protect_machine

        serialized = protect_machine(serialized)
    temporary = IDENTITY_FILE.with_name(f".{IDENTITY_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(serialized)
        if IDENTITY_FILE.suffix.lower() != ".dpapi":
            temporary.chmod(0o600)
        os.replace(temporary, IDENTITY_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def _load_identity() -> dict[str, Any]:
    if not IDENTITY_FILE.exists():
        raise SystemExit(f"[runner] Not enrolled. Run: netcode-runner enroll --server ... --join-token ...")
    serialized = IDENTITY_FILE.read_bytes()
    if IDENTITY_FILE.suffix.lower() == ".dpapi":
        from netcode.windows_security import unprotect_machine

        serialized = unprotect_machine(serialized)
    return json.loads(serialized.decode("utf-8"))


def _identity_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _token_rotation_due(identity: dict[str, Any], *, now: datetime | None = None) -> bool:
    if bool(identity.get("token_pending")):
        return True
    rotate_after = _identity_time(identity.get("token_rotate_after"))
    return rotate_after is None or rotate_after <= (now or datetime.now(timezone.utc))


def _pending_token_rejected(exc: Exception) -> bool:
    message = str(exc)
    return "HTTP 401" in message or "HTTP 403" in message


def _confirm_pending_identity(identity: dict[str, Any]) -> dict[str, Any]:
    token = str(identity.get("runner_token") or "")
    try:
        response = _post(identity["server"], "/api/runner/token/confirm", {}, token=token)
    except Exception as exc:
        fallback = str(identity.get("rotation_fallback_token") or "")
        if not fallback or not _pending_token_rejected(exc):
            raise
        restored = dict(identity)
        restored["runner_token"] = fallback
        restored["token_pending"] = False
        restored.pop("rotation_fallback_token", None)
        restored.pop("pending_token_valid_until", None)
        _write_identity(restored)
        return restored
    confirmed = dict(identity)
    confirmed["token_pending"] = False
    confirmed["token_expires_at"] = response.get("token_expires_at")
    confirmed["token_rotate_after"] = response.get("token_rotate_after")
    confirmed.pop("rotation_fallback_token", None)
    confirmed.pop("pending_token_valid_until", None)
    try:
        _write_identity(confirmed)
    except Exception:  # noqa: BLE001 - current server token remains the pending local token.
        # The pending token has already become current server-side. Keep using it;
        # idempotent confirmation will repair the local marker on the next pass.
        return identity
    return confirmed


def _maintain_runner_token(identity: dict[str, Any]) -> dict[str, Any]:
    if bool(identity.get("token_pending")):
        return _confirm_pending_identity(identity)
    if not _token_rotation_due(identity):
        return identity
    old_token = str(identity.get("runner_token") or "")
    response = _post(identity["server"], "/api/runner/token/rotate", {}, token=old_token)
    pending = dict(identity)
    pending["runner_token"] = response["runner_token"]
    pending["rotation_fallback_token"] = old_token
    pending["token_expires_at"] = response.get("token_expires_at")
    pending["token_rotate_after"] = response.get("token_rotate_after")
    pending["pending_token_valid_until"] = response.get("pending_token_valid_until")
    pending["token_pending"] = True
    _write_identity(pending)
    try:
        return _confirm_pending_identity(pending)
    except Exception:  # noqa: BLE001 - pending token remains valid and confirmation is retried.
        return pending


def import_inventory(args: argparse.Namespace) -> int:
    """Install a credentialed device inventory on the local runner only."""
    from netcode.yamlio import read_yaml, write_yaml

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
    write_yaml(INVENTORY_FILE, data)
    if INVENTORY_FILE.suffix.lower() != ".dpapi":
        INVENTORY_FILE.chmod(0o600)
    print(f"[runner] Imported {len(devices)} device(s) into {INVENTORY_FILE}")
    print("[runner] Credentials stay on this runner. They are not sent to the control plane.")
    return 0


def _atomic_write_inventory(path: Path, data: dict[str, Any]) -> None:
    """Replace an inventory without ever writing a plaintext DPAPI temporary."""
    from netcode.yamlio import write_yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")
    try:
        write_yaml(temporary, data)
        if temporary.suffix.lower() != ".dpapi":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap_discovered_inventory(
    payload: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build protected local inventory exclusively from successful discovery.

    The credential context is written to an encrypted temporary inventory so a
    failed or partial collector cannot damage the active inventory. Device facts
    come from Rez collection results; credentials remain runner-local.
    """
    from netcode.yamlio import read_yaml, write_yaml

    seed = str(payload.get("seed_node") or payload.get("seeds") or payload.get("host") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    site_name = str(payload.get("site") or "unassigned").strip() or "unassigned"
    try:
        max_devices = int(payload.get("max_devices") or COMMUNITY_MAX_DEVICES)
        port = int(payload.get("port") or 22)
    except (TypeError, ValueError):
        return {"ok": False, "status": "fail", "error": "Discovery limits and port must be integers."}
    if not seed:
        return {"ok": False, "status": "fail", "error": "At least one discovery seed is required."}
    if not username or not password:
        return {"ok": False, "status": "fail", "error": "A local device username and password are required."}
    if not 1 <= max_devices <= COMMUNITY_MAX_DEVICES:
        return {
            "ok": False,
            "status": "fail",
            "error": f"Community discovery is limited to {COMMUNITY_MAX_DEVICES} devices.",
            "limit": COMMUNITY_MAX_DEVICES,
        }
    if not 1 <= port <= 65535:
        return {"ok": False, "status": "fail", "error": "Discovery port must be between 1 and 65535."}

    merge = bool(payload.get("merge", True))
    existing_data: dict[str, Any] = {"defaults": {}, "devices": []}
    if INVENTORY_FILE.exists():
        try:
            existing_data = read_yaml(INVENTORY_FILE)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "fail", "error": f"Existing inventory cannot be read: {exc}"}

    temporary = INVENTORY_FILE.with_name(
        f".discovery-{uuid.uuid4().hex}{INVENTORY_FILE.suffix}"
    )
    bootstrap_defaults: dict[str, Any] = {
        "username": username,
        "password": password,
        "port": port,
    }
    requested_platform = str(payload.get("platform") or "").strip()
    if requested_platform:
        bootstrap_defaults["platform"] = requested_platform
    try:
        write_yaml(temporary, {"defaults": bootstrap_defaults, "devices": []})
        if temporary.suffix.lower() != ".dpapi":
            temporary.chmod(0o600)
        discovery_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"username", "password", "merge"}
        }
        discovery_payload["seed_node"] = seed
        discovery_payload["site"] = site_name
        discovery_payload["port"] = port
        discovery_payload["max_devices"] = max_devices
        result = _execute_rez_discover_network(
            discovery_payload,
            progress,
            inventory_path=temporary,
        )
        if not result.get("ok"):
            return {
                **result,
                "inventory": {
                    "written": False,
                    "preserved": INVENTORY_FILE.exists(),
                },
            }

        raw_candidates = result.get("source_of_truth_candidates") or []
        candidates: list[dict[str, Any]] = []
        public_fields = {
            "id", "hostname", "host", "platform", "site", "groups", "port",
            "serial", "aliases", "role", "building", "floor", "closet", "location",
            "management", "connection",
        }
        for value in raw_candidates:
            if not isinstance(value, dict):
                continue
            candidate = {key: value[key] for key in public_fields if key in value}
            if not all(str(candidate.get(key) or "").strip() for key in ("id", "host", "platform")):
                continue
            candidate["site"] = str(candidate.get("site") or site_name)
            groups = candidate.get("groups") or ["discovered"]
            candidate["groups"] = [str(group) for group in groups]
            candidate["username"] = username
            candidate["password"] = password
            candidate["port"] = int(candidate.get("port") or port)
            candidates.append(candidate)
        if not candidates:
            return {
                "ok": False,
                "status": "fail",
                "error": "Discovery returned no valid device records.",
                "inventory": {"written": False, "preserved": INVENTORY_FILE.exists()},
            }

        devices = list(existing_data.get("devices") or []) if merge else []
        added = 0
        updated = 0
        for candidate in candidates:
            for index, current in enumerate(devices):
                if not isinstance(current, dict):
                    continue
                same_id = str(current.get("id") or "").strip().lower() == str(candidate["id"]).strip().lower()
                same_host = str(current.get("host") or "").strip() == str(candidate["host"]).strip()
                if same_id or same_host:
                    devices[index] = {**current, **candidate}
                    updated += 1
                    break
            else:
                devices.append(candidate)
                added += 1
        if len(devices) > COMMUNITY_MAX_DEVICES:
            return {
                "ok": False,
                "status": "fail",
                "error": (
                    f"The merged inventory would contain {len(devices)} devices; "
                    f"Community is limited to {COMMUNITY_MAX_DEVICES}."
                ),
                "limit": COMMUNITY_MAX_DEVICES,
                "inventory": {"written": False, "preserved": INVENTORY_FILE.exists()},
            }

        defaults = dict(existing_data.get("defaults") or {}) if merge else {}
        defaults.pop("username", None)
        defaults.pop("password", None)
        defaults["port"] = port
        inventory_data = {**(existing_data if merge else {}), "defaults": defaults, "devices": devices}
        _atomic_write_inventory(INVENTORY_FILE, inventory_data)
        public_candidates = [
            {key: value for key, value in candidate.items() if key not in {"username", "password"}}
            for candidate in candidates
        ]
        return {
            **result,
            "source_of_truth_candidates": public_candidates,
            "inventory": {
                "written": True,
                "path": str(INVENTORY_FILE),
                "device_count": len(devices),
                "discovered": len(candidates),
                "added": added,
                "updated": updated,
                "mode": "merge" if merge else "replace",
                "protected": INVENTORY_FILE.suffix.lower() == ".dpapi",
            },
            "safety": {
                **dict(result.get("safety") or {}),
                "inventory_source": "successful_local_discovery",
                "inventory_written": True,
                "credentials_returned": False,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "fail",
            "error": f"Discovery bootstrap failed: {type(exc).__name__}: {exc}",
            "inventory": {"written": False, "preserved": INVENTORY_FILE.exists()},
        }
    finally:
        temporary.unlink(missing_ok=True)


def discover_inventory(args: argparse.Namespace) -> int:
    """Interactive recovery command for the same discovery flow used by the UI."""
    import getpass

    password = getpass.getpass("Device password (stored locally with DPAPI): ")
    result = bootstrap_discovered_inventory({
        "seed_node": args.seeds,
        "allowed_cidrs": args.allowed_cidrs,
        "excluded_cidrs": args.excluded_cidrs,
        "site": args.site,
        "platform": args.platform,
        "port": args.port,
        "username": args.username,
        "password": password,
        "depth": args.depth,
        "max_devices": args.max_devices,
        "concurrency": args.concurrency,
        "merge": not args.replace,
    })
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def _rez_runtime_check() -> dict[str, str]:
    """Load the bundled Rez driver registry without opening a device session."""
    try:
        from netcode.adapters.rez import RezAdapterBridge

        health = RezAdapterBridge().health()
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "rez_runtime",
            "status": "fail",
            "message": f"Rez driver runtime could not load: {type(exc).__name__}: {exc}",
        }

    if health.get("ok"):
        platform_count = int(health.get("platform_count") or 0)
        return {
            "id": "rez_runtime",
            "status": "pass",
            "message": f"Rez driver runtime loaded {platform_count} platform adapter(s).",
        }

    error = str(health.get("error") or "driver registry is unavailable")
    return {
        "id": "rez_runtime",
        "status": "fail",
        "message": f"Rez driver runtime could not load: {error[:500]}",
    }


def _governed_template_check() -> dict[str, str]:
    """Confirm the standalone runtime can render its supported governed jobs."""
    template_root = _runner_workspace_root() / "templates"
    required = (
        template_root / "arista" / "ntp_standardize.j2",
        template_root / "cisco_ios" / "ntp_standardize.j2",
    )
    missing = [str(path.relative_to(template_root)) for path in required if not path.is_file()]
    if missing:
        return {
            "id": "governed_templates",
            "status": "fail",
            "message": f"Governed execution template(s) are missing: {', '.join(missing)}.",
        }
    return {
        "id": "governed_templates",
        "status": "pass",
        "message": "Governed Arista and Cisco NTP templates are available locally.",
    }


def doctor(args: argparse.Namespace) -> int:
    """Report Local Connector readiness without revealing local credentials."""
    checks: list[dict[str, Any]] = []
    identity: dict[str, Any] = {}
    if IDENTITY_FILE.exists():
        try:
            identity = _load_identity()
            checks.append({"id": "identity", "status": "pass", "message": "Connector is enrolled."})
            expiry = _identity_time(identity.get("token_expires_at"))
            now = datetime.now(timezone.utc)
            if bool(identity.get("token_pending")):
                checks.append({
                    "id": "connector_token",
                    "status": "warn",
                    "message": "Connector credential rotation is awaiting server confirmation.",
                })
            elif expiry is not None and expiry <= now:
                checks.append({
                    "id": "connector_token",
                    "status": "fail",
                    "message": "Connector credential expired; re-enrollment is required.",
                })
            else:
                checks.append({
                    "id": "connector_token",
                    "status": "pass",
                    "message": (
                        f"Connector credential is active through {expiry.isoformat()}."
                        if expiry is not None
                        else "Legacy connector credential is active and will rotate on service start."
                    ),
                })
        except Exception as exc:  # noqa: BLE001
            checks.append({"id": "identity", "status": "fail", "message": f"Identity cannot be read: {exc}"})
    else:
        checks.append({"id": "identity", "status": "fail", "message": "Connector is not enrolled."})

    inventory_summary: dict[str, Any] = {"configured": False, "device_count": 0, "sites": [], "platforms": []}
    if INVENTORY_FILE.exists():
        try:
            from netcode.inventory import Inventory

            inventory = Inventory(INVENTORY_FILE)
            inventory_summary = {
                "configured": True,
                "device_count": len(inventory.devices),
                "sites": sorted({str(device.site) for device in inventory.devices if device.site}),
                "platforms": sorted({device.platform for device in inventory.devices}),
            }
            status = "pass" if inventory.devices else "fail"
            checks.append({
                "id": "inventory",
                "status": status,
                "message": f"{len(inventory.devices)} device record(s) are available locally.",
            })
        except Exception as exc:  # noqa: BLE001
            checks.append({"id": "inventory", "status": "fail", "message": f"Inventory cannot be read: {exc}"})
    else:
        checks.append({"id": "inventory", "status": "fail", "message": "No local device inventory is installed."})

    checks.append(_rez_runtime_check())
    checks.append(_governed_template_check())

    server = str(identity.get("server") or "")
    if server:
        try:
            manifest = _get(server, "/api/runner/download/windows/manifest", timeout=float(args.timeout))
            reachable = bool(manifest.get("ok"))
            checks.append({
                "id": "control_plane",
                "status": "pass" if reachable else "fail",
                "message": "Control plane is reachable." if reachable else "Control plane returned an invalid manifest.",
            })
        except Exception as exc:  # noqa: BLE001
            checks.append({"id": "control_plane", "status": "fail", "message": f"Control plane is unreachable: {exc}"})
    else:
        checks.append({"id": "control_plane", "status": "fail", "message": "No enrolled control-plane URL is available."})

    failed = [check for check in checks if check["status"] == "fail"]
    payload = {
        "ok": not failed,
        "status": "pass" if not failed else "fail",
        "connector_version": VERSION,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "home": str(IDENTITY_DIR),
        "security": {
            "dpapi_machine_scope": IDENTITY_FILE.suffix.lower() == ".dpapi",
            "credentials_returned": False,
            "inbound_listener": False,
        },
        "inventory": inventory_summary,
        "checks": checks,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def _public_inventory_snapshot() -> dict[str, Any]:
    """Return only searchable device metadata; credentials never enter this payload."""
    if not INVENTORY_FILE.exists():
        devices: list[dict[str, Any]] = []
    else:
        from netcode.inventory import Inventory

        devices = [
            {
                "id": device.id,
                "hostname": device.hostname,
                "host": device.host,
                "port": device.port,
                "platform": device.platform,
                "site": device.site or "",
                "role": device.role or "",
                "groups": list(device.groups),
                "aliases": list(device.aliases),
                "serial": device.serial,
                "building": device.building or "",
                "floor": device.floor or "",
                "closet": device.closet or "",
                "location": dict(device.location),
                "management": dict(device.management),
            }
            for device in Inventory(INVENTORY_FILE).devices
        ]
    serialized = json.dumps(devices, sort_keys=True, separators=(",", ":"))
    return {
        "revision": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "devices": devices,
        "replace": True,
    }


def _sync_inventory_catalog(server: str, token: str, previous_revision: str = "") -> str:
    snapshot = _public_inventory_snapshot()
    revision = str(snapshot["revision"])
    if revision == previous_revision:
        return previous_revision
    response = _post(server, "/api/runner/inventory-sync", snapshot, token=token, timeout=120)
    if not response or not response.get("ok"):
        raise RuntimeError((response or {}).get("message") or "inventory catalog sync failed")
    print(f"[runner] Synchronized {response.get('device_count', 0)} public device record(s).", flush=True)
    if response.get("conflicts"):
        print(f"[runner] Catalog conflicts require review: {len(response['conflicts'])}.", file=sys.stderr, flush=True)
    return revision


def _progress_reporter(
    server: str,
    token: str,
    secret: str,
    job: dict[str, Any],
) -> Callable[[dict[str, Any]], None] | None:
    job_id = str(job.get("id") or "")
    lease_token = str(job.get("lease_token") or "")
    job_action = str(job.get("action") or "").strip().lower()
    if job_action.startswith("lab_"):
        phase = job_action.removeprefix("lab_")
    elif job_action == "read_verify":
        phase = "verify"
    elif job_action == "read_rez_discover_network":
        phase = "discovery"
    else:
        return None
    payload = job.get("payload") or {}
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    device_id = str(payload.get("device_id") or device.get("id") or "")
    sequence = 1

    def report(raw: dict[str, Any]) -> None:
        nonlocal sequence
        sequence += 1
        event = {
            **raw,
            "event_id": str(uuid.uuid4()),
            "sequence": sequence,
            "phase": phase,
            "device_id": str(raw.get("device_id") or device_id),
        }
        signature = hmac.new(
            secret.encode("utf-8"),
            _canonical(event).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        try:
            _post(
                server,
                f"/api/runner/jobs/{job_id}/progress",
                {"event": event, "signature": signature, "lease_token": lease_token},
                token=token,
                timeout=8,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] Progress frame {job_id}:{sequence} was not delivered: {exc}", file=sys.stderr)

    return report


class _JobLeaseRenewer:
    """Keep one claimed job owned while a blocking device operation runs."""

    def __init__(self, server: str, runner_token: str, job: dict[str, Any]):
        self.server = server
        self.runner_token = runner_token
        self.job_id = str(job.get("id") or "")
        self.lease_token = str(job.get("lease_token") or "")
        try:
            lease_seconds = int(job.get("lease_seconds") or 90)
        except (TypeError, ValueError):
            lease_seconds = 90
        self.interval = max(5.0, min(30.0, lease_seconds / 3.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def valid(self) -> bool:
        return bool(self.job_id and self.lease_token)

    def start(self) -> None:
        if not self.valid:
            return
        self._thread = threading.Thread(target=self._run, name=f"job-lease-{self.job_id[:8]}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                _post(
                    self.server,
                    f"/api/runner/jobs/{self.job_id}/lease",
                    {"lease_token": self.lease_token},
                    token=self.runner_token,
                    timeout=8,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[runner] Job lease renewal failed for {self.job_id}: {exc}", file=sys.stderr, flush=True)


def _runner_workspace_root() -> Path:
    configured = str(os.environ.get("NETCODE_RUNNER_WORKSPACE", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _execute_job_inner(
    job: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one lab job locally: re-validate (fail-closed), resolve local creds, execute via the shared adapter."""
    # Imports are local so `enroll` works even without the full netcode package installed.
    import tempfile
    from netcode.inventory import Inventory
    from netcode.lab import AristaEOSLabAdapter, run_lab_action_for_device
    from netcode.models import load_intent
    from netcode.rendering import render_intent
    from netcode.runner_checks import local_policy_gate

    payload = job.get("payload") or {}
    job_action = str(job.get("action") or "")
    if job_action.startswith("read_"):
        return _execute_read(job_action[len("read_"):], payload, progress=progress)
    if job_action.startswith("manager_"):
        from netcode.manager_execution import execute_manager_job

        manager_payload = dict(payload)
        manager_payload["action"] = job_action.removeprefix("manager_")
        return execute_manager_job(
            manager_payload,
            inventory_path=INVENTORY_FILE,
            ledger_path=MANAGER_LEDGER_FILE,
        )
    action = payload.get("action")
    if action == "ansible_pack":
        return _execute_ansible_pack(payload)
    device_spec = payload.get("device") or {}
    device_id = device_spec.get("id")

    # Render workspace: the runner uses ITS OWN templates (never the control
    # plane's rendered output) so it fully controls what gets pushed. Resolve
    # relative to the installed runner, not the shell's launch directory.
    ws_root = _runner_workspace_root()
    workdir = Path(tempfile.mkdtemp(prefix="netcode-runner-"))
    intent_path = workdir / "intent.yaml"
    intent_path.write_text(payload.get("intent_yaml", ""), encoding="utf-8")
    intent = load_intent(intent_path)

    # Credentials and the authoritative platform come only from the connector's
    # inventory. Resolve them before rendering so a cloud payload cannot select
    # a different vendor template.
    if not INVENTORY_FILE.exists():
        return {"status": "fail", "action": action, "device_id": device_id,
                "message": f"No local inventory at {INVENTORY_FILE}; cannot resolve credentials."}
    inventory = Inventory(INVENTORY_FILE)
    device = inventory.find_device(device_id)
    if device is None:
        return {"status": "fail", "action": action, "device_id": device_id,
                "message": f"Device {device_id} not in local runner inventory."}
    render = render_intent(intent, _RunnerPaths(ws_root), platform=device.platform)

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
    if progress:
        progress({
            "stage": "policy_gate_passed",
            "status": "running",
            "message": "Runner-local policy and credential safety checks passed.",
        })

    if action not in {"dry-run", "apply", "rollback"}:
        return {"status": "fail", "action": action, "device_id": device_id, "message": f"Unknown action {action}."}
    lab = run_lab_action_for_device(
        device,
        intent,
        render,
        action,
        progress=progress,
        operation_id=str(job.get("idempotency_key") or ""),
        operation_context={
            "approved_pre_change_state": payload.get("approved_pre_change_state"),
            "rollback_state": payload.get("rollback_state"),
        },
    )
    result = lab.__dict__ if hasattr(lab, "__dict__") else dict(lab)
    result.setdefault("action", action)
    result.setdefault("device_id", device_id)
    result["runner_version"] = VERSION
    return result


def _execute_job(
    job: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute a claimed job with runner-local replay protection."""
    job_action = str(job.get("action") or "").strip().lower()
    if job_action.startswith("read_") or job_action.startswith("manager_"):
        return _execute_job_inner(job, progress=progress)
    if not (job_action.startswith("lab_") or job_action.startswith("ansible_")):
        return _execute_job_inner(job, progress=progress)

    operation_key = str(job.get("idempotency_key") or "").strip()
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    device_id = str(job.get("device_id") or payload.get("device_id") or device.get("id") or "").strip().lower()
    change_id = str(job.get("change_id") or payload.get("change_id") or "").strip()
    if not operation_key:
        return {
            "status": "fail",
            "action": str(payload.get("action") or job_action),
            "device_id": device_id,
            "message": "Runner refused a device operation without a durable idempotency key.",
            "error": "missing_operation_key",
        }

    request = {
        "job_action": job_action,
        "change_id": change_id,
        "device_id": device_id,
        "payload": payload,
    }
    from netcode.operation_ledger import RunnerOperationLedger

    ledger = RunnerOperationLedger(OPERATION_LEDGER_FILE)
    try:
        decision = ledger.begin(
            operation_key,
            request,
            action=job_action,
            change_id=change_id,
            device_id=device_id,
        )
    except ValueError as exc:
        return {
            "status": "fail",
            "action": str(payload.get("action") or job_action),
            "device_id": device_id,
            "message": str(exc),
            "error": "operation_key_conflict",
        }
    if decision.mode == "replay":
        return dict(decision.result or {})
    if decision.mode == "reconcile_required":
        return {
            "status": "reconcile_required",
            "action": str(payload.get("action") or job_action),
            "device_id": device_id,
            "message": "A prior attempt did not record a terminal result; inspect live state before any retry.",
            "operation_key": operation_key,
        }

    try:
        result = _execute_job_inner(job, progress=progress)
    except Exception as exc:  # noqa: BLE001 - an interrupted write has an uncertain outcome.
        result = {
            "status": "reconcile_required",
            "action": str(payload.get("action") or job_action),
            "device_id": device_id,
            "message": f"Device operation ended without a proven outcome: {type(exc).__name__}: {exc}",
            "operation_key": operation_key,
        }
    try:
        ledger.complete(operation_key, result)
    except Exception as exc:  # noqa: BLE001 - successful device work without a ledger commit is uncertain.
        return {
            "status": "reconcile_required",
            "action": str(payload.get("action") or job_action),
            "device_id": device_id,
            "message": f"Device outcome exists but its local terminal record could not be persisted: {type(exc).__name__}: {exc}",
            "operation_key": operation_key,
        }
    return result


def _runner_ws():
    return _RunnerPaths(Path(os.environ.get("NETCODE_RUNNER_WORKSPACE", "") or Path.cwd()).resolve())


def _ansible_network_vars(platform: str) -> dict[str, Any]:
    normalized = _netmiko_device_type(platform)
    mapping = {
        "arista_eos": {"ansible_connection": "network_cli", "ansible_network_os": "arista.eos.eos"},
        "cisco_ios": {"ansible_connection": "network_cli", "ansible_network_os": "cisco.ios.ios"},
        "cisco_xe": {"ansible_connection": "network_cli", "ansible_network_os": "cisco.ios.ios"},
        "cisco_nxos": {"ansible_connection": "network_cli", "ansible_network_os": "cisco.nxos.nxos"},
        "juniper_junos": {"ansible_connection": "netconf", "ansible_network_os": "junipernetworks.junos.junos"},
        "fortinet": {"ansible_connection": "httpapi", "ansible_network_os": "fortinet.fortios.fortios"},
        "paloalto_panos": {"ansible_connection": "local"},
    }
    return mapping.get(normalized, {"ansible_connection": "network_cli", "ansible_network_os": normalized})


def _write_ansible_inventory(devices: list[Any], destination: Path) -> None:
    from netcode.yamlio import write_yaml

    hosts: dict[str, dict[str, Any]] = {}
    strict_host_keys = _ansible_host_key_checking_enabled()
    for device in devices:
        host_vars = {
            "ansible_host": device.host,
            "ansible_port": int(device.port),
            "ansible_user": device.username,
            "ansible_password": device.password,
            **_ansible_network_vars(device.platform),
        }
        if not strict_host_keys:
            # Explicit lab-only override. Production omits these variables and
            # retains Ansible's strict host-key defaults.
            host_vars.update(
                {
                    "ansible_host_key_checking": False,
                    "ansible_ssh_host_key_checking": False,
                }
            )
        hosts[device.id] = {key: value for key, value in host_vars.items() if value not in (None, "")}
    write_yaml(destination, {"all": {"hosts": hosts}})
    destination.chmod(0o600)


def _ansible_host_key_checking_enabled() -> bool:
    """Keep production strict; disposable labs must opt out explicitly."""
    configured = os.environ.get("NETCODE_ANSIBLE_HOST_KEY_CHECKING", "true")
    return configured.strip().lower() not in {"0", "false", "no", "off"}


def _ansible_subprocess_env(job_root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    strict_host_keys = _ansible_host_key_checking_enabled()
    checking = "True" if strict_host_keys else "False"
    auto_add = "False" if strict_host_keys else "True"
    env.update(
        {
            "ANSIBLE_HOST_KEY_CHECKING": checking,
            "ANSIBLE_SSH_HOST_KEY_CHECKING": checking,
            "ANSIBLE_PARAMIKO_HOST_KEY_CHECKING": checking,
            "ANSIBLE_HOST_KEY_AUTO_ADD": auto_add,
            "ANSIBLE_PARAMIKO_HOST_KEY_AUTO_ADD": auto_add,
        }
    )
    if not strict_host_keys and job_root is not None:
        # Disposable labs regenerate SSH keys. Isolate their trust state from
        # the runner account instead of deleting or weakening global known_hosts.
        original_home = Path.home()
        ansible_home = job_root / "ansible-home"
        (ansible_home / ".ssh").mkdir(parents=True, exist_ok=True)
        local_temp = job_root / "ansible-local-temp"
        control_path = job_root / "ansible-pc"
        local_temp.mkdir(parents=True, exist_ok=True)
        control_path.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(ansible_home)
        env["ANSIBLE_LOCAL_TEMP"] = str(local_temp)
        env["ANSIBLE_PERSISTENT_CONTROL_PATH_DIR"] = str(control_path)

        user_site = site.getusersitepackages()
        user_sites = [user_site] if isinstance(user_site, str) else list(user_site)
        python_paths = [*user_sites]
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(item for item in python_paths if item)

        if not env.get("ANSIBLE_COLLECTIONS_PATH"):
            collection_paths = [
                original_home / ".ansible" / "collections",
                Path(sys.prefix) / "share" / "ansible" / "collections",
                Path("/usr/share/ansible/collections"),
            ]
            env["ANSIBLE_COLLECTIONS_PATH"] = os.pathsep.join(
                str(path) for path in collection_paths if path.exists()
            )
    return env


def _execute_ansible_pack(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute a reviewed Ansible pack on the local runner only.

    The runner re-audits the playbook against its own workspace and generates a
    temporary Ansible inventory from runner-local credentials. The control plane
    can request a pack, but cannot provide or override device credentials.
    """
    import subprocess
    import tempfile
    import shutil

    from netcode.ansible_backend import build_ansible_pack_plan
    from netcode.inventory import Inventory

    mode = str(payload.get("mode") or "check").strip().lower() or "check"
    targets = [str(item).strip() for item in (payload.get("targets") or []) if str(item).strip()]
    playbook_content = str(payload.get("playbook_content") or "")
    rollback_content = str(payload.get("rollback_playbook_content") or "")
    if not targets:
        return {"ok": False, "status": "fail", "action": "ansible_pack", "mode": mode, "message": "Explicit target device IDs are required."}
    if not INVENTORY_FILE.exists():
        return {"ok": False, "status": "fail", "action": "ansible_pack", "mode": mode, "message": f"No local inventory at {INVENTORY_FILE}."}
    if not playbook_content:
        return {
            "ok": False,
            "status": "fail",
            "action": "ansible_pack",
            "mode": mode,
            "message": "Reviewed playbook content was not included in the runner job.",
        }
    expected_hash = str(payload.get("playbook_sha256") or "")
    actual_hash = hashlib.sha256(playbook_content.encode("utf-8")).hexdigest()
    if not expected_hash or not hmac.compare_digest(expected_hash, actual_hash):
        return {
            "ok": False,
            "status": "fail",
            "action": "ansible_pack",
            "mode": mode,
            "message": "Playbook integrity verification failed on the runner.",
        }
    if rollback_content:
        rollback_hash = hashlib.sha256(rollback_content.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(
            str(payload.get("rollback_playbook_sha256") or ""), rollback_hash
        ):
            return {
                "ok": False,
                "status": "fail",
                "action": "ansible_pack",
                "mode": mode,
                "message": "Rollback playbook integrity verification failed on the runner.",
            }

    inventory = Inventory(INVENTORY_FILE)
    devices = []
    missing = []
    for target in targets:
        device = inventory.find_device(target)
        if device is None:
            missing.append(target)
        else:
            devices.append(device)
    if missing:
        return {
            "ok": False,
            "status": "fail",
            "action": "ansible_pack",
            "mode": mode,
            "message": f"Target device(s) not in local runner inventory: {', '.join(missing)}",
            "evidence": {"missing_targets": missing},
        }

    ansible_playbook = shutil.which("ansible-playbook")
    if not ansible_playbook:
        return {
            "ok": False,
            "status": "fail",
            "action": "ansible_pack",
            "mode": mode,
            "message": "ansible-playbook is not installed on this runner.",
            "evidence": {"required_binary": "ansible-playbook"},
        }

    with tempfile.TemporaryDirectory(prefix="netcode-ansible-") as tempdir:
        ws_root = Path(tempdir).resolve()
        playbook_name = Path(str(payload.get("playbook_name") or "playbook.yml")).name
        playbook = ws_root / playbook_name
        playbook.write_text(playbook_content, encoding="utf-8")
        rollback_playbook_path = ""
        if rollback_content:
            rollback_name = Path(
                str(payload.get("rollback_playbook_name") or "rollback.yml")
            ).name
            rollback_playbook = ws_root / rollback_name
            rollback_playbook.write_text(rollback_content, encoding="utf-8")
            rollback_playbook_path = rollback_name
        try:
            local_plan = build_ansible_pack_plan(
                ws_root,
                playbook_path=playbook_name,
                rollback_playbook_path=rollback_playbook_path,
                targets=targets,
                mode=mode,
                requested_by=str(payload.get("requested_by") or "runner"),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": "fail",
                "action": "ansible_pack",
                "mode": mode,
                "message": f"Local runner Ansible audit failed: {exc}",
            }
        if not local_plan.get("ok"):
            return {
                "ok": False,
                "status": "fail",
                "action": "ansible_pack",
                "mode": mode,
                "message": "Local runner Ansible audit blocked execution.",
                "evidence": {"plan": local_plan},
            }

        generated_inventory = ws_root / "inventory.yaml"
        _write_ansible_inventory(devices, generated_inventory)
        subprocess_env = _ansible_subprocess_env(ws_root)
        command = [
            ansible_playbook,
            str(playbook),
            "--inventory",
            str(generated_inventory),
            "--limit",
            ",".join(device.id for device in devices),
        ]
        if mode == "check":
            command.extend(["--check", "--diff"])
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=ws_root,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
                env=subprocess_env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "status": "fail",
                "action": "ansible_pack",
                "mode": mode,
                "message": "ansible-playbook timed out after 600s.",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "evidence": {"plan": local_plan, "stdout": str(exc.stdout or "")[-4000:], "stderr": str(exc.stderr or "")[-4000:]},
            }
    ok = completed.returncode == 0
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "action": "ansible_pack",
        "mode": mode,
        "targets": [device.id for device in devices],
        "message": "Ansible pack completed on the local runner." if ok else "Ansible pack failed on the local runner.",
        "duration_ms": int((time.monotonic() - started) * 1000),
        "runner_version": VERSION,
        "evidence": {
            "plan": local_plan,
            "playbook_sha256": actual_hash,
            "playbook_integrity_verified": True,
            "runner_local_inventory": True,
            "credentials_leave_runner": False,
            "command": [command[0], "<playbook>", "--inventory", "<runner-generated-inventory>", "--limit", ",".join(device.id for device in devices), *(["--check", "--diff"] if mode == "check" else [])],
            "stdout": str(completed.stdout or "")[-8000:],
            "stderr": str(completed.stderr or "")[-8000:],
            "returncode": completed.returncode,
        },
    }


READ_TIMEOUT_SECONDS = 30
READINESS_TIMEOUT_SECONDS = 55  # multi-device sweep; still under the control plane's 60s poll
MAX_READ_TIMEOUT_SECONDS = 120
MAX_DISCOVERY_TIMEOUT_SECONDS = 900


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
    from netcode.adapters.shell import netmiko_device_type

    return netmiko_device_type(platform)


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
        from netcode.adapters.shell import ssh_port_for

        conn = ConnectHandler(
            device_type=_netmiko_device_type(device.platform),
            host=device.host,
            username=device.username,
            password=device.password,
            port=ssh_port_for(device),
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


def _ping_succeeded(output: str) -> bool:
    text = str(output or "").lower()
    if re.search(r"(?:0\s+received|100(?:\.0)?%\s+packet\s+loss|success\s+rate\s+is\s+0\s+percent)", text):
        return False
    return bool(
        re.search(r"(?:[1-9]\d*\s+received|0(?:\.0)?%\s+packet\s+loss|success\s+rate\s+is\s+(?:[1-9]\d?|100)\s+percent)", text)
    )


def _execute_routing_reachability_checks(intent: Any) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for check in list(getattr(intent, "reachability_checks", None) or []):
        command = f"ping {check.destination} source {check.source_ip}"
        response = _execute_rez_ssh_command({"device": check.source_device, "command": command})
        output = str(response.get("stdout") or "")
        passed = bool(response.get("ok")) and _ping_succeeded(output)
        results.append({
            "source_device": check.source_device,
            "source_ip": check.source_ip,
            "destination": check.destination,
            "command": command,
            "passed": passed,
            "output": output,
            "error": str(response.get("error") or response.get("stderr") or ""),
        })
    return {
        "passed": bool(results) and all(bool(item["passed"]) for item in results),
        "checks": results,
    }


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
        from netcode.adapters.shell import ssh_port_for

        conn = ConnectHandler(
            device_type=_netmiko_device_type(device.platform),
            host=device.host,
            username=device.username,
            password=device.password,
            port=ssh_port_for(device),
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


def _execute_rez_scan_device(
    payload: dict[str, Any],
    *,
    persist_inventory: bool = True,
    inventory_path: Path | None = None,
) -> dict[str, Any]:
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

    inventory_path = inventory_path or INVENTORY_FILE
    inventory = Inventory(inventory_path)
    existing = inventory.find_device(device_id) if device_id else inventory.find_device(host)

    defaults = inventory.defaults
    username = existing.username if existing else str(defaults.get("username") or "")
    password = existing.password if existing else str(defaults.get("password") or "")
    port = requested_port or (existing.port if existing else int(defaults.get("port") or 22))

    # Auto-detection can fan out across several vendor drivers, so reject a
    # closed endpoint first. When the caller selected a platform, let that
    # adapter own transport validation; API-backed drivers are not required to
    # expose a generic SSH socket and can return a more precise failure.
    if existing is None and not requested_platform:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                pass
        except OSError as exc:
            return {
                "ok": False,
                "status": "fail",
                "found": False,
                "host": host,
                "port": port,
                "provider": "rez-runner",
                "requested_platform": requested_platform or "auto",
                "tried_platforms": [],
                "error": f"endpoint_unreachable:{type(exc).__name__}",
                "safety": {
                    "device_writes": "none",
                    "source_of_truth_written": False,
                    "message": "The endpoint did not accept a bounded connection, so vendor drivers were not tried.",
                },
            }

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
        state = dict(state)
        collected_at = datetime.now(timezone.utc).isoformat()
        state["_collected_at"] = str(state.get("_collected_at") or collected_at)
        state["collected_at"] = str(state.get("collected_at") or state["_collected_at"])
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
            "serial": str(state_summary.get("serial") or ""),
            "aliases": sorted({host, hostname} - {str(device_id or hostname)}),
        }
        action_taken = "observed"
        if persist_inventory:
            inventory_data = dict(inventory.raw or {})
            devices = list(inventory_data.get("devices") or [])
            persisted = dict(candidate)
            action_taken = "added"
            for index, raw_device in enumerate(devices):
                if not isinstance(raw_device, dict):
                    continue
                same_id = str(raw_device.get("id") or "").strip().lower() == str(candidate["id"]).strip().lower()
                same_endpoint = (
                    str(raw_device.get("host") or "") == host
                    and int(raw_device.get("port") or inventory.defaults.get("port") or 22) == port
                )
                if same_id or same_endpoint:
                    # Preserve any device-specific secrets already stored on the runner.
                    devices[index] = {**raw_device, **persisted}
                    action_taken = "updated"
                    break
            else:
                devices.append(persisted)
            inventory_data["devices"] = devices
            _atomic_write_inventory(inventory_path, inventory_data)
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
                "inventory": str(inventory_path),
                "written": persist_inventory,
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


def _execute_rez_discover_network(
    payload: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
    *,
    inventory_path: Path | None = None,
) -> dict[str, Any]:
    """Run bounded, recursive discovery entirely on the Local Connector."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from netcode.discovery_profile import (
        DiscoveryProfile,
        DiscoveryProfileError,
        DiscoveryTarget,
        discovery_neighbor_targets,
    )
    from netcode.inventory import Inventory

    inventory_path = inventory_path or INVENTORY_FILE
    if not inventory_path.exists():
        return {
            "ok": False,
            "status": "fail",
            "error": f"No Local Connector inventory at {inventory_path}.",
        }
    inventory = Inventory(inventory_path)
    try:
        profile = DiscoveryProfile.from_payload(payload, inventory)
    except DiscoveryProfileError as exc:
        return {"ok": False, "status": "fail", "error": str(exc), "scope_rejected": True}

    started = time.monotonic()
    event_log: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        safe = {
            "stage": str(event.get("stage") or "discovery"),
            "status": str(event.get("status") or "running"),
            "message": str(event.get("message") or "")[:1000],
            "device_id": str(event.get("device_id") or ""),
            "host": str(event.get("host") or ""),
            "depth": int(event.get("depth") or 0),
        }
        event_log.append(safe)
        if progress:
            progress(safe)

    emit({
        "stage": "scope_validated",
        "status": "running",
        "message": (
            f"Approved {len(profile.seeds)} seed(s); max {profile.max_devices} devices, "
            f"depth {profile.max_depth}, concurrency {profile.concurrency}."
        ),
    })

    frontier: list[tuple[DiscoveryTarget, int]] = [(target, 0) for target in profile.seeds]
    queued = {(target.host, target.port) for target in profile.seeds}
    scanned: set[tuple[str, int]] = set()
    states: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    while frontier and len(scanned) < profile.max_devices:
        current_depth = min(depth for _, depth in frontier)
        if current_depth > profile.max_depth:
            break
        wave = [item for item in frontier if item[1] == current_depth]
        frontier = [item for item in frontier if item[1] != current_depth]
        remaining = profile.max_devices - len(scanned)
        wave = wave[:remaining]
        emit({
            "stage": "wave_started",
            "status": "running",
            "depth": current_depth,
            "message": f"Collecting depth {current_depth}: {len(wave)} device(s).",
        })

        def scan_target(target: DiscoveryTarget) -> tuple[DiscoveryTarget, dict[str, Any]]:
            emit({
                "stage": "device_started",
                "status": "running",
                "device_id": target.device_id,
                "host": target.host,
                "depth": current_depth,
                "message": f"Collecting read-only state from {target.device_id or target.host}.",
            })
            return target, _execute_rez_scan_device(
                target.scan_payload(),
                persist_inventory=False,
                inventory_path=inventory_path,
            )

        completed: list[tuple[DiscoveryTarget, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=min(profile.concurrency, max(1, len(wave)))) as executor:
            futures = [executor.submit(scan_target, target) for target, _ in wave]
            for future in as_completed(futures):
                try:
                    completed.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    failures.append({"depth": current_depth, "error": f"collector_error:{type(exc).__name__}:{exc}"})

        for target, result in sorted(completed, key=lambda item: (item[0].device_id or item[0].host).lower()):
            target_key = (target.host, target.port)
            scanned.add(target_key)
            public_result = {
                key: value
                for key, value in result.items()
                if key not in {"state", "source_of_truth_yaml"}
            }
            public_result["depth"] = current_depth
            results.append(public_result)
            if not result.get("ok") or not isinstance(result.get("state"), dict):
                error = str(result.get("error") or result.get("message") or "collection_failed")
                if (
                    target.optional_probe
                    and not target.device_id
                    and error.startswith("endpoint_unreachable:")
                ):
                    skipped_target = {
                        "host": target.host,
                        "port": target.port,
                        "depth": current_depth,
                        "reason": "no_reachable_endpoint",
                    }
                    skipped.append(skipped_target)
                    emit({
                        "stage": "device_skipped",
                        "status": "skipped",
                        "host": target.host,
                        "depth": current_depth,
                        "message": "No reachable endpoint was found at this sweep address.",
                    })
                    continue
                failure = {
                    "device_id": target.device_id,
                    "host": target.host,
                    "port": target.port,
                    "depth": current_depth,
                    "error": error,
                    "tried_platforms": result.get("tried_platforms") or [],
                }
                failures.append(failure)
                emit({
                    "stage": "device_failed",
                    "status": "failed",
                    "device_id": target.device_id,
                    "host": target.host,
                    "depth": current_depth,
                    "message": failure["error"],
                })
                continue

            state = dict(result["state"])
            candidate = dict(result.get("source_of_truth_candidate") or {})
            node_id = str(candidate.get("id") or target.device_id or target.host)
            states[node_id] = state
            candidates.append(candidate)
            emit({
                "stage": "device_collected",
                "status": "passed",
                "device_id": node_id,
                "host": target.host,
                "depth": current_depth,
                "message": f"Collected normalized state from {node_id}.",
            })

            if current_depth >= profile.max_depth:
                continue
            for neighbor in discovery_neighbor_targets(state, inventory=inventory, profile=profile):
                neighbor_key = (neighbor.host, neighbor.port)
                if neighbor_key in scanned or neighbor_key in queued:
                    continue
                if len(queued) >= profile.max_devices:
                    break
                queued.add(neighbor_key)
                frontier.append((neighbor, current_depth + 1))

        emit({
            "stage": "wave_completed",
            "status": "running",
            "depth": current_depth,
            "message": f"Depth {current_depth} complete; {len(states)} device(s) collected.",
        })

    ok = bool(states)
    partial = ok and bool(failures)
    final_status = "partial" if partial else ("pass" if ok else "fail")
    emit({
        "stage": "discovery_completed" if ok else "discovery_failed",
        "status": "passed" if ok and not partial else ("partial" if partial else "failed"),
        "message": (
            (
                f"Discovery collected {len(states)} device(s); "
                f"{len(skipped)} unused sweep address(es) skipped; {len(failures)} failed."
            )
            if ok
            else "Discovery did not collect any device state."
        ),
    })
    return {
        "ok": ok,
        "status": final_status,
        "partial": partial,
        "provider": "rez-local-connector",
        "profile": profile.public_dict(),
        "device_states": states,
        "source_of_truth_candidates": candidates,
        "device_results": results,
        "failures": failures,
        "skipped_targets": skipped,
        "progress_events": event_log,
        "requested": len(scanned),
        "collected": len(states),
        "failed": len(failures),
        "skipped": len(skipped),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "safety": {
            "device_writes": "none",
            "credentials_returned": False,
            "execution_location": "local_connector",
            "scope_enforced": True,
            "source_of_truth_written": False,
        },
        "runner_version": VERSION,
    }


def _read_deadline_seconds(action: str, payload: dict[str, Any]) -> float:
    if action == "connector_capabilities":
        return 15.0
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
    if action == "rez_discover_network":
        try:
            requested = float(payload.get("_runner_timeout_seconds") or 300)
        except Exception:
            requested = 300.0
        return max(1.0, min(requested, float(MAX_DISCOVERY_TIMEOUT_SECONDS)))
    return float(READ_TIMEOUT_SECONDS)


def _execute_read(
    action: str,
    payload: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fail-closed wrapper: a hung device read must never wedge the runner's
    sequential job loop (a dead container / unreachable device can otherwise
    block every later job and stop heartbeats). Reads get a hard deadline."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    deadline = _read_deadline_seconds(action, payload)
    executor = ThreadPoolExecutor(max_workers=1)
    future = (
        executor.submit(_execute_read_inner, action, payload)
        if progress is None
        else executor.submit(_execute_read_inner, action, payload, progress)
    )
    try:
        return future.result(timeout=deadline)
    except FuturesTimeout:
        return {"ok": False, "status": "fail",
                "error": f"Runner read '{action}' timed out after {deadline}s (device unreachable or hung).",
                "message": f"Runner read '{action}' timed out after {deadline}s (device unreachable or hung)."}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _execute_read_inner(
    action: str,
    payload: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Device READ actions executed on the runner (next to the devices), using the
    runner's LOCAL credentialed inventory. Mirrors the control-plane read logic so
    the browser renders results identically whether local or runner mode."""
    import tempfile
    from netcode.inventory import Inventory
    from netcode.models import load_intent
    from netcode.yamlio import read_yaml, write_yaml

    if action == "connector_capabilities":
        expected_collections = (
            "arista.eos",
            "cisco.ios",
            "cisco.nxos",
            "junipernetworks.junos",
            "fortinet.fortios",
            "paloaltonetworks.panos",
        )
        ansible_playbook = shutil.which("ansible-playbook")
        ansible_galaxy = shutil.which("ansible-galaxy")
        installed_collections: dict[str, str] = {}
        collection_error = ""
        if ansible_galaxy:
            try:
                completed = subprocess.run(
                    [ansible_galaxy, "collection", "list", "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                if completed.returncode == 0:
                    raw_collections = json.loads(completed.stdout or "{}")
                    for collections in raw_collections.values() if isinstance(raw_collections, dict) else ():
                        if not isinstance(collections, dict):
                            continue
                        for name, metadata in collections.items():
                            version = str(metadata.get("version") or "installed") if isinstance(metadata, dict) else "installed"
                            installed_collections[str(name)] = version
                else:
                    collection_error = "Ansible collection inventory was unavailable."
            except Exception:
                collection_error = "Ansible collection inventory was unavailable."

        inventory_count = 0
        inventory_platforms: list[str] = []
        if INVENTORY_FILE.exists():
            try:
                local_inventory = Inventory(INVENTORY_FILE)
                inventory_count = len(local_inventory.by_id)
                inventory_platforms = sorted(
                    {str(device.platform or "unknown") for device in local_inventory.by_id.values()}
                )
            except Exception:
                collection_error = collection_error or "Local inventory could not be summarized."

        collection_rows = [
            {
                "name": name,
                "installed": name in installed_collections,
                "version": installed_collections.get(name, ""),
            }
            for name in expected_collections
        ]
        return {
            "ok": True,
            "status": "pass",
            "action": "connector_capabilities",
            "connector": {
                "version": VERSION,
                "operating_system": platform.system() or "unknown",
                "python_version": platform.python_version(),
                "outbound_only": True,
                "device_connections_opened": 0,
            },
            "device_access": {
                "ssh": True,
                "api": True,
                "credentials": "local_only",
            },
            "inventory": {
                "configured": INVENTORY_FILE.exists(),
                "device_count": inventory_count,
                "platforms": inventory_platforms,
            },
            "ansible": {
                "installed": bool(ansible_playbook),
                "collections": collection_rows,
                "collection_inventory_complete": bool(ansible_galaxy) and not collection_error,
                "message": collection_error,
            },
            "safety": {
                "device_writes": "none",
                "credentials_returned": False,
                "device_connections_opened": 0,
            },
        }

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
        from netcode.adapters.rez import READ_TRANSPORTS
        inventory = Inventory(INVENTORY_FILE)
        requested_ids = [
            str(value).strip()
            for value in (payload.get("device_ids") or [])
            if str(value).strip()
        ]
        missing: list[str] = []
        if requested_ids:
            devices = []
            for device_id in requested_ids:
                device = inventory.find_device(device_id)
                if device is None:
                    missing.append(device_id)
                elif device not in devices:
                    devices.append(device)
        else:
            devices = list(inventory.by_id.values())
        if not devices:
            return {
                "ok": False,
                "tested": 0,
                "readable": 0,
                "devices": [
                    {"id": device_id, "ok": False, "eligible": False, "error": "unknown_target"}
                    for device_id in missing
                ],
                "message": "No selected target exists in runner inventory." if requested_ids else "No devices in runner inventory.",
            }
        registry = AdapterRegistry()
        supported, excluded_rows = [], []
        for device in devices:
            normalized_platform = registry.rez.normalize_platform(device.platform)
            if normalized_platform not in READ_TRANSPORTS:
                excluded_rows.append({
                    "id": device.id,
                    "host": device.host,
                    "platform": device.platform,
                    "site": device.site,
                    "ok": False,
                    "eligible": False,
                    "error": f"unsupported_platform:{device.platform}",
                })
            else:
                supported.append(device)
        results = {
            str(item.get("device_id")): item
            for item in (registry.rez.collect_many(supported).get("results", []) if supported else [])
            if isinstance(item, dict)
        }
        rows, readable = [], 0
        for d in supported:
            r = results.get(d.id) or {}
            ok = bool(r.get("ok"))
            readable += 1 if ok else 0
            err = "" if ok else str(r.get("error") or (r.get("errors") or ["unreadable"])[0])
            rows.append({
                "id": d.id,
                "host": d.host,
                "platform": d.platform,
                "site": d.site,
                "ok": ok,
                "eligible": True,
                "error": err,
            })
        rows.extend(excluded_rows)
        rows.extend(
            {"id": device_id, "ok": False, "eligible": False, "error": "unknown_target"}
            for device_id in missing
        )
        tested = len(supported)
        return {
            "ok": tested > 0 and readable == tested and not missing and not excluded_rows,
            "tested": tested,
            "readable": readable,
            "devices": rows,
            "excluded": len(excluded_rows) + len(missing),
            "requested": len(requested_ids),
            "message": f"{readable}/{tested} selected supported devices are readable.",
        }

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

    if action == "rez_discover_network":
        return _execute_rez_discover_network(payload, progress=progress)

    if action == "rez_server_listener_probe":
        return _execute_rez_server_listener_probe(payload)

    if action == "rez_http_flow_probe":
        return _execute_rez_http_flow_probe(payload)
    if action == "cross_domain_verify":
        from netcode.cross_domain_runner import collect_exact_flow_evidence

        def collect_state(device_id: str) -> dict[str, Any]:
            device, state, error = _collect_rez_runner_state(device_id)
            if error:
                return error
            return {"ok": True, "device_id": getattr(device, "id", device_id), "state": state}

        def application_probe(flow) -> dict[str, Any]:  # noqa: ANN001
            if flow.protocol != "tcp" or flow.destination_port is None:
                return {"connected": None, "reason": "no_certified_probe_for_protocol"}
            result = _execute_rez_server_listener_probe({
                "source_device": flow.source_device,
                "src_ip": flow.source_ip,
                "dst_ip": flow.destination_ip,
                "dst_port": flow.destination_port,
                "timeout_seconds": 3,
            })
            return {
                **result,
                "connected": result.get("listener_present") is True,
                "refused": result.get("listener_present") is False,
            }

        return collect_exact_flow_evidence(payload, collect_state=collect_state, application_probe=application_probe)

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
        adapter = AristaEOSLabAdapter(device, progress=progress, operation="verify")
        try:
            adapter.connect()
            verification = adapter.verify_intent(intent, present=bool(payload.get("present", True)))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "device_id": device_id}
        finally:
            adapter.disconnect()
        verification_payload = dict(verification.__dict__)
        overall_ok = verification.status == "pass"
        if intent.change_type == "routing_redistribution" and bool(getattr(intent, "reachability_checks", None)):
            reachability = _execute_routing_reachability_checks(intent) if overall_ok else {
                "passed": False,
                "checks": [],
                "skipped": "configuration verification failed",
            }
            evidence = dict(verification_payload.get("evidence") or {})
            evidence["reachability"] = reachability
            verification_payload["evidence"] = evidence
            overall_ok = overall_ok and bool(reachability.get("passed"))
            verification_payload["status"] = "pass" if overall_ok else "fail"
            verification_payload["message"] = (
                "Controlled route exchange and scoped reachability checks passed."
                if overall_ok
                else "Controlled route exchange is present, but scoped reachability still failed."
            )
        return {
            "ok": overall_ok,
            "device_id": device.id,
            "platform": device.platform,
            "change_type": intent.change_type,
            "verification": verification_payload,
        }

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
        # Human CLI: the audit/guard runs HERE (the trust boundary). Config is
        # allowed for the interactive engineer path; unattended changes still
        # use the plan/dry-run/approval/apply pipeline.
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
        guard_enabled = bool(raw.get("guard_enabled", False))
        if guard_enabled:
            decision = guard_submit(state, str(payload.get("input", "")))
        else:
            lines = [line for line in str(payload.get("input", "")).replace("\r", "\n").split("\n") if line.strip()]
            for line in lines:
                normalized = " ".join(line.lower().split())
                if normalized in ("configure terminal", "conf t", "configure"):
                    state.in_config = True
                elif normalized in ("end", "disable"):
                    state.in_config = False
                elif normalized == "exit" and state.in_config:
                    continue
                elif state.in_config and normalized and not normalized.startswith(("show ", "do show ")):
                    state.device_touched = True
            decision = {
                "kind": "direct",
                "lines": lines,
                "events": [{"type": "guard", "action": "direct_mode", "message": "Full live shell: command sent to device and recorded."}],
                "state": state.as_dict(),
            }
        output = ""
        executed = False
        cleared = decision["kind"] in ("run_reads", "run_live", "direct")
        if decision["kind"] in ("run_reads", "run_live", "direct"):
            adapter_key = _shell_adapter_key(payload, device.id)
            try:
                adapter = _shell_adapter_for(adapter_key, device)
                output = "\n".join(adapter.show(line.strip()) for line in decision["lines"])
                executed = True
            except Exception as exc:  # noqa: BLE001
                _shell_adapter_drop(adapter_key)
                output = f"[shell] device read failed: {exc}"
        return {"ok": True, "cleared": cleared, "executed": executed, "guard_kind": decision["kind"],
                "output": output, "events": decision["events"], "state": decision["state"],
                "device_touched": bool(decision["state"].get("device_touched")),
                "device_id": device.id, "platform": device.platform}

    if action == "discovery":
        # Discovery must use the same runner-local inventory and credential
        # defaults that later Shell sessions use. It also persists the public
        # device facts locally so the connector can resolve the new device.
        return _execute_rez_scan_device(payload)

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
    try:
        identity = _maintain_runner_token(identity)
    except Exception as exc:  # noqa: BLE001 - existing token remains available for retry.
        print(f"[runner] Token maintenance deferred: {exc}", file=sys.stderr)
    server = identity["server"]
    token = identity["runner_token"]
    secret = identity["hmac_secret"]
    pool = identity["pool"]
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    print(f"[runner] Polling {server} for pool '{pool}' as {identity['name']} (v{VERSION}). Ctrl-C to stop.")
    _start_interactive_channel(server, lambda: token)  # token provider covers rotation on reconnect
    try:
        _post(server, "/api/runner/heartbeat", {"version": VERSION, "state": "online"}, token=token)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] Heartbeat failed (continuing): {exc}", file=sys.stderr)
    inventory_revision = ""
    inventory_sync_at = 0.0
    token_maintenance_at = time.monotonic() + 60.0
    try:
        inventory_revision = _sync_inventory_catalog(server, token)
        inventory_sync_at = time.monotonic()
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] Inventory catalog sync failed (continuing): {exc}", file=sys.stderr)
    while not _stop:
        if time.monotonic() >= token_maintenance_at:
            try:
                identity = _maintain_runner_token(identity)
                token = identity["runner_token"]
            except Exception as exc:  # noqa: BLE001 - retain current identity and retry later.
                print(f"[runner] Token maintenance deferred: {exc}", file=sys.stderr)
            token_maintenance_at = time.monotonic() + 60.0
        if time.monotonic() - inventory_sync_at >= 30.0:
            try:
                inventory_revision = _sync_inventory_catalog(server, token, inventory_revision)
            except Exception as exc:  # noqa: BLE001
                print(f"[runner] Inventory catalog sync failed (continuing): {exc}", file=sys.stderr)
            inventory_sync_at = time.monotonic()
        try:
            resp = _post(server, "/api/runner/poll", {"wait_seconds": 20}, token=token, timeout=40)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] Poll error: {exc}; retrying in 5s.", file=sys.stderr)
            time.sleep(5)
            continue
        if not resp:
            time.sleep(1)
            continue  # 204: no job, poll again
        job = resp.get("job") or {}
        job_id = job.get("id")
        action = (job.get("payload") or {}).get("action")
        lease_token = str(job.get("lease_token") or "")
        if not job_id or not lease_token:
            print("[runner] Refusing an unleased job claim from the control plane.", file=sys.stderr, flush=True)
            time.sleep(1)
            continue
        print(f"[runner] Claimed job {job_id} ({action}).")
        progress = _progress_reporter(server, token, secret, job)
        lease = _JobLeaseRenewer(server, token, job)
        lease.start()
        try:
            result = _execute_job(job, progress=progress)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "fail", "message": f"Runner execution error: {type(exc).__name__}: {exc}"}
        signature = hmac.new(secret.encode("utf-8"), _canonical(result).encode("utf-8"), hashlib.sha256).hexdigest()
        try:
            ack = _post(
                server,
                f"/api/runner/jobs/{job_id}/result",
                {"result": result, "signature": signature, "lease_token": lease_token},
                token=token,
            )
            print(f"[runner] Reported job {job_id}: {result.get('status')} — {(ack or {}).get('message', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] Failed to report job {job_id}: {exc}", file=sys.stderr)
        finally:
            lease.stop()
    try:
        _post(server, "/api/runner/heartbeat", {"version": VERSION, "state": "draining"}, token=token)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] Final drain heartbeat failed: {exc}", file=sys.stderr)
    print("[runner] Stopped.")
    return 0


def _start_interactive_channel(server: str, token_provider: Callable[[], str]) -> None:
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
            guard_enabled = bool(raw.get("guard_enabled", False))
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
                ws.send(json.dumps({"token": token_provider()}))
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
    if os.getenv("NETCODE_ALLOW_MANUAL_INVENTORY_IMPORT", "").strip() == "1":
        p_import = sub.add_parser("inventory-import", help="Import inventory for an approved migration.")
        p_import.add_argument("file", help="Path to an approved migration inventory YAML.")
        p_import.set_defaults(func=import_inventory)
    p_discover = sub.add_parser(
        "discover-inventory",
        help="Build local inventory from bounded, read-only network discovery.",
    )
    p_discover.add_argument("--seeds", required=True, help="Comma-separated IPs, ranges, or CIDRs.")
    p_discover.add_argument("--username", required=True, help="Runner-local device username.")
    p_discover.add_argument("--site", default="unassigned")
    p_discover.add_argument("--platform", default="", help="Optional Rez platform; empty enables detection.")
    p_discover.add_argument("--port", type=int, default=22)
    p_discover.add_argument("--allowed-cidrs", action="append", default=[])
    p_discover.add_argument("--excluded-cidrs", action="append", default=[])
    p_discover.add_argument("--depth", type=int, default=1)
    p_discover.add_argument("--max-devices", type=int, default=COMMUNITY_MAX_DEVICES)
    p_discover.add_argument("--concurrency", type=int, default=4)
    p_discover.add_argument("--replace", action="store_true", help="Replace rather than merge discovered inventory.")
    p_discover.set_defaults(func=discover_inventory)
    p_doctor = sub.add_parser("doctor", help="Check enrollment, inventory, and outbound control-plane reachability.")
    p_doctor.add_argument("--timeout", type=float, default=10.0)
    p_doctor.set_defaults(func=doctor)
    p_run = sub.add_parser("run", help="Poll for and execute jobs.")
    p_run.set_defaults(func=run)
    p_control = sub.add_parser("control", help="Open the Windows Local Connector control application.")
    p_control.set_defaults(func=lambda _args: __import__("netcode.windows_connector_control", fromlist=["main"]).main())
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
