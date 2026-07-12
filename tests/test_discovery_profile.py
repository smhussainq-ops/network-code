from __future__ import annotations

from pathlib import Path

import pytest

from netcode.discovery_profile import (
    DiscoveryProfile,
    DiscoveryProfileError,
    discovery_neighbor_targets,
)
from netcode.inventory import Inventory


def _inventory(tmp_path: Path) -> Inventory:
    path = tmp_path / "inventory.yaml"
    path.write_text(
        """
defaults:
  username: local-user
  password: local-secret
  platform: arista_eos
devices:
  - id: core-1
    hostname: CORE-1
    host: 10.20.0.10
    platform: arista_eos
    site: hq
    aliases: [core-one]
  - id: edge-1
    hostname: EDGE-1
    host: 10.20.0.11
    platform: cisco_ios
    site: hq
""".strip(),
        encoding="utf-8",
    )
    return Inventory(path)


def test_default_profile_uses_exact_inventory_and_seed_hosts_only(tmp_path: Path):
    profile = DiscoveryProfile.from_payload(
        {"seed_node": "core-1", "depth": 2},
        _inventory(tmp_path),
    )

    assert profile.scope_source == "connector_inventory_and_explicit_seeds"
    assert profile.is_allowed("10.20.0.10") is True
    assert profile.is_allowed("10.20.0.11") is True
    assert profile.is_allowed("10.20.0.12") is False
    assert "10.20.0.0/8" not in profile.public_dict()["allowed_cidrs"]


def test_explicit_range_is_bounded_and_does_not_expand_subnet(tmp_path: Path):
    profile = DiscoveryProfile.from_payload(
        {"seed_node": "192.0.2.10-12", "max_devices": 3},
        _inventory(tmp_path),
    )

    assert [target.host for target in profile.seeds] == ["192.0.2.10", "192.0.2.11", "192.0.2.12"]
    assert profile.is_allowed("192.0.2.13") is False


def test_explicit_allow_and_exclusion_are_fail_closed(tmp_path: Path):
    profile = DiscoveryProfile.from_payload(
        {
            "seed_node": "10.20.0.10",
            "allowed_cidrs": ["10.20.0.0/24"],
            "excluded_cidrs": ["10.20.0.11/32"],
        },
        _inventory(tmp_path),
    )

    assert profile.scope_source == "explicit_profile"
    assert profile.is_allowed("10.20.0.10") is True
    assert profile.is_allowed("10.20.0.11") is False
    assert profile.is_allowed("10.21.0.10") is False


def test_large_cidr_is_rejected_before_any_scan(tmp_path: Path):
    with pytest.raises(DiscoveryProfileError, match="above the remaining max_devices"):
        DiscoveryProfile.from_payload(
            {"seed_node": "10.0.0.0/8", "max_devices": 100},
            _inventory(tmp_path),
        )


def test_unknown_hostname_requires_local_inventory(tmp_path: Path):
    with pytest.raises(DiscoveryProfileError, match="Unknown hostname"):
        DiscoveryProfile.from_payload(
            {"seed_node": "untrusted.example.test"},
            _inventory(tmp_path),
        )


def test_neighbor_expansion_resolves_known_aliases_but_not_unapproved_ips(tmp_path: Path):
    inventory = _inventory(tmp_path)
    profile = DiscoveryProfile.from_payload(
        {"seed_node": "core-1", "depth": 2},
        inventory,
    )
    state = {
        "lldp_neighbors": [
            {"neighbor_id": "EDGE-1", "management_address": "10.20.0.11"},
            {"neighbor_id": "outside", "management_address": "203.0.113.9"},
        ],
        "bgp": {"neighbors": {"10.20.0.11": {"state": "established"}}},
    }

    targets = discovery_neighbor_targets(state, inventory=inventory, profile=profile)

    assert [(target.device_id, target.host) for target in targets] == [("edge-1", "10.20.0.11")]


def test_port_forwarded_devices_with_shared_host_are_not_collapsed(tmp_path: Path):
    path = tmp_path / "forwarded.yaml"
    path.write_text(
        """
defaults:
  username: local-user
  password: local-secret
devices:
  - id: fw-1
    host: 127.0.0.1
    port: 3122
    platform: fortinet
  - id: fw-2
    host: 127.0.0.1
    port: 3222
    platform: fortinet
""".strip(),
        encoding="utf-8",
    )

    profile = DiscoveryProfile.from_payload(
        {"seed_node": "fw-1,fw-2"},
        Inventory(path),
    )

    assert [(target.device_id, target.host, target.port) for target in profile.seeds] == [
        ("fw-1", "127.0.0.1", 3122),
        ("fw-2", "127.0.0.1", 3222),
    ]
