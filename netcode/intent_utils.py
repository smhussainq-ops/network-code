"""Desired-state intent helpers."""

from __future__ import annotations

import re
from typing import Any

from netcode.models import (
    AclRuleIntent,
    AddVlanIntent,
    BgpNeighborIntent,
    CustomConfigIntent,
    InterfaceConfigIntent,
    Intent,
    SiteDeviceIntent,
)


CHANGE_TYPE_LABELS = {
    "add_vlan": "Add VLAN",
    "interface_config": "Interface Config",
    "bgp_neighbor": "BGP Neighbor",
    "acl_rule": "ACL Rule",
    "site_device_intent": "Site / Device Intent",
    "custom_config": "Custom Config",
}


def _custom_first_line(intent: "CustomConfigIntent") -> str:
    lines = [line.strip() for line in intent.custom.config_lines.splitlines() if line.strip()]
    return lines[0] if lines else "custom config"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-").lower() or "intent"


def intent_title(intent: Intent) -> str:
    if isinstance(intent, AddVlanIntent):
        return f"Add VLAN {intent.vlan.id} ({intent.vlan.name})"
    if isinstance(intent, InterfaceConfigIntent):
        return f"Configure {intent.interface.name}"
    if isinstance(intent, BgpNeighborIntent):
        first = intent.bgp.neighbors[0]
        return f"Configure BGP neighbor {first.address}"
    if isinstance(intent, AclRuleIntent):
        return f"Update ACL {intent.acl.name} sequence {intent.acl.sequence}"
    if isinstance(intent, SiteDeviceIntent):
        return f"Register {intent.device.device_id} as {intent.device.role}"
    if isinstance(intent, CustomConfigIntent):
        return f"Custom config: {intent.custom.description or _custom_first_line(intent)}"
    return CHANGE_TYPE_LABELS.get(intent.change_type, intent.change_type)


def intent_slug(intent: Intent) -> str:
    if isinstance(intent, AddVlanIntent):
        return f"{intent.site}-add-vlan-{intent.vlan.id}"
    if isinstance(intent, InterfaceConfigIntent):
        return f"{intent.site}-interface-{safe_name(intent.interface.name)}"
    if isinstance(intent, BgpNeighborIntent):
        first = intent.bgp.neighbors[0]
        return f"{intent.site}-bgp-{intent.bgp.asn}-neighbor-{safe_name(first.address)}"
    if isinstance(intent, AclRuleIntent):
        return f"{intent.site}-acl-{safe_name(intent.acl.name)}-{intent.acl.sequence}"
    if isinstance(intent, SiteDeviceIntent):
        return f"{intent.site}-device-{safe_name(intent.device.device_id)}"
    if isinstance(intent, CustomConfigIntent):
        return f"{intent.site}-custom-{safe_name(intent.custom.description or _custom_first_line(intent))[:32]}"
    return f"{intent.site}-{safe_name(intent.change_type)}"


def template_for_intent(intent: Intent) -> str:
    templates = {
        "add_vlan": "add_vlan.j2",
        "interface_config": "interface_config.j2",
        "bgp_neighbor": "bgp_neighbor.j2",
        "acl_rule": "acl_rule.j2",
        "site_device_intent": "site_device_intent.j2",
        "custom_config": "custom_config.j2",
    }
    return templates[intent.change_type]


def config_filename(intent: Intent) -> str:
    return f"{intent_slug(intent)}.eos"


def report_stem(intent: Intent) -> str:
    return intent_slug(intent)


def target_device_id(intent: Intent) -> str | None:
    return intent.targets.device_ids[0] if intent.targets.device_ids else None


def intent_risk(intent: Intent) -> str:
    if isinstance(intent, AddVlanIntent):
        return "Low for lab"
    if isinstance(intent, InterfaceConfigIntent):
        return "Medium: interface behavior can affect connected hosts"
    if isinstance(intent, BgpNeighborIntent):
        return "High: routing changes can affect reachability"
    if isinstance(intent, AclRuleIntent):
        return "High: policy changes can permit or block traffic"
    if isinstance(intent, SiteDeviceIntent):
        return "Inventory only"
    if isinstance(intent, CustomConfigIntent):
        return "High: free-form config — review every line before dry-run"
    return "Review required"


def lab_write_supported(intent: Intent) -> bool:
    return isinstance(intent, (AddVlanIntent, InterfaceConfigIntent, BgpNeighborIntent, AclRuleIntent, CustomConfigIntent))


