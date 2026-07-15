from __future__ import annotations

from netcode.bootstrap import init_workspace
from netcode.discovery import DiscoveryService
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore
from netcode.yamlio import read_yaml, write_yaml


def test_first_discovery_import_creates_secret_free_source_of_truth(tmp_path):
    paths = WorkspacePaths(tmp_path)
    paths.ensure()
    inventory_path = paths.inventories / "lab.yaml"

    result = DiscoveryService(paths).import_candidate(
        {
            "id": "first-edge",
            "hostname": "FIRST-EDGE",
            "host": "192.0.2.10",
            "platform": "arista_eos",
            "site": "site-101",
            "password": "must-not-persist",
        }
    )

    assert result["ok"] is True
    assert result["action"] == "added"
    assert inventory_path.exists()
    inventory = read_yaml(inventory_path)
    assert inventory == {
        "devices": [
            {
                "id": "first-edge",
                "hostname": "FIRST-EDGE",
                "host": "192.0.2.10",
                "platform": "arista_eos",
                "site": "site-101",
                "role": "",
                "groups": ["discovered"],
                "port": 22,
                "serial": "",
                "aliases": [],
            }
        ]
    }
    assert "password" not in inventory
    assert "defaults" not in inventory


def test_discovery_import_rejects_serial_change_on_existing_device(tmp_path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    inventory_path = paths.inventories / "lab.yaml"
    write_yaml(
        inventory_path,
        {
            "devices": [
                {"id": "edge-1", "host": "10.20.0.10", "port": 22, "platform": "arista_eos", "serial": "SERIAL-A"}
            ]
        },
    )

    result = DiscoveryService(paths).import_candidate(
        {"id": "edge-1", "host": "10.20.0.10", "port": 22, "platform": "arista_eos", "serial": "SERIAL-B"}
    )

    assert result["ok"] is False
    assert result["status"] == "conflict"
    assert read_yaml(inventory_path)["devices"][0]["serial"] == "SERIAL-A"


def test_discovery_import_preserves_canonical_id_on_serial_matched_hostname_change(tmp_path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    inventory_path = paths.inventories / "lab.yaml"
    write_yaml(
        inventory_path,
        {
            "devices": [
                {"id": "edge-1", "hostname": "EDGE-1", "host": "10.20.0.10", "port": 22, "platform": "arista_eos", "serial": "SERIAL-A"}
            ]
        },
    )

    result = DiscoveryService(paths).import_candidate(
        {"id": "edge-renamed", "hostname": "EDGE-RENAMED", "host": "10.20.0.12", "port": 22, "platform": "arista_eos", "serial": "SERIAL-A"}
    )

    assert result["ok"] is True
    assert result["action"] == "updated"
    assert result["device"]["id"] == "edge-1"
    assert "edge-renamed" in result["device"]["aliases"]
    devices = read_yaml(inventory_path)["devices"]
    assert len(devices) == 1
    assert devices[0]["id"] == "edge-1"


def test_device_catalog_holds_serial_endpoint_and_alias_conflicts_for_review(tmp_path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    store = PlatformStore(paths)
    first = store.create_runner("connector-a", "pilot", "token-a", "secret-a")
    second = store.create_runner("connector-b", "pilot", "token-b", "secret-b")
    first_result = store.sync_runner_devices(
        first,
        [{
            "id": "edge-1",
            "hostname": "EDGE-1",
            "host": "10.20.0.10",
            "port": 22,
            "platform": "arista_eos",
            "serial": "SERIAL-A",
            "aliases": ["primary-edge"],
        }],
        revision="one",
    )
    assert first_result["conflicts"] == []

    serial_conflict = store.sync_runner_devices(
        second,
        [{"id": "edge-2", "host": "10.20.0.11", "platform": "arista_eos", "serial": "SERIAL-A"}],
        revision="two",
    )
    endpoint_conflict = store.sync_runner_devices(
        second,
        [{"id": "edge-3", "host": "10.20.0.10", "port": 22, "platform": "arista_eos", "serial": "SERIAL-C"}],
        revision="three",
    )
    alias_conflict = store.sync_runner_devices(
        second,
        [{"id": "edge-4", "host": "10.20.0.14", "platform": "arista_eos", "serial": "SERIAL-D", "aliases": ["primary-edge"]}],
        revision="four",
    )

    assert serial_conflict["conflicts"][0]["type"] == "serial_identity_conflict"
    assert endpoint_conflict["conflicts"][0]["type"] == "endpoint_identity_conflict"
    assert alias_conflict["conflicts"][0]["type"] == "alias_identity_conflict"
    assert store.resolve_device(first.org_id, "primary-edge")["canonical_id"] == "edge-1"
