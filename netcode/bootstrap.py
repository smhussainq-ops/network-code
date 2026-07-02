"""Initial workspace files."""

from __future__ import annotations

from pathlib import Path

from netcode.paths import WorkspacePaths
from netcode.yamlio import write_yaml


ADD_VLAN_TEMPLATE = """vlan {{ vlan.id }}
   name {{ vlan.name }}
{% if vlan.svi.enabled %}
interface Vlan{{ vlan.id }}
   description {{ vlan.name }} gateway
   ip address {{ vlan.svi.gateway_ip }} {{ vlan.netmask }}
{% endif %}
"""


def init_workspace(paths: WorkspacePaths, force: bool = False) -> list[Path]:
    paths.ensure()
    written: list[Path] = []

    files: list[tuple[Path, str]] = [
        (paths.templates / "arista" / "add_vlan.j2", ADD_VLAN_TEMPLATE),
    ]
    for path, content in files:
        if force or not path.exists():
            path.write_text(content, encoding="utf-8")
            written.append(path)

    inventory = {
        "lab_type": "arista_containerlab_v2",
        "description": "Rez Arista containerlab v2 reachable from ORB VM clab",
        "defaults": {
            "platform": "arista_eos",
            "username": "admin",
            "password": "admin",
            "port": 22,
        },
        "devices": [
            {
                "id": "v2-store1",
                "hostname": "v2-store1",
                "host": "172.100.1.41",
                "groups": ["stores", "access-switches"],
                "site": "store-1842",
            },
            {
                "id": "v2-store2",
                "hostname": "v2-store2",
                "host": "172.100.1.42",
                "groups": ["stores", "access-switches"],
                "site": "store-1843",
            },
            {
                "id": "v2-store3",
                "hostname": "v2-store3",
                "host": "172.100.1.43",
                "groups": ["stores", "access-switches"],
                "site": "store-1844",
            },
        ],
        "known_subnets": {
            "store-1842": ["10.42.30.0/24", "10.42.40.0/24"],
            "store-1843": ["10.43.30.0/24", "10.43.40.0/24"],
            "store-1844": ["10.44.30.0/24", "10.44.40.0/24"],
        },
    }
    inv_path = paths.inventories / "lab.yaml"
    if force or not inv_path.exists():
        write_yaml(inv_path, inventory)
        written.append(inv_path)

    policies = {
        "vlan": {
            "allowed_range": [2, 4094],
            "reserved": [1002, 1003, 1004, 1005],
            "name_pattern": "^[A-Z0-9_\\-]{2,32}$",
        },
        "segmentation": {
            "pci_subnets": ["10.42.30.0/24", "10.43.30.0/24", "10.44.30.0/24"],
            "guest_purposes": ["guest", "guest_wifi", "public_wifi"],
        },
        "render_scope": {
            "add_vlan_allowed_prefixes": [
                "vlan ",
                "   name ",
                "interface Vlan",
                "   description ",
                "   ip address ",
            ],
            "blocked_fragments": [
                "username ",
                "management api",
                "interface Management",
                "ip access-list",
                "router bgp",
                "router ospf",
                "enable secret",
            ],
        },
    }
    policy_path = paths.policies / "invariants.yaml"
    if force or not policy_path.exists():
        write_yaml(policy_path, policies)
        written.append(policy_path)

    example = {
        "change_type": "add_vlan",
        "site": "store-1842",
        "targets": {"device_ids": ["v2-store1"], "device_group": "access-switches"},
        "vlan": {
            "id": 90,
            "name": "GUEST_WIFI",
            "subnet": "10.42.90.0/24",
            "purpose": "guest",
            "svi": {"enabled": False},
        },
        "policy": {"pci_reachable": False, "internet_reachable": True},
        "metadata": {"requested_by": "lab-engineer", "learning_mode": True},
    }
    example_path = paths.intents / "examples" / "add_guest_vlan.yaml"
    if force or not example_path.exists():
        write_yaml(example_path, example)
        written.append(example_path)

    return written
