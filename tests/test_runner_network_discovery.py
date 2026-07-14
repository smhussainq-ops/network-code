from __future__ import annotations

import json
from pathlib import Path
import types

from netcode import runner_agent


def _write_inventory(path: Path) -> str:
    content = """
defaults:
  username: local-user
  password: local-secret
devices:
  - id: core-1
    hostname: CORE-1
    host: 10.20.0.10
    platform: arista_eos
    site: hq
  - id: edge-1
    hostname: EDGE-1
    host: 10.20.0.11
    platform: arista_eos
    site: hq
""".strip()
    path.write_text(content, encoding="utf-8")
    return content


def _install_fake_rez(monkeypatch, calls: list[str], *, fail_host: str = ""):
    import netcode.adapters.registry as registry

    class FakeRez:
        def normalize_platform(self, value):  # noqa: ANN001
            return value or ""

        def driver_map(self):
            return {"arista_eos": object}

        def summary(self):
            return {}

        def collect_device_state(self, device):  # noqa: ANN001
            calls.append(device.host)
            assert device.username == "local-user"
            assert device.password == "local-secret"
            if device.host == fail_host:
                return {"ok": False, "error": "authentication failed", "warnings": [], "errors": []}
            neighbors = []
            if device.host == "10.20.0.10":
                neighbors = [
                    {"neighbor_id": "EDGE-1", "management_address": "10.20.0.11"},
                    {"neighbor_id": "outside", "management_address": "203.0.113.9"},
                ]
            return {
                "ok": True,
                "adapter": "rez.arista_eos",
                "driver": "fake",
                "state": {
                    "device": {"hostname": device.hostname},
                    "interfaces": {"Ethernet1": {"status": "up"}},
                    "lldp_neighbors": neighbors,
                },
                "warnings": [],
                "errors": [],
            }

    monkeypatch.setattr(registry, "AdapterRegistry", lambda: types.SimpleNamespace(rez=FakeRez()))


def test_recursive_discovery_runs_on_connector_and_preserves_inventory(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    original = _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls)
    progress: list[dict] = []

    result = runner_agent._execute_rez_discover_network(
        {"seed_node": "core-1", "depth": 1, "max_devices": 10, "concurrency": 2},
        progress.append,
    )

    assert result["ok"] is True
    assert result["partial"] is False
    assert result["collected"] == 2
    assert set(result["device_states"]) == {"core-1", "edge-1"}
    assert all(state.get("_collected_at") for state in result["device_states"].values())
    assert all(state.get("collected_at") for state in result["device_states"].values())
    assert calls == ["10.20.0.10", "10.20.0.11"]
    assert "203.0.113.9" not in calls
    assert inventory_path.read_text(encoding="utf-8") == original
    assert result["safety"] == {
        "device_writes": "none",
        "credentials_returned": False,
        "execution_location": "local_connector",
        "scope_enforced": True,
        "source_of_truth_written": False,
    }
    assert "local-secret" not in json.dumps(result)
    assert {event["stage"] for event in progress} >= {
        "scope_validated",
        "device_started",
        "device_collected",
        "discovery_completed",
    }


def test_exclusion_wins_over_neighbor_expansion(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls)

    result = runner_agent._execute_rez_discover_network(
        {
            "seed_node": "core-1",
            "depth": 2,
            "allowed_cidrs": ["10.20.0.0/24"],
            "excluded_cidrs": ["10.20.0.11/32"],
        }
    )

    assert result["ok"] is True
    assert result["collected"] == 1
    assert calls == ["10.20.0.10"]


def test_partial_discovery_reports_failure_without_dropping_good_state(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls, fail_host="10.20.0.11")

    result = runner_agent._execute_rez_discover_network(
        {"seed_node": "core-1", "depth": 1}
    )

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["collected"] == 1
    assert result["failed"] == 1
    assert result["failures"][0]["host"] == "10.20.0.11"
    assert set(result["device_states"]) == {"core-1"}


def test_unsafe_scope_is_rejected_before_adapter_runs(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls)

    result = runner_agent._execute_rez_discover_network(
        {"seed_node": "10.0.0.0/8", "max_devices": 25}
    )

    assert result["ok"] is False
    assert result["scope_rejected"] is True
    assert calls == []


def test_unknown_unreachable_endpoint_skips_vendor_driver_fanout(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls)

    def unreachable(*_args, **_kwargs):
        raise TimeoutError("closed")

    monkeypatch.setattr(runner_agent.socket, "create_connection", unreachable)
    result = runner_agent._execute_rez_scan_device(
        {"host": "10.20.0.99", "port": 22},
        persist_inventory=False,
    )

    assert result["ok"] is False
    assert result["error"] == "endpoint_unreachable:TimeoutError"
    assert result["tried_platforms"] == []
    assert calls == []


def test_range_sweep_skips_unknown_closed_addresses_without_marking_partial(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls)

    def unreachable(*_args, **_kwargs):
        raise TimeoutError("closed")

    monkeypatch.setattr(runner_agent.socket, "create_connection", unreachable)
    result = runner_agent._execute_rez_discover_network(
        {"seed_node": "10.20.0.10-12", "depth": 0, "max_devices": 10}
    )

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["partial"] is False
    assert result["collected"] == 2
    assert result["failed"] == 0
    assert result["skipped"] == 1
    assert result["skipped_targets"] == [
        {"host": "10.20.0.12", "port": 22, "depth": 0, "reason": "no_reachable_endpoint"}
    ]
    assert set(calls) == {"10.20.0.10", "10.20.0.11"}


def test_explicit_unknown_closed_address_remains_a_failure(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    _write_inventory(inventory_path)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)
    calls: list[str] = []
    _install_fake_rez(monkeypatch, calls)

    def unreachable(*_args, **_kwargs):
        raise TimeoutError("closed")

    monkeypatch.setattr(runner_agent.socket, "create_connection", unreachable)
    result = runner_agent._execute_rez_discover_network(
        {"seed_node": "10.20.0.99", "depth": 0}
    )

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert result["failed"] == 1
    assert result["skipped"] == 0
    assert result["failures"][0]["host"] == "10.20.0.99"
    assert calls == []
