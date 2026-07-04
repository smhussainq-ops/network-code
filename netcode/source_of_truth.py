"""Source-of-truth provider contracts and local YAML implementation."""

from __future__ import annotations

import os
from typing import Any

from netcode.inventory import Inventory
from netcode.netbox import NetBoxClient, NetBoxError
from netcode.paths import WorkspacePaths
from netcode.ui_config import configured_inventory_path, configured_policy_path, configured_template_dir, read_ui_config
from netcode.yamlio import read_yaml, write_yaml


class LocalSourceOfTruth:
    """Expose the current local files as the source-of-truth provider."""

    provider = "local_yaml"

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self.config = read_ui_config(paths)
        self.inventory_path = configured_inventory_path(paths)
        self.policy_path = configured_policy_path(paths)
        self.template_dir = configured_template_dir(paths)

    def snapshot(self) -> dict[str, Any]:
        inventory = Inventory(self.inventory_path)
        policies = read_yaml(self.policy_path)
        templates = sorted(str(path.relative_to(self.paths.root)) if path.is_relative_to(self.paths.root) else str(path) for path in self.template_dir.rglob("*") if path.is_file())
        sites = sorted({device.site for device in inventory.devices if device.site})
        groups = sorted({group for device in inventory.devices for group in device.groups})
        platforms = sorted({device.platform for device in inventory.devices})
        return {
            "ok": True,
            "provider": str(self.config.get("source_of_truth", {}).get("provider") or self.provider),
            "files": {
                "inventory": str(self.inventory_path),
                "policies": str(self.policy_path),
                "templates": str(self.template_dir),
            },
            "sites": sites,
            "groups": groups,
            "platforms": platforms,
            "devices": [
                {
                    "id": device.id,
                    "hostname": device.hostname,
                    "host": device.host,
                    "platform": device.platform,
                    "site": device.site,
                    "groups": list(device.groups),
                    "port": device.port,
                    "credential_source": "inventory-default-or-device",
                }
                for device in inventory.devices
            ],
            "known_subnets": inventory.raw.get("known_subnets", {}),
            "policies": policies,
            "templates": templates,
            "summary": {
                "device_count": len(inventory.devices),
                "site_count": len(sites),
                "platform_count": len(platforms),
                "template_count": len(templates),
            },
        }


def provider_catalog(netbox_configured: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "id": "local_yaml",
            "name": "Local YAML",
            "status": "active",
            "capabilities": ["devices", "sites", "platforms", "known_subnets", "policies", "templates"],
            "writes": False,
            "message": "Active provider for the current lab and demo slice.",
        },
        {
            "id": "netbox",
            "name": "NetBox",
            "status": "configured" if netbox_configured else "available",
            "capabilities": ["devices", "sites", "prefixes", "vlans", "tenants"],
            "writes": False,
            "message": (
                "Configured. Test the connection, then sync devices into inventory."
                if netbox_configured
                else "Read-only device sync. Set source_of_truth.netbox.url + token to enable."
            ),
        },
        {
            "id": "nautobot",
            "name": "Nautobot",
            "status": "stub",
            "capabilities": ["devices", "sites", "prefixes", "vlans", "jobs"],
            "writes": False,
            "message": "Provider contract reserved; configure API URL/token before enabling.",
        },
        {
            "id": "servicenow_cmdb",
            "name": "ServiceNow CMDB",
            "status": "stub",
            "capabilities": ["business_service", "change_ticket", "owner", "maintenance_window"],
            "writes": False,
            "message": "Provider contract reserved for ownership and change context.",
        },
        {
            "id": "ipam",
            "name": "Enterprise IPAM",
            "status": "stub",
            "capabilities": ["prefix_allocation", "vlan_allocation", "reservation"],
            "writes": False,
            "message": "Provider contract reserved for allocation authority.",
        },
    ]


def _netbox_settings(paths: WorkspacePaths, url: str = "", token: str = "") -> tuple[str, str]:
    # URL may live in ui_config (non-secret). The token is a SENSITIVE key that
    # ui_config deliberately never persists, so it comes from the request or the
    # NETBOX_TOKEN env var — never stored in the config file or returned in reads.
    config = read_ui_config(paths).get("source_of_truth", {}).get("netbox", {}) or {}
    resolved_url = (url or str(config.get("url") or "") or os.environ.get("NETBOX_URL", "")).rstrip("/")
    resolved_token = token or str(config.get("token") or "") or os.environ.get("NETBOX_TOKEN", "")
    return resolved_url, resolved_token


def netbox_test(paths: WorkspacePaths, url: str = "", token: str = "", get_json=None) -> dict[str, Any]:
    url, token = _netbox_settings(paths, url, token)
    if not url:
        return {"ok": False, "error": "NetBox URL is not configured (source_of_truth.netbox.url)."}
    return NetBoxClient(url, token, get_json=get_json).test_connection()


def netbox_sync(paths: WorkspacePaths, url: str = "", token: str = "", get_json=None) -> dict[str, Any]:
    """Read devices from NetBox and merge them into the local inventory (read-only on NetBox;
    never writes device credentials — synced devices use inventory defaults)."""
    url, token = _netbox_settings(paths, url, token)
    if not url:
        return {"ok": False, "error": "NetBox URL is not configured (source_of_truth.netbox.url)."}
    try:
        candidates = NetBoxClient(url, token, get_json=get_json).list_devices()
    except NetBoxError as exc:
        return {"ok": False, "error": str(exc)}

    inventory_path = configured_inventory_path(paths)
    inventory = read_yaml(inventory_path)
    devices = list(inventory.get("devices") or [])
    by_id = {str(d.get("id")): i for i, d in enumerate(devices)}
    by_host = {str(d.get("host")): i for i, d in enumerate(devices)}
    imported = updated = 0
    synced_ids: list[str] = []
    for candidate in candidates:
        record = {key: candidate[key] for key in ("id", "hostname", "host", "platform", "site", "groups", "port")}
        index = by_id.get(record["id"], by_host.get(record["host"]))
        if index is not None:
            devices[index] = {**devices[index], **record}
            updated += 1
        else:
            devices.append(record)
            imported += 1
        synced_ids.append(record["id"])

    inventory["devices"] = devices
    write_yaml(inventory_path, inventory)
    return {
        "ok": True,
        "imported": imported,
        "updated": updated,
        "total": len(candidates),
        "devices": synced_ids,
        "inventory": str(inventory_path),
        "message": f"Synced {len(candidates)} device(s) from NetBox into local inventory ({imported} new, {updated} updated).",
    }


def source_of_truth(paths: WorkspacePaths) -> dict[str, Any]:
    snapshot = LocalSourceOfTruth(paths).snapshot()
    netbox_url, _ = _netbox_settings(paths)
    snapshot["providers"] = provider_catalog(netbox_configured=bool(netbox_url))
    snapshot["netbox_configured"] = bool(netbox_url)
    return snapshot
