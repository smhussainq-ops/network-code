"""Device discovery backed by Rez multi-vendor state drivers."""

from __future__ import annotations

import re
import time
from typing import Any

from netcode.adapters.rez import RezAdapterBridge
from netcode.inventory import Device, Inventory
from netcode.paths import WorkspacePaths
from netcode.ui_config import configured_inventory_path
from netcode.yamlio import dumps_yaml, read_yaml, write_yaml

SSH_AUTODETECT_ORDER = [
    "cisco_ios",
    "arista_eos",
    "cisco_nxos",
    "cisco_asa",
    "juniper_junos",
    "aruba_aoscx",
    "nokia_srl",
    "fortinet",
    "palo_alto",
]


def _safe_device_id(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-").lower()
    return candidate or "discovered-device"


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _count_collection(value: Any) -> int:
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, list):
        return len(value)
    return 0


def _extract_state_summary(state: Any, fallback_hostname: str, platform: str) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {
            "hostname": fallback_hostname,
            "platform": platform,
            "interfaces": 0,
            "vlans": 0,
            "routes": 0,
            "bgp_neighbors": 0,
        }

    device = state.get("device") if isinstance(state.get("device"), dict) else {}
    layer2 = state.get("layer2") if isinstance(state.get("layer2"), dict) else {}
    routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
    bgp = state.get("bgp") if isinstance(state.get("bgp"), dict) else {}

    interfaces = state.get("interfaces") or device.get("interfaces") or []
    vlans = layer2.get("vlans") or state.get("vlans") or []
    routes = routing.get("routes") or state.get("routes") or []
    bgp_neighbors = bgp.get("neighbors") or state.get("bgp_neighbors") or []

    return {
        "hostname": _first_string(
            device.get("hostname"),
            state.get("hostname"),
            state.get("node_id"),
            fallback_hostname,
        ),
        "platform": _first_string(state.get("platform"), device.get("platform"), platform),
        "model": _first_string(device.get("model"), state.get("model")),
        "version": _first_string(device.get("version"), state.get("version")),
        "interfaces": _count_collection(interfaces),
        "vlans": _count_collection(vlans),
        "routes": _count_collection(routes),
        "bgp_neighbors": _count_collection(bgp_neighbors),
    }


