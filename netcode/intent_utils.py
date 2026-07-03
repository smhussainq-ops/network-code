"""Desired-state intent helpers."""

from __future__ import annotations

import re
from typing import Any

from netcode.models import (
    AclRuleIntent,
    AddVlanIntent,
    BgpNeighborIntent,
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
}


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
    return f"{intent.site}-{safe_name(intent.change_type)}"


def template_for_intent(intent: Intent) -> str:
    templates = {
        "add_vlan": "add_vlan.j2",
        "interface_config": "interface_config.j2",
        "bgp_neighbor": "bgp_neighbor.j2",
        "acl_rule": "acl_rule.j2",
        "site_device_intent": "site_device_intent.j2",
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
    return "Review required"


def lab_write_supported(intent: Intent) -> bool:
    return isinstance(intent, (AddVlanIntent, InterfaceConfigIntent, BgpNeighborIntent, AclRuleIntent))


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
    return {"check": "source_of_truth_only", "params": {}}


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
    }