def production_write_supported(intent: Intent) -> bool:
    return False


def rollback_config(intent: Intent) -> str:
    if isinstance(intent, AddVlanIntent):
        return f"no vlan {intent.vlan.id}\n"
    if isinstance(intent, InterfaceConfigIntent):
        # Lab-only rollback. Production rollback should use captured pre-change state.
        return f"default interface {intent.interface.name}\n"
    if isinstance(intent, BgpNeighborIntent):
        lines = [f"router bgp {intent.bgp.asn}"]
        for neighbor in intent.bgp.neighbors:
            lines.append(f"   no neighbor {neighbor.address}")
        return "\n".join(lines) + "\n"
    if isinstance(intent, AclRuleIntent):
        return f"ip access-list {intent.acl.name}\n   no {intent.acl.sequence}\n"
    if isinstance(intent, CustomConfigIntent):
        rollback = intent.custom.rollback_lines.strip()
        return f"{rollback}\n" if rollback else ""
    return ""


def verification_hint(intent: Intent) -> dict[str, Any]:
    if isinstance(intent, AddVlanIntent):
        return {"check": "vlan_exists", "params": {"vlan_id": intent.vlan.id, "name": intent.vlan.name}}
    if isinstance(intent, InterfaceConfigIntent):
        return {"check": "running_config_contains", "params": {"section": f"interface {intent.interface.name}"}}
    if isinstance(intent, BgpNeighborIntent):
        return {"check": "running_config_contains", "params": {"section": f"router bgp {intent.bgp.asn}"}}
    if isinstance(intent, AclRuleIntent):
        return {"check": "running_config_contains", "params": {"section": f"ip access-list {intent.acl.name}"}}
    if isinstance(intent, CustomConfigIntent):
        return {"check": "running_config_contains", "params": {"section": intent.custom.verify_contains.strip() or _custom_first_line(intent)}}
    return {"check": "source_of_truth_only", "params": {}}


def rollback_confidence(intent: Intent) -> dict[str, str]:
    if isinstance(intent, AddVlanIntent):
        return {"level": "high", "reason": "Exact inverse command. VLAN absence is verified after rollback in the lab."}
    if isinstance(intent, InterfaceConfigIntent):
        return {"level": "medium", "reason": "Resets the interface to defaults; does not restore captured pre-change state."}
    if isinstance(intent, BgpNeighborIntent):
        return {"level": "medium", "reason": "Removes the neighbors added here; does not restore prior neighbor settings."}
    if isinstance(intent, AclRuleIntent):
        return {"level": "medium", "reason": "Removes this sequence; does not restore a rule it may have replaced."}
    if isinstance(intent, SiteDeviceIntent):
        return {"level": "none", "reason": "Inventory record only. No device config to roll back."}
    if isinstance(intent, CustomConfigIntent):
        if intent.custom.rollback_lines.strip():
            return {"level": "medium", "reason": "Engineer-supplied rollback commands. The platform runs them but cannot prove they are the exact inverse."}
        return {"level": "none", "reason": "No rollback commands were supplied for this custom change."}
    return {"level": "unknown", "reason": "Rollback behavior is not defined for this change type."}


def blast_radius(intent: Intent) -> dict[str, Any]:
    devices = list(intent.targets.device_ids or [])
    if not devices and intent.targets.device_group:
        devices = [f"group:{intent.targets.device_group}"]
    objects: list[str] = []
    if isinstance(intent, AddVlanIntent):
        objects.append(f"VLAN {intent.vlan.id} ({intent.vlan.name})")
        if intent.vlan.svi and intent.vlan.svi.enabled:
            objects.append(f"SVI interface Vlan{intent.vlan.id}")
    elif isinstance(intent, InterfaceConfigIntent):
        objects.append(f"Interface {intent.interface.name}")
        if intent.interface.access_vlan:
            objects.append(f"Access VLAN {intent.interface.access_vlan}")
    elif isinstance(intent, BgpNeighborIntent):
        objects.append(f"BGP AS {intent.bgp.asn}")
        objects.extend(f"Neighbor {neighbor.address} (AS {neighbor.remote_as})" for neighbor in intent.bgp.neighbors)
    elif isinstance(intent, AclRuleIntent):
        objects.append(f"ACL {intent.acl.name} sequence {intent.acl.sequence}")
    elif isinstance(intent, SiteDeviceIntent):
        objects.append(f"Inventory record {intent.device.device_id}")
    elif isinstance(intent, CustomConfigIntent):
        lines = [line for line in intent.custom.config_lines.splitlines() if line.strip()]
        objects.append(f"{len(lines)} free-form config line{'s' if len(lines) != 1 else ''}")
        # Top-level config sections give reviewers a fast read of what is touched.
        sections = [line.strip() for line in lines if line and not line.startswith((" ", "\t", "!"))]
        objects.extend(sections[:8])
        if len(sections) > 8:
            objects.append(f"+{len(sections) - 8} more sections")
    return {
        "devices": devices,
        "device_count": len(devices),
        "objects": objects,
        "site": intent.site,
    }


