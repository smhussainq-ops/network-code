"""Change-type registry — one spec per change type, registered once.

This is the single seam for adding a network use case. Instead of editing ~10
`isinstance`/`change_type` ladders scattered across models, orchestrator,
intent_utils, validation, and lab, a new change type registers ONE ChangeTypeSpec
here (plus a template, a UI-config catalog entry, and — for the two
instance-coupled concerns — a named policy check in validation.py and a named
verify method in lab.py, which the spec references by name to avoid import cycles).

The pure data + transforms (model, template, field-mapping build, slug/title,
risk, render scope, rollback, blast radius, pre/post checks) all live here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from netcode.models import (
    AclRuleIntent,
    AddVlanIntent,
    BgpNeighborIntent,
    CustomConfigIntent,
    Intent,
    InterfaceConfigIntent,
    NtpStandardizeIntent,
    OsUpgradeIntent,
    RoutingRedistributionIntent,
    SiteDeviceIntent,
)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-").lower() or "intent"


def _custom_first_line(intent: CustomConfigIntent) -> str:
    lines = [line.strip() for line in intent.custom.config_lines.splitlines() if line.strip()]
    return lines[0] if lines else "custom config"


@dataclass(frozen=True)
class ChangeTypeSpec:
    key: str
    label: str
    model: type
    template: str
    risk: str
    lab_write: bool
    # (common, values, device_id) -> mutates/returns the intent-shaped dict
    build: Callable[[dict, dict, str], dict]
    title: Callable[[Any], str]
    slug: Callable[[Any], str]
    rollback: Callable[[Any], str]
    rollback_confidence: Callable[[Any], dict]
    blast_objects: Callable[[Any], list]
    checks: Callable[[Any], dict]
    verification_hint: Callable[[Any], dict]
    # Names resolved at call time to avoid import cycles with validation/lab.
    policy_checks: list[str]          # StaticValidator method names
    verify_method: str               # AristaEOSLabAdapter method name
    allow_prefixes: list[str]        # render-scope allow-list ("" empty => free-form)
    block_carveouts: list[str] = field(default_factory=list)  # blocked fragments to un-block for this type
    production_write: bool = False


REGISTRY: dict[str, ChangeTypeSpec] = {}


def register(spec: ChangeTypeSpec) -> None:
    REGISTRY[spec.key] = spec


def spec_for(intent_or_type: Any) -> ChangeTypeSpec:
    key = intent_or_type if isinstance(intent_or_type, str) else intent_or_type.change_type
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported change_type: {key!r}") from exc


def change_type_keys() -> list[str]:
    return list(REGISTRY.keys())


# ── add_vlan ───────────────────────────────────────────────────────────────
def _build_add_vlan(common: dict, values: dict, device_id: str) -> dict:
    common["vlan"] = {
        "id": int(values.get("vlan_id", 90)),
        "name": str(values.get("name", "GUEST_WIFI")),
        "subnet": str(values.get("subnet", "10.42.90.0/24")),
        "purpose": str(values.get("purpose", "guest")),
        "svi": {"enabled": bool(values.get("svi_enabled", False)), "gateway_ip": values.get("gateway_ip") or None},
    }
    return common


def _blast_add_vlan(intent: AddVlanIntent) -> list:
    objects = [f"VLAN {intent.vlan.id} ({intent.vlan.name})"]
    if intent.vlan.svi and intent.vlan.svi.enabled:
        objects.append(f"SVI interface Vlan{intent.vlan.id}")
    return objects


register(ChangeTypeSpec(
    key="add_vlan", label="Add VLAN", model=AddVlanIntent, template="add_vlan.j2",
    risk="Low for lab", lab_write=True, build=_build_add_vlan,
    title=lambda i: f"Add VLAN {i.vlan.id} ({i.vlan.name})",
    slug=lambda i: f"{i.site}-add-vlan-{i.vlan.id}",
    rollback=lambda i: f"no vlan {i.vlan.id}\n",
    rollback_confidence=lambda i: {"level": "high", "reason": "Exact inverse command. VLAN absence is verified after rollback in the lab."},
    blast_objects=_blast_add_vlan,
    checks=lambda i: {
        "pre": [{"id": "vlan_absent", "description": f"VLAN {i.vlan.id} is not already configured on the target device.", "executable": True}],
        "post": [
            {"id": "vlan_present", "description": f"VLAN {i.vlan.id} exists with name {i.vlan.name}.", "executable": True},
            {"id": "state_cross_check", "description": "Independent live-state collection confirms the VLAN.", "executable": True},
        ],
    },
    verification_hint=lambda i: {"check": "vlan_exists", "params": {"vlan_id": i.vlan.id, "name": i.vlan.name}},
    policy_checks=["_vlan_policy", "_subnet_overlap", "_segmentation"],
    verify_method="_verify_add_vlan",
    allow_prefixes=["vlan ", "   name ", "interface Vlan", "   description ", "   ip address "],
))


# ── interface_config ───────────────────────────────────────────────────────
def _build_interface(common: dict, values: dict, device_id: str) -> dict:
    trunk_raw = str(values.get("trunk_allowed_vlans", "")).strip()
    trunk_vlans = [int(v.strip()) for v in trunk_raw.split(",") if v.strip()]
    common["interface"] = {
        "name": str(values.get("interface", "Ethernet1")),
        "description": str(values.get("description", "")),
        "enabled": bool(values.get("enabled", True)),
        "apply_scope": str(values.get("apply_scope", "full")),
        "mode": str(values.get("mode", "access")),
        "access_vlan": int(values["access_vlan"]) if values.get("access_vlan") not in (None, "") else None,
        "trunk_allowed_vlans": trunk_vlans,
        "ip_address": values.get("ip_address") or None,
    }
    return common


def _blast_interface(intent: InterfaceConfigIntent) -> list:
    objects = [f"Interface {intent.interface.name}"]
    if intent.interface.access_vlan:
        objects.append(f"Access VLAN {intent.interface.access_vlan}")
    return objects


def _rollback_interface(intent: InterfaceConfigIntent) -> str:
    if intent.interface.apply_scope == "admin_state":
        inverse = "shutdown" if intent.interface.enabled else "no shutdown"
        return f"interface {intent.interface.name}\n   {inverse}\n"
    return f"default interface {intent.interface.name}\n"


def _rollback_confidence_interface(intent: InterfaceConfigIntent) -> dict:
    if intent.interface.apply_scope == "admin_state":
        return {
            "level": "high",
            "reason": "Restores only the inverse administrative state without changing unrelated interface configuration.",
        }
    return {
        "level": "medium",
        "reason": "Resets the interface to defaults; full interface changes require explicit review of the generated rollback.",
    }


register(ChangeTypeSpec(
    key="interface_config", label="Interface Config", model=InterfaceConfigIntent, template="interface_config.j2",
    risk="Medium: interface behavior can affect connected hosts", lab_write=True, build=_build_interface,
    title=lambda i: f"Configure {i.interface.name}",
    slug=lambda i: f"{i.site}-interface-{safe_name(i.interface.name)}",
    rollback=_rollback_interface,
    rollback_confidence=_rollback_confidence_interface,
    blast_objects=_blast_interface,
    checks=lambda i: {
        "pre": [{"id": "interface_exists", "description": f"Interface {i.interface.name} exists on the target device.", "executable": False, "note": "Live pre-check not wired yet."}],
        "post": [{"id": "running_config_contains", "description": f"Running config contains the interface {i.interface.name} section.", "executable": True}],
    },
    verification_hint=lambda i: {"check": "running_config_contains", "params": {"section": f"interface {i.interface.name}"}},
    policy_checks=["_interface_policy"], verify_method="_verify_interface",
    allow_prefixes=["interface ", "   description ", "   switchport ", "   no switchport", "   ip address ", "   shutdown", "   no shutdown"],
))


# ── bgp_neighbor ───────────────────────────────────────────────────────────
def _build_bgp(common: dict, values: dict, device_id: str) -> dict:
    common["bgp"] = {
        "asn": int(values.get("asn", 65001)),
        "router_id": values.get("router_id") or None,
        "neighbors": [{
            "address": str(values.get("neighbor", "10.255.0.2")),
            "remote_as": int(values.get("remote_as", 65002)),
            "description": str(values.get("description", "")),
            "update_source": values.get("update_source") or None,
            "shutdown": bool(values.get("shutdown", False)),
        }],
    }
    return common


def _blast_bgp(intent: BgpNeighborIntent) -> list:
    objects = [f"BGP AS {intent.bgp.asn}"]
    objects.extend(f"Neighbor {n.address} (AS {n.remote_as})" for n in intent.bgp.neighbors)
    return objects


def _rollback_bgp(intent: BgpNeighborIntent) -> str:
    lines = [f"router bgp {intent.bgp.asn}"]
    for neighbor in intent.bgp.neighbors:
        lines.append(f"   no neighbor {neighbor.address}")
    return "\n".join(lines) + "\n"


register(ChangeTypeSpec(
    key="bgp_neighbor", label="BGP Neighbor", model=BgpNeighborIntent, template="bgp_neighbor.j2",
    risk="High: routing changes can affect reachability", lab_write=True, build=_build_bgp,
    title=lambda i: f"Configure BGP neighbor {i.bgp.neighbors[0].address}",
    slug=lambda i: f"{i.site}-bgp-{i.bgp.asn}-neighbor-{safe_name(i.bgp.neighbors[0].address)}",
    rollback=_rollback_bgp,
    rollback_confidence=lambda i: {"level": "medium", "reason": "Removes the neighbors added here; does not restore prior neighbor settings."},
    blast_objects=_blast_bgp,
    checks=lambda i: {
        "pre": [{"id": "bgp_neighbor_absent", "description": "Neighbors are not already configured.", "executable": False, "note": "Live pre-check not wired yet."}],
        "post": [{"id": "running_config_contains", "description": f"Running config contains router bgp {i.bgp.asn} with the neighbors.", "executable": True}],
    },
    verification_hint=lambda i: {"check": "running_config_contains", "params": {"section": f"router bgp {i.bgp.asn}"}},
    policy_checks=["_bgp_policy"], verify_method="_verify_bgp",
    allow_prefixes=["router bgp ", "   router-id ", "   neighbor ", "   no neighbor "],
    block_carveouts=["router bgp"],
))


# ── routing_redistribution ────────────────────────────────────────────────
def _build_routing_redistribution(common: dict, values: dict, device_id: str) -> dict:
    prefixes = values.get("prefixes") or []
    if isinstance(prefixes, str):
        prefixes = [item.strip() for item in prefixes.split(",") if item.strip()]
    common["redistribution"] = {
        "from_protocol": str(values.get("from_protocol", "bgp")),
        "to_protocol": str(values.get("to_protocol", "ospf")),
        "target_process": str(values.get("target_process", "1")),
        "route_map": str(values.get("route_map", "BGP-TO-OSPF")),
        "prefix_list": str(values.get("prefix_list", "APPROVED-BGP-TO-OSPF")),
        "prefixes": list(prefixes),
        "route_tag": int(values.get("route_tag", 65000)),
    }
    reverse = values.get("reverse_redistribution")
    if isinstance(reverse, dict):
        reverse_prefixes = reverse.get("prefixes") or []
        if isinstance(reverse_prefixes, str):
            reverse_prefixes = [item.strip() for item in reverse_prefixes.split(",") if item.strip()]
        common["reverse_redistribution"] = {
            "from_protocol": str(reverse.get("from_protocol", "ospf")),
            "to_protocol": str(reverse.get("to_protocol", "bgp")),
            "target_process": str(reverse.get("target_process", "")),
            "route_map": str(reverse.get("route_map", "")),
            "prefix_list": str(reverse.get("prefix_list", "")),
            "prefixes": list(reverse_prefixes),
            "route_tag": reverse.get("route_tag"),
        }
    reachability_checks = values.get("reachability_checks")
    if isinstance(reachability_checks, list):
        common["reachability_checks"] = [dict(item) for item in reachability_checks if isinstance(item, dict)]
    return common


def redistribution_items(intent: RoutingRedistributionIntent) -> list:
    items = [intent.redistribution]
    if intent.reverse_redistribution is not None:
        items.append(intent.reverse_redistribution)
    return items


def _rollback_routing_redistribution(intent: RoutingRedistributionIntent) -> str:
    blocks: list[str] = []
    for item in reversed(redistribution_items(intent)):
        if item.to_protocol == "bgp":
            blocks.append(
                f"router bgp {item.target_process}\n"
                "   address-family ipv4\n"
                f"      no redistribute {item.from_protocol} route-map {item.route_map}\n"
                f"no route-map {item.route_map} permit 10\n"
                f"no ip prefix-list {item.prefix_list}\n"
            )
        else:
            blocks.append(
                f"router ospf {item.target_process}\n"
                f"   no redistribute {item.from_protocol} route-map {item.route_map}\n"
                f"no route-map {item.route_map} permit 10\n"
                f"no ip prefix-list {item.prefix_list}\n"
            )
    return "".join(blocks)


register(ChangeTypeSpec(
    key="routing_redistribution",
    label="Controlled Route Redistribution",
    model=RoutingRedistributionIntent,
    template="routing_redistribution.j2",
    risk="High: changes route propagation across protocol boundaries",
    lab_write=True,
    build=_build_routing_redistribution,
    title=lambda i: (
        "Configure bidirectional route exchange"
        if i.reverse_redistribution is not None
        else f"Configure {i.redistribution.from_protocol.upper()} to {i.redistribution.to_protocol.upper()} redistribution"
    ),
    slug=lambda i: f"{i.site}-{i.redistribution.from_protocol}-to-{i.redistribution.to_protocol}-{safe_name(i.redistribution.route_map)}",
    rollback=_rollback_routing_redistribution,
    rollback_confidence=lambda i: {
        "level": "high",
        "reason": "Removes the exact redistribution statement, route-map sequence, and dedicated prefix list generated by this intent.",
    },
    blast_objects=lambda i: [
        value
        for item in redistribution_items(i)
        for value in (
            f"{item.from_protocol.upper()}→{item.to_protocol.upper()} boundary",
            f"Route-map {item.route_map}",
            f"Prefix-list {item.prefix_list}",
        )
    ],
    checks=lambda i: {
        "pre": [
            {
                "id": "approved_prefix_scope",
                "description": "Every redistributed prefix is explicitly listed; default-route redistribution is forbidden.",
                "executable": True,
            }
        ],
        "post": [
            {
                "id": "redistribution_present",
                "description": "Every approved route-exchange direction references its dedicated route-map.",
                "executable": True,
            }
        ],
    },
    verification_hint=lambda i: {
        "check": "redistribution_present",
        "params": {
            "boundaries": [
                {
                    "source_protocol": item.from_protocol,
                    "target_protocol": item.to_protocol,
                    "target_process": item.target_process,
                    "route_map": item.route_map,
                }
                for item in redistribution_items(i)
            ],
        },
    },
    policy_checks=["_routing_redistribution_policy"],
    verify_method="_verify_redistribution",
    allow_prefixes=[
        "ip prefix-list ",
        "route-map ",
        "   match ip address prefix-list ",
        "   set tag ",
        "router ospf ",
        "   redistribute bgp route-map ",
        "router bgp ",
        "   address-family ipv4",
        "      redistribute ospf route-map ",
    ],
    block_carveouts=[
        "router ospf", "redistribute bgp", "router bgp", "address-family ipv4",
        "redistribute ospf", "route-map", "ip prefix-list",
    ],
))


# ── acl_rule ───────────────────────────────────────────────────────────────
def _build_acl(common: dict, values: dict, device_id: str) -> dict:
    common["acl"] = {
        "name": str(values.get("acl_name", "NETCODE_TEST")),
        "sequence": int(values.get("sequence", 10)),
        "action": str(values.get("action", "permit")),
        "protocol": str(values.get("protocol", "ip")),
        "source": str(values.get("source", "any")),
        "destination": str(values.get("destination", "any")),
        "destination_port": values.get("destination_port") or None,
        "remark": str(values.get("remark", "")),
    }
    return common


register(ChangeTypeSpec(
    key="acl_rule", label="ACL Rule", model=AclRuleIntent, template="acl_rule.j2",
    risk="High: policy changes can permit or block traffic", lab_write=True, build=_build_acl,
    title=lambda i: f"Update ACL {i.acl.name} sequence {i.acl.sequence}",
    slug=lambda i: f"{i.site}-acl-{safe_name(i.acl.name)}-{i.acl.sequence}",
    rollback=lambda i: f"ip access-list {i.acl.name}\n   no {i.acl.sequence}\n",
    rollback_confidence=lambda i: {"level": "medium", "reason": "Removes this sequence; does not restore a rule it may have replaced."},
    blast_objects=lambda i: [f"ACL {i.acl.name} sequence {i.acl.sequence}"],
    checks=lambda i: {
        "pre": [{"id": "acl_sequence_absent", "description": f"ACL {i.acl.name} sequence {i.acl.sequence} is not already in use.", "executable": False, "note": "Live pre-check not wired yet."}],
        "post": [{"id": "running_config_contains", "description": f"Running config contains the ACL {i.acl.name} rule.", "executable": True}],
    },
    verification_hint=lambda i: {"check": "running_config_contains", "params": {"section": f"ip access-list {i.acl.name}"}},
    policy_checks=["_acl_policy"], verify_method="_verify_acl",
    allow_prefixes=["ip access-list ", "   remark ", "   permit ", "   deny "],
    block_carveouts=["ip access-list"],
))


# ── site_device_intent ─────────────────────────────────────────────────────
def _build_site(common: dict, values: dict, device_id: str) -> dict:
    device_name = str(values.get("new_device_id") or device_id)
    common["targets"] = {"device_ids": [device_name], "device_group": values.get("device_group", "stores")}
    common["device"] = {
        "device_id": device_name,
        "role": str(values.get("role", "access-switch")),
        "platform": str(values.get("platform", "arista_eos")),
        "management_ip": str(values.get("management_ip", values.get("host", "172.100.1.41"))),
        "groups": [g.strip() for g in str(values.get("groups", "stores,access-switches")).split(",") if g.strip()],
        "notes": str(values.get("notes", "")),
    }
    return common


register(ChangeTypeSpec(
    key="site_device_intent", label="Site / Device Intent", model=SiteDeviceIntent, template="site_device_intent.j2",
    risk="Inventory only", lab_write=False, build=_build_site,
    title=lambda i: f"Register {i.device.device_id} as {i.device.role}",
    slug=lambda i: f"{i.site}-device-{safe_name(i.device.device_id)}",
    rollback=lambda i: "",
    rollback_confidence=lambda i: {"level": "none", "reason": "Inventory record only. No device config to roll back."},
    blast_objects=lambda i: [f"Inventory record {i.device.device_id}"],
    checks=lambda i: {"pre": [], "post": [{"id": "source_of_truth_updated", "description": "The source-of-truth record is written and loadable.", "executable": True}]},
    verification_hint=lambda i: {"check": "source_of_truth_only", "params": {}},
    policy_checks=["_site_policy"], verify_method="_verify_unsupported",
    allow_prefixes=["! "],
))


# ── custom_config ──────────────────────────────────────────────────────────
def _build_custom(common: dict, values: dict, device_id: str) -> dict:
    common["custom"] = {
        "config_lines": str(values.get("config_lines", "")),
        "rollback_lines": str(values.get("rollback_lines", "")),
        "verify_contains": str(values.get("verify_contains", "")),
        "description": str(values.get("description", "")),
        "acknowledge_no_rollback": bool(values.get("acknowledge_no_rollback", False)),
    }
    return common


def _blast_custom(intent: CustomConfigIntent) -> list:
    lines = [line for line in intent.custom.config_lines.splitlines() if line.strip()]
    objects = [f"{len(lines)} free-form config line{'s' if len(lines) != 1 else ''}"]
    sections = [line.strip() for line in lines if line and not line.startswith((" ", "\t", "!"))]
    objects.extend(sections[:8])
    if len(sections) > 8:
        objects.append(f"+{len(sections) - 8} more sections")
    return objects


def _rollback_confidence_custom(intent: CustomConfigIntent) -> dict:
    if intent.custom.rollback_lines.strip():
        return {"level": "medium", "reason": "Engineer-supplied rollback commands. The platform runs them but cannot prove they are the exact inverse."}
    return {"level": "none", "reason": "No rollback commands were supplied for this custom change."}


def _checks_custom(intent: CustomConfigIntent) -> dict:
    needle = intent.custom.verify_contains.strip() or _custom_first_line(intent)
    return {
        "pre": [{"id": "rollback_supplied", "description": "Rollback commands are supplied (or no-rollback is explicitly acknowledged).", "executable": True}],
        "post": [{"id": "running_config_contains", "description": f"Running config contains: {needle}", "executable": True}],
    }


register(ChangeTypeSpec(
    key="custom_config", label="Custom Config", model=CustomConfigIntent, template="custom_config.j2",
    risk="High: free-form config — review every line before dry-run", lab_write=True, build=_build_custom,
    title=lambda i: f"Custom config: {i.custom.description or _custom_first_line(i)}",
    slug=lambda i: f"{i.site}-custom-{safe_name(i.custom.description or _custom_first_line(i))[:32]}",
    rollback=lambda i: f"{i.custom.rollback_lines.strip()}\n" if i.custom.rollback_lines.strip() else "",
    rollback_confidence=_rollback_confidence_custom,
    blast_objects=_blast_custom, checks=_checks_custom,
    verification_hint=lambda i: {"check": "running_config_contains", "params": {"section": i.custom.verify_contains.strip() or _custom_first_line(i)}},
    policy_checks=["_custom_config_policy"], verify_method="_verify_custom",
    allow_prefixes=[],
))


# ── ntp_standardize ────────────────────────────────────────────────────────
def _build_ntp(common: dict, values: dict, device_id: str) -> dict:
    raw = values.get("servers", "")
    servers = [s.strip() for s in str(raw).replace("\n", ",").split(",") if s.strip()] if not isinstance(raw, list) else [str(s).strip() for s in raw if str(s).strip()]
    common["ntp"] = {"servers": servers, "prefer_first": bool(values.get("prefer_first", True))}
    return common


def _rollback_ntp(intent: NtpStandardizeIntent) -> str:
    return "".join(f"no ntp server {server}\n" for server in intent.ntp.servers)


register(ChangeTypeSpec(
    key="ntp_standardize", label="NTP Standardization", model=NtpStandardizeIntent, template="ntp_standardize.j2",
    risk="Low: additive time-source configuration", lab_write=True, build=_build_ntp,
    title=lambda i: f"Standardize NTP ({len(i.ntp.servers)} approved server{'s' if len(i.ntp.servers) != 1 else ''})",
    slug=lambda i: f"{i.site}-ntp-standardize",
    rollback=_rollback_ntp,
    rollback_confidence=lambda i: {"level": "high", "reason": "Exact inverse: removes only the servers this change added."},
    blast_objects=lambda i: [f"NTP server {server}" for server in i.ntp.servers],
    checks=lambda i: {
        "pre": [{"id": "ntp_reachability", "description": "Approved NTP servers should be reachable from the device.", "executable": False, "note": "Live pre-check not wired yet."}],
        "post": [{"id": "ntp_servers_present", "description": f"Running config lists all {len(i.ntp.servers)} approved NTP servers.", "executable": True}],
    },
    verification_hint=lambda i: {"check": "running_config_contains", "params": {"section": f"ntp server {i.ntp.servers[0]}"}},
    policy_checks=["_ntp_policy"], verify_method="_verify_ntp",
    allow_prefixes=["ntp server "],
))


# ── os_upgrade ─────────────────────────────────────────────────────────────
def _build_os_upgrade(common: dict, values: dict, device_id: str) -> dict:
    common["os_upgrade"] = {
        "image": str(values.get("image", "EOS-4.35.1F.swi")),
        "target_version": str(values.get("target_version", "4.35.1F")),
        "md5": str(values.get("md5", "")),
        "image_uri": str(values.get("image_uri", "")),
        "current_version": str(values.get("current_version", "")),
        "rollback_image": str(values.get("rollback_image", "")),
        "maintenance_window": str(values.get("maintenance_window", "")),
        "canary_size": int(values.get("canary_size", 1)),
        "batch_size": int(values.get("batch_size", 5)),
        "verify_bgp": bool(values.get("verify_bgp", True)),
    }
    return common


def _rollback_os_upgrade(intent: OsUpgradeIntent) -> str:
    lines = [f"no boot system flash:{intent.os_upgrade.image}"]
    if intent.os_upgrade.rollback_image:
        lines.append(f"boot system flash:{intent.os_upgrade.rollback_image}")
    lines.append("! reload is a separate approved maintenance-window action")
    return "\n".join(lines) + "\n"


register(ChangeTypeSpec(
    key="os_upgrade", label="EOS OS Upgrade", model=OsUpgradeIntent, template="os_upgrade.j2",
    risk="High: staged software upgrade, reload gated by maintenance approval", lab_write=True, build=_build_os_upgrade,
    title=lambda i: f"Stage EOS {i.os_upgrade.target_version} ({i.os_upgrade.image})",
    slug=lambda i: f"{i.site}-os-upgrade-{safe_name(i.os_upgrade.target_version)}",
    rollback=_rollback_os_upgrade,
    rollback_confidence=lambda i: {
        "level": "medium" if i.os_upgrade.rollback_image else "low",
        "reason": (
            "Reverts the staged boot image to the supplied rollback image; reload still requires approval."
            if i.os_upgrade.rollback_image
            else "Removes only the newly staged boot image; previous boot image was not supplied."
        ),
    },
    blast_objects=lambda i: [
        f"Target EOS version {i.os_upgrade.target_version}",
        f"Image {i.os_upgrade.image}",
        f"Maintenance window {i.os_upgrade.maintenance_window}",
        f"Canary {i.os_upgrade.canary_size}, batch {i.os_upgrade.batch_size}",
    ],
    checks=lambda i: {
        "pre": [
            {"id": "version_precheck", "description": "Collect current EOS version and boot image before staging.", "executable": True},
            {"id": "md5_verify", "description": f"Verify image MD5 equals {i.os_upgrade.md5}.", "executable": True},
            {"id": "maintenance_window", "description": f"Reload requires approved window: {i.os_upgrade.maintenance_window}.", "executable": True},
        ],
        "post": [
            {"id": "boot_variable_staged", "description": f"Boot variable points to {i.os_upgrade.image}; reload is not rendered in this plan.", "executable": True},
            {"id": "post_reload_version", "description": f"After approved canary reload, verify EOS version {i.os_upgrade.target_version}.", "executable": True},
            {"id": "bgp_after_reload", "description": "After reload, verify BGP neighbors recover before batch promotion.", "executable": bool(i.os_upgrade.verify_bgp)},
        ],
    },
    verification_hint=lambda i: {"check": "running_config_contains", "params": {"section": f"boot system flash:{i.os_upgrade.image}"}},
    policy_checks=["_os_upgrade_policy"], verify_method="_verify_os_upgrade",
    allow_prefixes=["! ", "boot system flash:"],
))
