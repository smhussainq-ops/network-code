"""First-party Netcode workflow pack catalog."""

from __future__ import annotations

from typing import Any

from netcode.change_types import REGISTRY


_PACKS: list[dict[str, Any]] = [
    {
        "id": "golden-baseline-standardization",
        "name": "Golden Baseline Standardization",
        "description": "Standardize baseline services such as NTP and approved platform config with dry-run, canary, verify, and rollback.",
        "change_types": ["ntp_standardize", "custom_config"],
        "target_selector": ["site", "device_group", "tag"],
        "default_gates": ["plan", "validate", "dry_run", "canary", "batch_apply", "verify", "rollback"],
        "diagnostics_handoff": True,
        "production_writes": "locked_until_approved",
    },
    {
        "id": "branch-site-onboarding",
        "name": "Branch / Site Onboarding",
        "description": "Create source-of-truth site/device intent, VLANs, and interface roles for a new branch or access site.",
        "change_types": ["site_device_intent", "add_vlan", "interface_config"],
        "target_selector": ["site", "device_group"],
        "default_gates": ["plan", "validate", "dry_run", "canary", "batch_apply", "verify", "rollback"],
        "diagnostics_handoff": True,
        "production_writes": "locked_until_approved",
    },
    {
        "id": "controlled-routing-acl-update",
        "name": "Controlled Routing / ACL Update",
        "description": "Plan and gate BGP neighbor or ACL changes where blast radius and rollback proof are mandatory.",
        "change_types": ["bgp_neighbor", "routing_redistribution", "acl_rule"],
        "target_selector": ["site", "device_id", "device_group"],
        "default_gates": ["plan", "validate", "dry_run", "peer_review", "canary", "verify", "rollback"],
        "diagnostics_handoff": True,
        "production_writes": "locked_until_approved",
    },
    {
        "id": "eos-os-upgrade",
        "name": "EOS OS Upgrade",
        "description": "Stage EOS images with MD5 proof, boot-variable rollback, maintenance-window approval, canary reload, and batch promotion.",
        "change_types": ["os_upgrade"],
        "target_selector": ["site", "device_id", "device_group"],
        "default_gates": ["pre_check", "stage_image", "md5_verify", "dry_run", "peer_review", "maintenance_window", "canary", "verify", "batch_apply", "rollback"],
        "diagnostics_handoff": True,
        "production_writes": "locked_until_approved",
    },
]


def _catalog_packs() -> list[dict[str, Any]]:
    packs: list[dict[str, Any]] = []
    for pack in _PACKS:
        missing = [change_type for change_type in pack["change_types"] if change_type not in REGISTRY]
        status = "ready" if not missing else "blocked"
        packs.append(
            {
                **pack,
                "status": status,
                "missing_change_types": missing,
                "native_engine": True,
                "ansible_backend": False,
            }
        )
    return packs


def entitled_change_types(max_packs: int) -> set[str]:
    allowed: set[str] = set()
    for pack in _catalog_packs()[: max(0, int(max_packs))]:
        if pack["status"] == "ready":
            allowed.update(str(value) for value in pack["change_types"])
    return allowed


def workflow_pack_catalog(max_packs: int | None = None) -> dict[str, Any]:
    all_packs = _catalog_packs()
    limit = len(all_packs) if max_packs is None else max(0, int(max_packs))
    packs = all_packs[:limit]
    return {
        "ok": True,
        "catalog_version": "netcode-native-workflow-packs.v1",
        "packs": packs,
        "entitled_count": len(packs),
        "available_count": len(all_packs),
        "safety": {
            "credentials": "runner-local",
            "writes": "gated",
            "diagnostics": "read-only",
        },
    }