def pre_post_checks(intent: Intent) -> dict[str, list[dict[str, Any]]]:
    """Pre/post check definitions per change type. `executable` means the platform can run it live today."""
    if isinstance(intent, AddVlanIntent):
        return {
            "pre": [
                {
                    "id": "vlan_absent",
                    "description": f"VLAN {intent.vlan.id} is not already configured on the target device.",
                    "executable": True,
                }
            ],
            "post": [
                {
                    "id": "vlan_present",
                    "description": f"VLAN {intent.vlan.id} exists with name {intent.vlan.name}.",
                    "executable": True,
                },
                {
                    "id": "state_cross_check",
                    "description": "Independent live-state collection confirms the VLAN.",
                    "executable": True,
                },
            ],
        }
    if isinstance(intent, InterfaceConfigIntent):
        return {
            "pre": [
                {
                    "id": "interface_exists",
                    "description": f"Interface {intent.interface.name} exists on the target device.",
                    "executable": False,
                    "note": "Live pre-check not wired yet.",
                }
            ],
            "post": [
                {
                    "id": "running_config_contains",
                    "description": f"Running config contains the interface {intent.interface.name} section.",
                    "executable": True,
                }
            ],
        }
    if isinstance(intent, BgpNeighborIntent):
        return {
            "pre": [
                {
                    "id": "bgp_neighbor_absent",
                    "description": "Neighbors are not already configured.",
                    "executable": False,
                    "note": "Live pre-check not wired yet.",
                }
            ],
            "post": [
                {
                    "id": "running_config_contains",
                    "description": f"Running config contains router bgp {intent.bgp.asn} with the neighbors.",
                    "executable": True,
                }
            ],
        }
    if isinstance(intent, AclRuleIntent):
        return {
            "pre": [
                {
                    "id": "acl_sequence_absent",
                    "description": f"ACL {intent.acl.name} sequence {intent.acl.sequence} is not already in use.",
                    "executable": False,
                    "note": "Live pre-check not wired yet.",
                }
            ],
            "post": [
                {
                    "id": "running_config_contains",
                    "description": f"Running config contains the ACL {intent.acl.name} rule.",
                    "executable": True,
                }
            ],
        }
    if isinstance(intent, CustomConfigIntent):
        needle = intent.custom.verify_contains.strip() or _custom_first_line(intent)
        return {
            "pre": [
                {
                    "id": "rollback_supplied",
                    "description": "Rollback commands are supplied (or no-rollback is explicitly acknowledged).",
                    "executable": True,
                }
            ],
            "post": [
                {
                    "id": "running_config_contains",
                    "description": f"Running config contains: {needle}",
                    "executable": True,
                }
            ],
        }
    return {
        "pre": [],
        "post": [
            {
                "id": "source_of_truth_updated",
                "description": "The source-of-truth record is written and loadable.",
                "executable": True,
            }
        ],
    }


def suggested_branch(intent: Intent) -> str:
    return f"change/{intent_slug(intent)}"


def plan_metadata(intent: Intent) -> dict[str, Any]:
    return {
        "change_type": intent.change_type,
        "label": CHANGE_TYPE_LABELS.get(intent.change_type, intent.change_type),
        "title": intent_title(intent),
        "slug": intent_slug(intent),
        "risk": intent_risk(intent),
        "target_device_id": target_device_id(intent),
        "lab_write_supported": lab_write_supported(intent),
        "production_write_supported": production_write_supported(intent),
        "verification": verification_hint(intent),
        "blast_radius": blast_radius(intent),
        "rollback": {
            "commands": rollback_config(intent),
            "confidence": rollback_confidence(intent),
        },
        "checks": pre_post_checks(intent),
        "suggested_branch": suggested_branch(intent),
    }