class DiscoveryService:
    """Read-only discovery and source-of-truth staging."""

    def __init__(self, paths: WorkspacePaths, rez: RezAdapterBridge | None = None):
        self.paths = paths
        self.rez = rez or RezAdapterBridge()

    def scan(
        self,
        *,
        host: str,
        username: str = "",
        password: str = "",
        platform: str = "",
        port: int = 22,
        device_id: str = "",
        site: str = "",
        groups: list[str] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        host = host.strip()
        if not host:
            return {"ok": False, "error": "Device IP/hostname is required."}

        inventory = Inventory(configured_inventory_path(self.paths))
        existing = self._match_existing_device(inventory, host, device_id)
        defaults = inventory.defaults
        effective_username = username or (existing.username if existing else str(defaults.get("username") or ""))
        effective_password = password or (existing.password if existing else str(defaults.get("password") or ""))
        effective_port = port or (existing.port if existing else int(defaults.get("port") or 22))
        requested_platform = self.rez.normalize_platform(platform) or (existing.platform if existing else "")

        driver_map = self.rez.driver_map()
        if not driver_map:
            return {
                "ok": False,
                "host": host,
                "provider": "rez",
                "error": self.rez.summary().get("error") or "Rez drivers unavailable",
                "duration_seconds": round(time.perf_counter() - started, 3),
            }

        candidates = self._candidate_platforms(requested_platform, driver_map)
        if requested_platform and requested_platform not in driver_map:
            return {
                "ok": False,
                "host": host,
                "provider": "rez",
                "requested_platform": requested_platform,
                "error": f"Rez has no driver for platform {requested_platform}",
                "supported_platforms": sorted(driver_map.keys()),
                "duration_seconds": round(time.perf_counter() - started, 3),
            }

        attempts: list[dict[str, Any]] = []
        winning_state: dict[str, Any] | None = None
        winning_platform = ""
        for candidate in candidates:
            probe = Device(
                id=device_id or (existing.id if existing else _safe_device_id(host)),
                hostname=device_id or (existing.hostname if existing else _safe_device_id(host)),
                host=host,
                platform=candidate,
                username=effective_username,
                password=effective_password,
                port=effective_port,
                site=site or (existing.site if existing else None),
                groups=tuple(groups or (list(existing.groups) if existing else [])),
            )
            result = self.rez.collect_device_state(probe)
            attempts.append(
                {
                    "platform": candidate,
                    "ok": bool(result.get("ok")),
                    "adapter": result.get("adapter"),
                    "error": result.get("error"),
                    "warnings": result.get("warnings", []),
                    "collection_time": result.get("collection_time"),
                }
            )
            if result.get("ok"):
                winning_state = result
                winning_platform = candidate
                break
            if requested_platform:
                break

        if not winning_state:
            return {
                "ok": False,
                "found": False,
                "host": host,
                "provider": "rez",
                "requested_platform": requested_platform or "auto",
                "tried_platforms": attempts,
                "supported_platforms": sorted(driver_map.keys()),
                "safety": {
                    "device_writes": "none",
                    "source_of_truth_written": False,
                    "message": "Discovery failed or the device did not accept the tried Rez driver.",
                },
                "duration_seconds": round(time.perf_counter() - started, 3),
            }

        state_summary = _extract_state_summary(winning_state.get("state"), device_id or host, winning_platform)
        hostname = state_summary.get("hostname") or device_id or _safe_device_id(host)
        candidate = {
            "id": _safe_device_id(device_id or str(hostname)),
            "hostname": str(hostname),
            "host": host,
            "platform": winning_platform,
            "site": site or (existing.site if existing else "unassigned"),
            "groups": groups or (list(existing.groups) if existing else ["discovered"]),
            "port": effective_port,
        }
        source_yaml = dumps_yaml({"devices": [candidate]})
        return {
            "ok": True,
            "found": True,
            "provider": "rez",
            "host": host,
            "platform": winning_platform,
            "adapter": winning_state.get("adapter"),
            "driver": winning_state.get("driver"),
            "existing_device_id": existing.id if existing else None,
            "state_summary": state_summary,
            "source_of_truth_candidate": candidate,
            "source_of_truth_yaml": source_yaml,
            "tried_platforms": attempts,
            "supported_platforms": sorted(driver_map.keys()),
            "warnings": winning_state.get("warnings", []),
            "errors": winning_state.get("errors", []),
            "safety": {
                "device_writes": "none",
                "source_of_truth_written": False,
                "message": "Discovery used Rez read/state collection only. Review the candidate before importing it.",
            },
            "duration_seconds": round(time.perf_counter() - started, 3),
        }

    def import_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        required = ["id", "host", "platform"]
        missing = [key for key in required if not str(candidate.get(key) or "").strip()]
        if missing:
            return {"ok": False, "error": f"Missing required source-of-truth fields: {', '.join(missing)}"}

        inventory_path = configured_inventory_path(self.paths)
        inventory = read_yaml(inventory_path)
        devices = list(inventory.get("devices") or [])
        sanitized = {
            "id": _safe_device_id(str(candidate.get("id"))),
            "hostname": str(candidate.get("hostname") or candidate.get("id")),
            "host": str(candidate.get("host")),
            "platform": self.rez.normalize_platform(str(candidate.get("platform"))) or str(candidate.get("platform")),
            "site": str(candidate.get("site") or "unassigned"),
            "groups": [str(group) for group in candidate.get("groups") or ["discovered"]],
            "port": int(candidate.get("port") or 22),
        }
        action = "added"
        for index, existing in enumerate(devices):
            if str(existing.get("id")) == sanitized["id"] or str(existing.get("host")) == sanitized["host"]:
                devices[index] = {**existing, **sanitized}
                action = "updated"
                break
        else:
            devices.append(sanitized)

        inventory["devices"] = devices
        write_yaml(inventory_path, inventory)
        return {
            "ok": True,
            "action": action,
            "inventory": str(inventory_path),
            "device": sanitized,
            "source_of_truth_written": True,
            "message": f"Device {sanitized['id']} {action} in local YAML source of truth.",
        }

    def _match_existing_device(self, inventory: Inventory, host: str, device_id: str = "") -> Device | None:
        lookup = (device_id or "").strip()
        return inventory.find_device(lookup) if lookup else inventory.find_device(host)

    def _candidate_platforms(self, requested_platform: str, driver_map: dict[str, Any]) -> list[str]:
        if requested_platform:
            return [requested_platform]
        ordered = [platform for platform in SSH_AUTODETECT_ORDER if platform in driver_map]
        remaining = sorted(set(driver_map) - set(ordered))
        return ordered + remaining
