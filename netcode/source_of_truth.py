"""Source-of-truth provider contracts and local YAML implementation."""

from __future__ import annotations

from typing import Any

from netcode.inventory import Inventory
from netcode.paths import WorkspacePaths
from netcode.yamlio import read_yaml


class LocalSourceOfTruth:
    """Expose the current local files as the source-of-truth provider."""

    provider = "local_yaml"

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self.inventory_path = paths.inventories / "lab.yaml"
        self.policy_path = paths.policies / "invariants.yaml"

    def snapshot(self) -> dict[str, Any]:
        inventory = Inventory(self.inventory_path)
        policies = read_yaml(self.policy_path)
        templates = sorted(str(path.relative_to(self.paths.root)) for path in self.paths.templates.rglob("*") if path.is_file())
        sites = sorted({device.site for device in inventory.devices if device.site})
        groups = sorted({group for device in inventory.devices for group in device.groups})
        platforms = sorted({device.platform for device in inventory.devices})
        return {
            "ok": True,
            "provider": self.provider,
            "files": {
                "inventory": str(self.inventory_path),
                "policies": str(self.policy_path),
                "templates": str(self.paths.templates),
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


def provider_catalog() -> list[dict[str, Any]]:
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
            "status": "stub",
            "capabilities": ["devices", "sites", "prefixes", "vlans", "tenants"],
            "writes": False,
            "message": "Provider contract reserved; configure API URL/token before enabling.",
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


def source_of_truth(paths: WorkspacePaths) -> dict[str, Any]:
    snapshot = LocalSourceOfTruth(paths).snapshot()
    snapshot["providers"] = provider_catalog()
    return snapshot
