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


INTERFACE_CONFIG_TEMPLATE = """interface {{ interface.name }}
{% if interface.description %}
   description {{ interface.description }}
{% endif %}
{% if interface.mode == "access" %}
   switchport mode access
   switchport access vlan {{ interface.access_vlan }}
{% elif interface.mode == "trunk" %}
   switchport mode trunk
{% if interface.trunk_allowed_vlans %}
   switchport trunk allowed vlan {{ interface.trunk_allowed_vlans | join(',') }}
{% endif %}
{% elif interface.mode == "routed" %}
   no switchport
   ip address {{ interface.ip_address }}
{% endif %}
{% if interface.enabled %}
   no shutdown
{% else %}
   shutdown
{% endif %}
"""


BGP_NEIGHBOR_TEMPLATE = """router bgp {{ bgp.asn }}
{% if bgp.router_id %}
   router-id {{ bgp.router_id }}
{% endif %}
{% for neighbor in bgp.neighbors %}
   neighbor {{ neighbor.address }} remote-as {{ neighbor.remote_as }}
{% if neighbor.description %}
   neighbor {{ neighbor.address }} description {{ neighbor.description }}
{% endif %}
{% if neighbor.update_source %}
   neighbor {{ neighbor.address }} update-source {{ neighbor.update_source }}
{% endif %}
{% if neighbor.shutdown %}
   neighbor {{ neighbor.address }} shutdown
{% else %}
   no neighbor {{ neighbor.address }} shutdown
{% endif %}
{% endfor %}
"""


ROUTING_REDISTRIBUTION_TEMPLATE = """{% macro render_boundary(item) -%}
{% for prefix in item.prefixes %}
ip prefix-list {{ item.prefix_list }} seq {{ loop.index * 10 }} permit {{ prefix }} le 32
{% endfor %}
route-map {{ item.route_map }} permit 10
   match ip address prefix-list {{ item.prefix_list }}
{% if item.to_protocol == "ospf" %}
   set tag {{ item.route_tag }}
router ospf {{ item.target_process }}
   redistribute {{ item.from_protocol }} route-map {{ item.route_map }}
{% else %}
router bgp {{ item.target_process }}
   address-family ipv4
      redistribute {{ item.from_protocol }} route-map {{ item.route_map }}
{% endif %}
{%- endmacro %}
{{ render_boundary(redistribution) }}
{% if reverse_redistribution %}
{{ render_boundary(reverse_redistribution) }}
{% endif %}
"""


ACL_RULE_TEMPLATE = """ip access-list {{ acl.name }}
{% if acl.remark %}
   remark {{ acl.remark }}
{% endif %}
   {{ acl.sequence }} {{ acl.action }} {{ acl.protocol }} {{ acl.source }} {{ acl.destination }}{% if acl.destination_port %} eq {{ acl.destination_port }}{% endif %}
"""


SITE_DEVICE_INTENT_TEMPLATE = """! Source-of-truth only intent.
! Device {{ device.device_id }} should be represented in inventory before config push.
"""


CUSTOM_CONFIG_TEMPLATE = """{{ custom.config_lines }}
"""

NTP_STANDARDIZE_TEMPLATE = """{% for server in ntp.servers %}
ntp server {{ server }}{% if ntp.prefer_first and loop.first %} prefer{% endif %}

{% endfor %}
"""


OS_UPGRADE_TEMPLATE = """! Netcode EOS OS upgrade staged workflow
! pre-check: show version
! pre-check: show boot-config
{% if os_upgrade.verify_bgp %}! pre-check: show ip bgp summary
{% endif %}! stage image: {{ os_upgrade.image_uri or 'runner-local image repository' }}
! verify md5 {{ os_upgrade.image }} {{ os_upgrade.md5 }}
! maintenance window required: {{ os_upgrade.maintenance_window }}
boot system flash:{{ os_upgrade.image }}
! device reload is not rendered; canary reload requires separate human approval
"""



WORKSPACE_GITIGNORE = """# Netcode change workspace: Git tracks ONLY change artifacts, never platform code,
# UI, runtime state, or dev files. This keeps branch switching from colliding with
# source files when the workspace and the code happen to share a directory.
/*
!/.gitignore
!/intents/
!/rendered/
!/reports/
!/inventories/
!/policies/
!/templates/
"""


def init_workspace(paths: WorkspacePaths, force: bool = False) -> list[Path]:
    paths.ensure()
    written: list[Path] = []

    files: list[tuple[Path, str]] = [
        (paths.templates / "arista" / "add_vlan.j2", ADD_VLAN_TEMPLATE),
        (paths.templates / "arista" / "interface_config.j2", INTERFACE_CONFIG_TEMPLATE),
        (paths.templates / "arista" / "bgp_neighbor.j2", BGP_NEIGHBOR_TEMPLATE),
        (paths.templates / "arista" / "routing_redistribution.j2", ROUTING_REDISTRIBUTION_TEMPLATE),
        (paths.templates / "arista" / "acl_rule.j2", ACL_RULE_TEMPLATE),
        (paths.templates / "arista" / "site_device_intent.j2", SITE_DEVICE_INTENT_TEMPLATE),
        (paths.templates / "arista" / "custom_config.j2", CUSTOM_CONFIG_TEMPLATE),
        (paths.templates / "arista" / "ntp_standardize.j2", NTP_STANDARDIZE_TEMPLATE),
        (paths.templates / "arista" / "os_upgrade.j2", OS_UPGRADE_TEMPLATE),
    ]
    for path, content in files:
        if force or not path.exists():
            path.write_text(content, encoding="utf-8")
            written.append(path)

    # Create-only (never overwritten, even with force): protects a real repo's .gitignore.
    gitignore_path = paths.root / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(WORKSPACE_GITIGNORE, encoding="utf-8")
        written.append(gitignore_path)

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
            "interface_config_allowed_prefixes": [
                "interface ",
                "   description ",
                "   switchport ",
                "   no switchport",
                "   ip address ",
                "   shutdown",
                "   no shutdown",
            ],
            "bgp_neighbor_allowed_prefixes": [
                "router bgp ",
                "   router-id ",
                "   neighbor ",
                "   no neighbor ",
            ],
            "acl_rule_allowed_prefixes": [
                "ip access-list ",
                "   remark ",
                "   permit ",
                "   deny ",
            ],
            "site_device_intent_allowed_prefixes": [
                "! ",
            ],
            "os_upgrade_allowed_prefixes": [
                "! ",
                "boot system flash:",
            ],
            "blocked_fragments": [
                "username ",
                "management api",
                "interface Management",
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
