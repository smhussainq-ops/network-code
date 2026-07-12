from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA, NETWORK_OBSERVATION_SCHEMA, NetworkModelError
from netcode.network_model_reconcile import reconcile_revision
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.runner_hub import enroll_runner, mint_join_token
from netcode.store import PlatformStore


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _repository(tmp_path: Path) -> NetworkModelRepository:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    return NetworkModelRepository(PlatformStore(workspace))


def _revision() -> dict:
    return {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": "org-default",
        "environment_id": "pilot-a",
        "revision_id": "rev-001",
        "status": "approved",
        "source": {"type": "git", "reference": "model/rev-001"},
        "coverage": {"domains": ["routing", "topology"]},
        "authority_bindings": {
            "routing": {"source": "git", "mode": "authoritative"},
            "topology": {"source": "git", "mode": "authoritative"},
        },
        "approval": {
            "status": "approved",
            "approved_by": "marcus",
            "approved_at": "2026-07-12T10:00:00Z",
        },
        "model": {
            "sites": {
                "site-101": {
                    "devices": {"edge-1": {"role": "edge"}},
                    "operational_dependencies": [
                        {
                            "id": "bgp-wan-a",
                            "device_id": "edge-1",
                            "kind": "bgp",
                            "identity": {"neighbor": "198.51.100.1"},
                            "expected": {"state": "established"},
                        },
                        {
                            "id": "qos-wan-a",
                            "device_id": "edge-1",
                            "kind": "qos",
                            "identity": {"interface": "Ethernet1"},
                            "expected": {"policy": "WAN-EDGE"},
                        },
                    ],
                }
            }
        },
    }


def _observation(observation_id: str, facts: dict, *, expires_at: str = "2026-07-12T12:10:00Z", grade: str = "validated") -> dict:
    return {
        "schema": NETWORK_OBSERVATION_SCHEMA,
        "org_id": "org-default",
        "environment_id": "pilot-a",
        "observation_id": observation_id,
        "domain": "routing",
        "subject_id": "bgp-wan-a",
        "source": "device",
        "collector_id": "connector-1",
        "observed_at": "2026-07-12T11:59:00Z",
        "expires_at": expires_at,
        "validation_grade": grade,
        "facts": facts,
    }


def test_reconciliation_requires_fresh_exact_evidence_and_preserves_unknown_coverage(tmp_path: Path):
    repository = _repository(tmp_path)
    revision = repository.create_revision(_revision(), created_by="marcus")
    repository.record_observation(
        _observation("obs-001", {"neighbor": "198.51.100.1", "state": "established"})
    )

    result = reconcile_revision(repository, revision, site_id="site-101", now=NOW)

    assert result["status"] == "unknown"
    assert result["summary"] == {
        "status": "unknown",
        "reason": "insufficient_fresh_validated_evidence",
        "modeled_dependencies": 2,
        "match": 1,
        "drift": 0,
        "unknown": 1,
    }
    by_id = {item["dependency_id"]: item for item in result["findings"]}
    assert by_id["bgp-wan-a"]["status"] == "match"
    assert by_id["qos-wan-a"]["reason"] == "domain_not_covered"


def test_mismatch_is_drift_but_stale_or_unvalidated_is_unknown(tmp_path: Path):
    for suffix, observation, expected_status in (
        ("drift", _observation("obs-drift", {"neighbor": "198.51.100.1", "state": "idle"}), "drift"),
        ("stale", _observation("obs-stale", {"neighbor": "198.51.100.1", "state": "idle"}, expires_at="2026-07-12T11:59:30Z"), "unknown"),
        ("weak", _observation("obs-weak", {"neighbor": "198.51.100.1", "state": "idle"}, grade="unknown"), "unknown"),
    ):
        repository = _repository(tmp_path / suffix)
        revision = repository.create_revision(_revision(), created_by="marcus")
        repository.record_observation(observation)
        result = reconcile_revision(repository, revision, site_id="site-101", now=NOW)
        bgp = next(item for item in result["findings"] if item["dependency_id"] == "bgp-wan-a")
        assert bgp["status"] == expected_status


def test_observation_is_append_only_and_out_of_order_data_cannot_replace_current(tmp_path: Path):
    repository = _repository(tmp_path)
    newest = _observation("obs-new", {"neighbor": "198.51.100.1", "state": "established"})
    old = _observation("obs-old", {"neighbor": "198.51.100.1", "state": "idle"})
    old["observed_at"] = "2026-07-12T11:00:00Z"
    old["expires_at"] = "2026-07-12T12:05:00Z"
    repository.record_observation(newest)
    repository.record_observation(old)

    current = repository.current_observations("org-default", "pilot-a", [("routing", "bgp-wan-a")])
    assert current[("routing", "bgp-wan-a")]["observation_id"] == "obs-new"
    assert repository.record_observation(newest)["created"] is False
    changed = {**newest, "facts": {"state": "idle"}}
    with pytest.raises(ValueError, match="different content"):
        repository.record_observation(changed)


