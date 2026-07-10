"""Editable UI and workflow configuration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netcode.paths import WorkspacePaths
from netcode.yamlio import read_yaml, write_yaml


SENSITIVE_KEYS = {"password", "secret", "token", "api_token", "private_key"}


DEFAULT_UI_CONFIG: dict[str, Any] = {
    "version": 1,
    "git": {
        "repo_url": "https://github.com/smhussainq-ops/network-code.git",
        "branch": "main",
        "default_commit_message": "Describe network change",
        "artifact_globs": ["intents/", "rendered/", "reports/", "inventories/", "policies/", "templates/"],
    },
    "source_of_truth": {
        "provider": "local_yaml",
        "inventory_path": "inventories/lab.yaml",
        "policy_path": "policies/invariants.yaml",
        "template_dir": "templates",
        "write_imports": True,
        "netbox": {"url": "", "token": ""},
    },
    "credentials": {
        "profile": "lab-default",
        "username": "",
        "port": 22,
        "password_storage": "never_persist_passwords",
    },
    "discovery": {
        "defaults": {
            "host": "172.100.1.41",
            "platform": "arista_eos",
            "device_id": "v2-store1",
            "site": "store-1842",
            "groups": ["stores", "access-switches"],
            "port": 22,
            "username": "",
        },
        "vendor_options": [
            ["", "Auto detect with Rez"],
            ["arista_eos", "Arista EOS"],
            ["aruba_aoscx", "Aruba AOS-CX"],
            ["cisco_asa", "Cisco ASA"],
            ["cisco_ios", "Cisco IOS/XE"],
            ["cisco_nxos", "Cisco NX-OS"],
            ["cisco_sdwan", "Cisco SD-WAN"],
            ["fortinet", "Fortinet"],
            ["juniper_junos", "Juniper Junos"],
            ["meraki", "Meraki"],
            ["nokia_srl", "Nokia SR Linux"],
            ["palo_alto", "Palo Alto"],
        ],
    },
    "desired_state": {
        "selected_change_type": "add_vlan",
        "common": {
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "lab-engineer",
            "ticket_id": "",
        },
        "change_types": {
            "add_vlan": {
                "label": "Add VLAN",
                "outcome": "Create a Layer 2 segment and optional SVI.",
                "risk": "Low for lab",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "vlan_id", "label": "VLAN ID", "type": "number", "value": 90, "min": 2, "max": 4094},
                    {"name": "name", "label": "Name", "type": "text", "value": "GUEST_WIFI"},
                    {"name": "subnet", "label": "Subnet", "type": "text", "value": "10.42.90.0/24"},
                    {"name": "purpose", "label": "Purpose", "type": "text", "value": "guest"},
                    {"name": "svi_enabled", "label": "Create SVI", "type": "checkbox", "value": False},
                    {"name": "gateway_ip", "label": "Gateway IP", "type": "text", "value": "", "placeholder": "auto from subnet"},
                    {"name": "pci_reachable", "label": "PCI reachable", "type": "checkbox", "value": False},
                ],
            },
            "ntp_standardize": {
                "label": "NTP Standardization",
                "outcome": "Ensure every device uses the approved time sources.",
                "risk": "Low: additive time-source configuration",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "servers", "label": "Approved NTP servers", "type": "text", "value": "10.42.0.10, 10.42.0.11", "placeholder": "comma separated IPs/hostnames"},
                    {"name": "prefer_first", "label": "Prefer first server", "type": "checkbox", "value": True},
                ],
            },
            "os_upgrade": {
                "label": "EOS OS Upgrade",
                "outcome": "Stage an EOS image, verify MD5, set boot image, and require human-approved maintenance/canary reload.",
                "risk": "High: staged software upgrade, reload gated by maintenance approval",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "image", "label": "EOS image", "type": "text", "value": "EOS-4.35.1F.swi"},
                    {"name": "target_version", "label": "Target version", "type": "text", "value": "4.35.1F"},
                    {"name": "md5", "label": "Expected MD5", "type": "text", "value": "0123456789abcdef0123456789abcdef"},
                    {"name": "image_uri", "label": "Image source", "type": "text", "value": "", "placeholder": "runner-local repository or HTTPS artifact URL"},
                    {"name": "rollback_image", "label": "Rollback image", "type": "text", "value": "", "placeholder": "previous EOS image, if known"},
                    {"name": "maintenance_window", "label": "Maintenance window", "type": "text", "value": "Sunday 02:00-04:00 UTC"},
                    {"name": "canary_size", "label": "Canary devices", "type": "number", "value": 1, "min": 1},
                    {"name": "batch_size", "label": "Batch size", "type": "number", "value": 5, "min": 1},
                    {"name": "verify_bgp", "label": "Verify BGP after reload", "type": "checkbox", "value": True},
                ],
            },
            "interface_config": {
                "label": "Interface Config",
                "outcome": "Configure access, trunk, or routed interface intent.",
                "risk": "Medium: can affect connected hosts",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "interface", "label": "Interface", "type": "text", "value": "Ethernet1"},
                    {"name": "description", "label": "Description", "type": "text", "value": "NETCODE_LAB_ENDPOINT"},
                    {
                        "name": "mode",
                        "label": "Mode",
                        "type": "select",
                        "value": "access",
                        "options": [["access", "Access"], ["trunk", "Trunk"], ["routed", "Routed"]],
                    },
                    {"name": "access_vlan", "label": "Access VLAN", "type": "number", "value": 90, "min": 2, "max": 4094},
                    {"name": "trunk_allowed_vlans", "label": "Trunk VLANs", "type": "text", "value": "90,91", "placeholder": "comma separated"},
                    {"name": "ip_address", "label": "Routed IP", "type": "text", "value": "", "placeholder": "10.0.0.1/31"},
                    {"name": "enabled", "label": "No shutdown", "type": "checkbox", "value": True},
                ],
            },
            "bgp_neighbor": {
                "label": "BGP Neighbor",
                "outcome": "Define routing adjacency intent and generated router BGP commands.",
                "risk": "High: can affect reachability",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "asn", "label": "Local ASN", "type": "number", "value": 65001, "min": 1},
                    {"name": "router_id", "label": "Router ID", "type": "text", "value": "10.255.0.1"},
                    {"name": "neighbor", "label": "Neighbor IP", "type": "text", "value": "10.255.0.2"},
                    {"name": "remote_as", "label": "Remote ASN", "type": "number", "value": 65002, "min": 1},
                    {"name": "description", "label": "Description", "type": "text", "value": "NETCODE_LAB_PEER"},
                    {"name": "update_source", "label": "Update source", "type": "text", "value": "", "placeholder": "Loopback0"},
                    {"name": "shutdown", "label": "Keep neighbor shutdown", "type": "checkbox", "value": False},
                ],
            },
            "routing_redistribution": {
                "label": "Controlled Route Redistribution",
                "outcome": "Propagate only approved prefix classes across a BGP-to-OSPF boundary.",
                "risk": "High: changes route propagation; route-map, dry-run, approval, and rollback required",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "from_protocol", "label": "Source protocol", "type": "select", "value": "bgp", "options": [["bgp", "BGP"]]},
                    {"name": "to_protocol", "label": "Target protocol", "type": "select", "value": "ospf", "options": [["ospf", "OSPF"]]},
                    {"name": "target_process", "label": "OSPF process", "type": "text", "value": "1"},
                    {"name": "route_map", "label": "Route-map", "type": "text", "value": "BGP-TO-OSPF"},
                    {"name": "prefix_list", "label": "Prefix-list", "type": "text", "value": "APPROVED-BGP-TO-OSPF"},
                    {"name": "prefixes", "label": "Approved prefixes", "type": "text", "value": "10.0.0.0/8", "placeholder": "comma separated, no default route"},
                    {"name": "route_tag", "label": "Route tag", "type": "number", "value": 65000, "min": 1},
                ],
            },
            "acl_rule": {
                "label": "ACL Rule",
                "outcome": "Add a sequenced permit or deny rule to a named ACL.",
                "risk": "High: can permit or block traffic",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {"name": "acl_name", "label": "ACL name", "type": "text", "value": "NETCODE_LAB"},
                    {"name": "sequence", "label": "Sequence", "type": "number", "value": 10, "min": 1},
                    {"name": "action", "label": "Action", "type": "select", "value": "permit", "options": [["permit", "Permit"], ["deny", "Deny"]]},
                    {
                        "name": "protocol",
                        "label": "Protocol",
                        "type": "select",
                        "value": "ip",
                        "options": [["ip", "IP"], ["tcp", "TCP"], ["udp", "UDP"], ["icmp", "ICMP"]],
                    },
                    {"name": "source", "label": "Source", "type": "text", "value": "any"},
                    {"name": "destination", "label": "Destination", "type": "text", "value": "any"},
                    {"name": "destination_port", "label": "Destination port", "type": "text", "value": "", "placeholder": "443"},
                    {"name": "remark", "label": "Remark", "type": "text", "value": "managed by netcode"},
                ],
            },
            "site_device_intent": {
                "label": "Site / Device Intent",
                "outcome": "Capture source-of-truth intent before config exists.",
                "risk": "Inventory only",
                "lab_write_supported": False,
                "production_write_supported": False,
                "fields": [
                    {"name": "new_device_id", "label": "New device ID", "type": "text", "value": "v2-store4"},
                    {"name": "role", "label": "Role", "type": "text", "value": "access-switch"},
                    {
                        "name": "platform",
                        "label": "Platform",
                        "type": "select",
                        "value": "arista_eos",
                        "options": [["arista_eos", "Arista EOS"], ["cisco_ios", "Cisco IOS/XE"], ["cisco_nxos", "Cisco NX-OS"], ["juniper_junos", "Juniper Junos"]],
                    },
                    {"name": "management_ip", "label": "Management IP", "type": "text", "value": "172.100.1.44"},
                    {"name": "groups", "label": "Groups", "type": "text", "value": "stores,access-switches"},
                    {"name": "notes", "label": "Notes", "type": "textarea", "value": "source-of-truth proposal only"},
                ],
            },
            "custom_config": {
                "label": "Custom Config",
                "outcome": "Push exactly the config you paste — any feature — gated by the same plan, validation, dry-run, and rollback flow.",
                "risk": "High: free-form config — review every line",
                "lab_write_supported": True,
                "production_write_supported": False,
                "fields": [
                    {
                        "name": "description",
                        "label": "What is this change?",
                        "type": "text",
                        "value": "",
                        "placeholder": "e.g. NTP servers for store-1842",
                    },
                    {
                        "name": "config_lines",
                        "label": "Config to push (exact CLI lines)",
                        "type": "textarea",
                        "value": "",
                        "placeholder": "ntp server 10.42.0.10\nntp server 10.42.0.11",
                    },
                    {
                        "name": "rollback_lines",
                        "label": "Rollback commands (required unless acknowledged below)",
                        "type": "textarea",
                        "value": "",
                        "placeholder": "no ntp server 10.42.0.10\nno ntp server 10.42.0.11",
                    },
                    {
                        "name": "verify_contains",
                        "label": "Verify after apply: running-config must contain",
                        "type": "text",
                        "value": "",
                        "placeholder": "ntp server 10.42.0.10",
                    },
                    {
                        "name": "acknowledge_no_rollback",
                        "label": "I accept this change has no rollback",
                        "type": "checkbox",
                        "value": False,
                    },
                ],
            },
        },
    },
    "workflow": {
        "environment": "lab",
        "dry_run_required": True,
        "production_writes_locked": True,
        "require_git_review": True,
        "require_validation": True,
        "require_verification": True,
        "canary_size": 1,
        "batch_size": 100,
    },
    "audit": {
        "log_ui_actions": True,
        "log_config_changes": True,
        "log_command_sessions": True,
        "history_limit": 100,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _scrub_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_sensitive(item) for key, item in value.items() if key.lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    return value


def ui_config_path(paths: WorkspacePaths) -> Path:
    return paths.state / "ui_config.yaml"


def ui_config_history_path(paths: WorkspacePaths) -> Path:
    return paths.state / "ui_config_history.yaml"


def read_ui_config(paths: WorkspacePaths) -> dict[str, Any]:
    path = ui_config_path(paths)
    if not path.exists():
        return deepcopy(DEFAULT_UI_CONFIG)
    return _deep_merge(DEFAULT_UI_CONFIG, _scrub_sensitive(read_yaml(path)))


def write_ui_config(paths: WorkspacePaths, config: dict[str, Any], *, actor: str = "ui") -> dict[str, Any]:
    clean = _deep_merge(DEFAULT_UI_CONFIG, _scrub_sensitive(config))
    write_yaml(ui_config_path(paths), clean)
    _record_config_event(paths, "updated", actor, clean)
    return clean


def reset_ui_config(paths: WorkspacePaths, *, actor: str = "ui") -> dict[str, Any]:
    clean = deepcopy(DEFAULT_UI_CONFIG)
    write_yaml(ui_config_path(paths), clean)
    _record_config_event(paths, "reset", actor, clean)
    return clean


def ui_config_history(paths: WorkspacePaths) -> list[dict[str, Any]]:
    path = ui_config_history_path(paths)
    if not path.exists():
        return []
    history = read_yaml(path).get("events", [])
    return history if isinstance(history, list) else []


def desired_state_catalog_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    change_types = config.get("desired_state", {}).get("change_types", {})
    if not isinstance(change_types, dict):
        return []
    catalog: list[dict[str, Any]] = []
    for change_type, settings in change_types.items():
        if not isinstance(settings, dict):
            continue
        catalog.append({"id": change_type, **settings})
    return catalog


def configured_inventory_path(paths: WorkspacePaths) -> Path:
    return _resolve_workspace_path(paths, read_ui_config(paths)["source_of_truth"].get("inventory_path", "inventories/lab.yaml"))


def configured_policy_path(paths: WorkspacePaths) -> Path:
    return _resolve_workspace_path(paths, read_ui_config(paths)["source_of_truth"].get("policy_path", "policies/invariants.yaml"))


def configured_template_dir(paths: WorkspacePaths) -> Path:
    return _resolve_workspace_path(paths, read_ui_config(paths)["source_of_truth"].get("template_dir", "templates"))


def _resolve_workspace_path(paths: WorkspacePaths, value: object) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return paths.root
    path = Path(raw).expanduser()
    return path if path.is_absolute() else paths.root / path


def _record_config_event(paths: WorkspacePaths, action: str, actor: str, config: dict[str, Any]) -> None:
    history_path = ui_config_history_path(paths)
    events = ui_config_history(paths)
    limit = int(config.get("audit", {}).get("history_limit") or 100)
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "actor": actor,
        "config_path": str(ui_config_path(paths)),
        "summary": {
            "git_repo_url": config.get("git", {}).get("repo_url"),
            "source_of_truth": config.get("source_of_truth", {}),
            "selected_change_type": config.get("desired_state", {}).get("selected_change_type"),
            "change_type_count": len(config.get("desired_state", {}).get("change_types", {}) or {}),
            "workflow": config.get("workflow", {}),
        },
    }
    events = (events + [event])[-limit:]
    write_yaml(history_path, {"events": events})
