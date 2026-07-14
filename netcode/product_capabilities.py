"""Machine-readable, evidence-bound product support matrix.

Adapter registration proves that code can be loaded.  It does not prove live
read depth or safe writes.  This matrix is the product contract consumed by the
API and release evidence; unsupported and unproven actions stay explicit.
"""

from __future__ import annotations

from typing import Any

from netcode.adapters.registry import AdapterRegistry
from netcode.adapters.rez import READ_TRANSPORTS


STATUS_VALUES = frozenset({
    "GA",
    "pilot-certified",
    "contract-tested",
    "read-only",
    "manager-assisted",
    "hardware-blocked",
    "planned",
    "unsupported",
})

FEATURES = (
    "discovery",
    "ssh_read",
    "api_read",
    "shell",
    "configured_state",
    "network_map",
    "network_health",
    "rez_rca",
    "validation",
    "dry_run",
    "write",
    "verify",
    "rollback",
    "manager_execution",
)

_CONFIGURED_STATE_PLATFORMS = frozenset({
    "arista_eos",
    "cisco_ios",
    "cisco_nxos",
    "cisco_asa",
    "juniper_junos",
    "nokia_srl",
    "fortinet",
    "palo_alto",
    "meraki",
    "fortimanager",
    "panorama",
})

_PLATFORM_LABELS = {
    "arista_eos": "Arista EOS",
    "cisco_ios": "Cisco IOS / IOS-XE",
    "cisco_nxos": "Cisco NX-OS",
    "cisco_asa": "Cisco ASA",
    "juniper_junos": "Juniper Junos",
    "nokia_srl": "Nokia SR Linux",
    "fortinet": "Fortinet FortiGate",
    "palo_alto": "Palo Alto PAN-OS",
    "aruba_aoscx": "HPE Aruba AOS-CX",
    "cisco_sdwan": "Cisco SD-WAN / vManage",
    "meraki": "Cisco Meraki Dashboard",
    "fortimanager": "Fortinet FortiManager",
    "panorama": "Palo Alto Panorama",
}

_EVIDENCE = {
    "arista_eos": [
        "NETCODE_REZ_FULL_LOOP_CERTIFICATION_2026-07-08",
        "DIGITAL_TWIN_ENTERPRISE_UPGRADE_RESULTS_2026-07-11",
        "SHELL_TRANSCRIPT_HISTORY_CERTIFICATION_2026-07-10",
    ],
    "fortinet": [
        "REZ_CERTIFICATION_MASTER_LEDGER_2026-06-12",
        "uc101_150_certification_status_manifest_2026_06_13",
    ],
}


def _status(value: str, note: str = "") -> dict[str, str]:
    if value not in STATUS_VALUES:
        raise ValueError(f"Unsupported capability status: {value}")
    result = {"status": value}
    if note:
        result["note"] = note
    return result


def _base_capabilities(platform: str) -> dict[str, dict[str, str]]:
    transports = set(READ_TRANSPORTS.get(platform, ()))
    has_driver = platform in READ_TRANSPORTS
    configured = platform in _CONFIGURED_STATE_PLATFORMS
    controller = platform in {"fortimanager", "panorama"}
    return {
        "discovery": _status("contract-tested" if has_driver else "unsupported"),
        "ssh_read": _status("contract-tested" if "ssh" in transports else "unsupported"),
        "api_read": _status("contract-tested" if "api" in transports else "unsupported"),
        "shell": _status("contract-tested" if "ssh" in transports and not controller else "unsupported"),
        "configured_state": _status("contract-tested" if configured else "unsupported"),
        "network_map": _status("contract-tested" if has_driver else "unsupported"),
        "network_health": _status("contract-tested" if has_driver and not controller else "read-only"),
        "rez_rca": _status("contract-tested" if has_driver else "unsupported"),
        "validation": _status("contract-tested" if has_driver else "unsupported"),
        "dry_run": _status("planned" if not controller else "hardware-blocked"),
        "write": _status("planned" if not controller else "hardware-blocked"),
        "verify": _status("planned" if not controller else "hardware-blocked"),
        "rollback": _status("planned" if not controller else "hardware-blocked"),
        "manager_execution": _status("hardware-blocked" if controller else "unsupported"),
    }


def _reviewed_capabilities(platform: str) -> dict[str, dict[str, str]]:
    capabilities = _base_capabilities(platform)
    if platform == "arista_eos":
        for feature in (
            "discovery",
            "ssh_read",
            "shell",
            "configured_state",
            "network_map",
            "network_health",
            "rez_rca",
            "validation",
            "dry_run",
            "write",
            "verify",
            "rollback",
        ):
            capabilities[feature] = _status("pilot-certified")
        capabilities["api_read"] = _status("contract-tested", "eAPI read path is implemented; the launch write proof uses SSH config sessions.")
    elif platform == "fortinet":
        for feature in ("discovery", "ssh_read", "api_read", "configured_state", "rez_rca", "validation"):
            capabilities[feature] = _status("pilot-certified")
        capabilities["network_map"] = _status("contract-tested")
        capabilities["network_health"] = _status("contract-tested")
        capabilities["write"] = _status("planned", "Direct FortiGate writes are not a launch capability; use a reviewed manager path when certified.")
    elif platform == "cisco_ios":
        capabilities["dry_run"] = _status("contract-tested", "Offline validation and generated diff; no native candidate commit claim.")
        for feature in ("write", "verify", "rollback"):
            capabilities[feature] = _status(
                "contract-tested",
                "Community Golden Baseline NTP workflow only; live Cisco GNS3 proof is still required.",
            )
    elif platform in {"fortimanager", "panorama"}:
        capabilities["manager_execution"] = _status(
            "hardware-blocked",
            "Typed preview/install/reconcile contracts exist; real controller candidate, install, verify, and rollback proof is required.",
        )
    return capabilities


def product_support_matrix(registry: AdapterRegistry | None = None) -> dict[str, Any]:
    """Return the reviewed support contract plus runtime adapter availability."""
    adapter_registry = registry or AdapterRegistry()
    runtime = adapter_registry.rez.summary()
    runtime_platforms = set(runtime.get("platforms", [])) if runtime.get("available") else set()
    rows = []
    for platform in sorted(READ_TRANSPORTS):
        capabilities = _reviewed_capabilities(platform)
        execution = AdapterRegistry.EXECUTION_ADAPTERS.get(platform, {})
        rows.append({
            "platform": platform,
            "label": _PLATFORM_LABELS[platform],
            "runtime_adapter_available": platform in runtime_platforms,
            "read_transports": list(READ_TRANSPORTS[platform]),
            "capabilities": capabilities,
            "supported_change_types": list(execution.get("supported_change_types", [])),
            "evidence": list(_EVIDENCE.get(platform, [])),
        })
    return {
        "schema": "rezonance.product-support.v1",
        "status_values": sorted(STATUS_VALUES),
        "features": list(FEATURES),
        "rows": rows,
        "rules": {
            "adapter_registration_is_not_write_support": True,
            "unknown_platform_status": "unsupported",
            "hardware_blocked_is_not_pilot_certified": True,
        },
    }


def unsupported_platform_row(platform: str) -> dict[str, Any]:
    """Return a fail-closed row for an undeclared platform."""
    normalized = str(platform or "unknown").strip().lower() or "unknown"
    return {
        "platform": normalized,
        "label": normalized,
        "runtime_adapter_available": False,
        "read_transports": [],
        "capabilities": {feature: _status("unsupported") for feature in FEATURES},
        "supported_change_types": [],
        "evidence": [],
    }