def test_incident_observation_cannot_mutate_revision_or_become_health_baseline(tmp_path: Path):
    repository = _repository(tmp_path)
    revision = repository.create_revision(_revision(), created_by="marcus")
    incident = _observation("obs-incident", {"neighbor": "198.51.100.1", "state": "established"})
    incident["source"] = "incident"
    repository.record_observation(incident)

    unchanged = repository.get_revision("org-default", "pilot-a", "rev-001")
    assert unchanged == revision
    with repository.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM network_model_heads").fetchone()["count"] == 0


def test_proposed_revision_cannot_be_reconciled(tmp_path: Path):
    repository = _repository(tmp_path)
    proposed = _revision()
    proposed["status"] = "proposed"
    proposed.pop("approval")
    revision = repository.create_revision(proposed, created_by="marcus")
    with pytest.raises(NetworkModelError, match="approved or active"):
        reconcile_revision(repository, revision, now=NOW)


def test_user_cannot_forge_validated_live_observation_but_connector_can(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(workspace)
    join = mint_join_token(store, "pilot")
    enrolled = enroll_runner(store, join["join_token"], "connector-1")
    client = TestClient(api.app)
    forged = _observation("obs-user", {"neighbor": "198.51.100.1", "state": "established"})

    user_response = client.post("/api/network-model/observations", json=forged)
    assert user_response.status_code == 200
    assert user_response.json()["observation"]["validation_grade"] == "unknown"
    assert user_response.json()["observation"]["source"] == "manual_review"

    runner_observation = _observation("obs-runner", {"neighbor": "198.51.100.1", "state": "established"})
    runner_response = client.post(
        "/api/runner/network-model/observations",
        headers={"Authorization": f"Bearer {enrolled['runner_token']}"},
        json=runner_observation,
    )
    assert runner_response.status_code == 200
    assert runner_response.json()["observation"]["validation_grade"] == "validated"
    assert runner_response.json()["observation"]["collector_id"] == enrolled["runner_id"]

    listed = client.get(
        "/api/network-model/observations",
        params={"environment_id": "pilot-a", "subject_id": "bgp-wan-a", "limit": 1},
    )
    assert listed.status_code == 200
    assert listed.json()["returned"] == 1
    assert listed.json()["device_connections_opened"] == 0


def test_rez_refresh_projects_only_safe_fresh_observations_and_replay_is_idempotent(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    result = {
        "ok": True,
        "timestamp": "2026-07-12T12:00:00+00:00",
        "device_states": {
            "EDGE-1": {
                "_collected_at": "2026-07-12T12:00:00+00:00",
                "interfaces": {
                    "Ethernet1": {
                        "admin_state": "up",
                        "oper_state": "up",
                        "ip_prefixes": ["192.0.2.1/30"],
                        "raw_command": "show running-config",
                    },
                    "Ethernet2": {
                        "status": "down",
                        "ip_prefixes": "must-not-be-split-into-characters",
                    },
                },
                "routes": [{"prefix": "0.0.0.0/0"}],
                "bgp_neighbors": [{"neighbor_ip": "192.0.2.2", "state": "Established", "remote_as": 65001}],
                "ospf_neighbors": [{"neighbor_id": "10.0.0.2", "state": "FULL", "interface": "Ethernet1"}],
                "lldp_neighbors": [
                    {"neighbor_id": "core-1", "local_interface": "Ethernet1", "neighbor_interface": "Ethernet2"}
                ],
                "running_config": "username admin secret forbidden",
                "password": "must-never-persist",
            }
        },
    }

    first = api._record_rez_refresh_observations(
        store,
        org_id="org-default",
        environment_id="pilot-a",
        runner_id="connector-1",
        result=result,
    )
    replay = api._record_rez_refresh_observations(
        store,
        org_id="org-default",
        environment_id="pilot-a",
        runner_id="connector-1",
        result=result,
    )

    assert first == 2
    assert replay == 0
    observations = NetworkModelRepository(store).list_observations(
        "org-default", "pilot-a", subject_id="edge-1", limit=10
    )["observations"]
    assert len(observations) == 2
    assert {item["domain"] for item in observations} == {"routing", "topology"}
    for observation in observations:
        assert observation["collector_id"] == "connector-1"
        assert observation["validation_grade"] == "device_authoritative"
        assert observation["observed_at"] == "2026-07-12T12:00:00Z"
        assert observation["expires_at"] == "2026-07-12T12:10:00Z"
        assert observation["metadata"] == {
            "privacy_policy": "rez_control_plane_policy",
            "raw_configuration_stored": False,
        }
    serialized = json.dumps(observations)
    assert "must-never-persist" not in serialized
    assert "username admin" not in serialized
    assert "show running-config" not in serialized
    topology = next(item for item in observations if item["domain"] == "topology")
    assert topology["facts"]["actual"]["interfaces"]["Ethernet1"]["admin_status"] == "up"
    assert topology["facts"]["actual"]["interfaces"]["Ethernet1"]["oper_status"] == "up"
    assert topology["facts"]["actual"]["interfaces"]["Ethernet2"]["ip_prefixes"] == []
