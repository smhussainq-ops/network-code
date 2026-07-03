"""Workflow orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netcode.bootstrap import init_workspace
from netcode.gitflow import git_evidence
from netcode.intent_utils import report_stem
from netcode.models import PipelineArtifacts, PipelineResult, load_intent
from netcode.paths import WorkspacePaths
from netcode.rendering import render_intent, write_rendered_config
from netcode.reporting import write_reports
from netcode.validation import StaticValidator
from netcode.yamlio import dumps_yaml, read_yaml, write_yaml


def ensure_initialized(paths: WorkspacePaths) -> None:
    if not (paths.templates / "arista" / "add_vlan.j2").exists():
        init_workspace(paths)


def create_add_vlan_intent(
    paths: WorkspacePaths,
    site: str,
    device_id: str,
    vlan_id: int,
    name: str,
    subnet: str,
    purpose: str,
    pci_reachable: bool,
    requested_by: str,
) -> Path:
    ensure_initialized(paths)
    data = {
        "change_type": "add_vlan",
        "site": site,
        "targets": {"device_ids": [device_id], "device_group": "access-switches"},
        "vlan": {
            "id": vlan_id,
            "name": name,
            "subnet": subnet,
            "purpose": purpose,
            "svi": {"enabled": False},
        },
        "policy": {"pci_reachable": pci_reachable, "internet_reachable": True},
        "metadata": {"requested_by": requested_by, "learning_mode": True},
    }
    filename = f"{site}-add-vlan-{vlan_id}.yaml"
    path = paths.intents / site / filename
    write_yaml(path, data)
    return path


def create_desired_state_intent(
    paths: WorkspacePaths,
    change_type: str,
    site: str,
    device_id: str,
    requested_by: str,
    values: dict[str, Any],
) -> Path:
    ensure_initialized(paths)
    targets = {"device_ids": [device_id], "device_group": values.get("device_group", "access-switches")}
    metadata = {
        "requested_by": requested_by,
        "ticket_id": values.get("ticket_id") or None,
        "learning_mode": bool(values.get("learning_mode", True)),
    }
    common: dict[str, Any] = {
        "change_type": change_type,
        "site": site,
        "targets": targets,
        "policy": {
            "pci_reachable": bool(values.get("pci_reachable", False)),
            "internet_reachable": bool(values.get("internet_reachable", True)),
        },
        "metadata": metadata,
    }

    if change_type == "add_vlan":
        common["vlan"] = {
            "id": int(values.get("vlan_id", 90)),
            "name": str(values.get("name", "GUEST_WIFI")),
            "subnet": str(values.get("subnet", "10.42.90.0/24")),
            "purpose": str(values.get("purpose", "guest")),
            "svi": {
                "enabled": bool(values.get("svi_enabled", False)),
                "gateway_ip": values.get("gateway_ip") or None,
            },
        }
    elif change_type == "interface_config":
        trunk_raw = str(values.get("trunk_allowed_vlans", "")).strip()
        trunk_vlans = [int(v.strip()) for v in trunk_raw.split(",") if v.strip()]
        common["interface"] = {
            "name": str(values.get("interface", "Ethernet1")),
            "description": str(values.get("description", "")),
            "enabled": bool(values.get("enabled", True)),
            "mode": str(values.get("mode", "access")),
            "access_vlan": int(values["access_vlan"]) if values.get("access_vlan") not in (None, "") else None,
            "trunk_allowed_vlans": trunk_vlans,
            "ip_address": values.get("ip_address") or None,
        }
    elif change_type == "bgp_neighbor":
        common["bgp"] = {
            "asn": int(values.get("asn", 65001)),
            "router_id": values.get("router_id") or None,
            "neighbors": [
                {
                    "address": str(values.get("neighbor", "10.255.0.2")),
                    "remote_as": int(values.get("remote_as", 65002)),
                    "description": str(values.get("description", "")),
                    "update_source": values.get("update_source") or None,
                    "shutdown": bool(values.get("shutdown", False)),
                }
            ],
        }
    elif change_type == "acl_rule":
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
    elif change_type == "site_device_intent":
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
    elif change_type == "custom_config":
        common["custom"] = {
            "config_lines": str(values.get("config_lines", "")),
            "rollback_lines": str(values.get("rollback_lines", "")),
            "verify_contains": str(values.get("verify_contains", "")),
            "description": str(values.get("description", "")),
            "acknowledge_no_rollback": bool(values.get("acknowledge_no_rollback", False)),
        }
    else:
        raise ValueError(f"Unsupported change_type: {change_type}")

    validated = load_intent_from_data(common)
    filename = f"{report_stem(validated)}.yaml"
    path = paths.intents / site / filename
    write_yaml(path, common)
    return path


def load_intent_from_data(data: dict[str, Any]):
    temp_path = Path("__memory__.yaml")
    change_type = data.get("change_type")
    if change_type == "add_vlan":
        from netcode.models import AddVlanIntent

        return AddVlanIntent.model_validate(data)
    if change_type == "interface_config":
        from netcode.models import InterfaceConfigIntent

        return InterfaceConfigIntent.model_validate(data)
    if change_type == "bgp_neighbor":
        from netcode.models import BgpNeighborIntent

        return BgpNeighborIntent.model_validate(data)
    if change_type == "acl_rule":
        from netcode.models import AclRuleIntent

        return AclRuleIntent.model_validate(data)
    if change_type == "site_device_intent":
        from netcode.models import SiteDeviceIntent

        return SiteDeviceIntent.model_validate(data)
    if change_type == "custom_config":
        from netcode.models import CustomConfigIntent

        return CustomConfigIntent.model_validate(data)
    raise ValueError(f"Unsupported change_type: {change_type!r} in {temp_path}")


def run_static_pipeline(paths: WorkspacePaths, intent_path: Path) -> PipelineResult:
    ensure_initialized(paths)
    intent_path = intent_path.resolve()
    intent = load_intent(intent_path)
    render = render_intent(intent, paths)
    rendered_path = write_rendered_config(paths, intent, render)
    validation = StaticValidator(paths).validate(intent, render)
    intent_data = read_yaml(intent_path)
    partial = PipelineResult(
        status=validation.status,
        intent=intent_data,
        intent_yaml=dumps_yaml(intent_data),
        render=render,
        validation=validation,
        git=git_evidence(paths.root, intent_path),
        artifacts=None,
    )
    stem = report_stem(intent)
    md_path, json_path = write_reports(paths, partial, stem)
    result = partial.model_copy(
        update={
            "artifacts": PipelineArtifacts(
                intent_path=str(intent_path),
                rendered_path=str(rendered_path),
                report_markdown_path=str(md_path),
                report_json_path=str(json_path),
            )
        }
    )
    write_reports(paths, result, stem)
    return result
