from __future__ import annotations

from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore


def _document(revision_id: str = "rev-001", org_id: str = "org-default") -> dict:
    return {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": org_id,
        "environment_id": "pilot-a",
        "revision_id": revision_id,
        "status": "proposed",
        "source": {"type": "manual_review", "reference": f"model/{revision_id}"},
        "coverage": {"domains": ["identity", "sites"]},
        "authority_bindings": {
            "identity": {"source": "rezonance", "mode": "authoritative"},
            "sites": {"source": "rezonance", "mode": "authoritative"},
        },
        "model": {
            "sites": {"site-101": {"region": "east", "criticality": "production"}},
            "devices": {
                "edge-1": {"site": "site-101", "role": "edge", "platform": "arista_eos"},
                "edge-2": {"site": "site-101", "role": "edge", "platform": "junos"},
            },
        },
    }


def _repository(tmp_path: Path) -> NetworkModelRepository:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    return NetworkModelRepository(PlatformStore(workspace))


def test_revision_is_immutable_and_persists_materialized_entities(tmp_path: Path):
    repository = _repository(tmp_path)
    created = repository.create_revision(_document(), created_by="marcus@example.com")

    assert created["revision_id"] == "rev-001"
    assert created["model"]["devices"]["edge-2"]["platform"] == "junos"
    devices = repository.list_entities(
        "org-default", "pilot-a", "rev-001", entity_type="devices", site="site-101", limit=1
    )
    assert devices["returned"] == 1
    assert devices["next_cursor"]
    second = repository.list_entities(
        "org-default",
        "pilot-a",
        "rev-001",
        entity_type="devices",
        site="site-101",
        cursor=devices["next_cursor"],
        limit=1,
    )
    assert second["returned"] == 1
    assert {devices["entities"][0]["entity_id"], second["entities"][0]["entity_id"]} == {"edge-1", "edge-2"}

    with pytest.raises(ValueError, match="already exists"):
        repository.create_revision(_document(), created_by="another-user")


def test_revision_cannot_skip_governed_activation_or_reference_unknown_parent(tmp_path: Path):
    repository = _repository(tmp_path)
    active = _document()
    active.update(
        {
            "status": "active",
            "approval": {
                "status": "approved",
                "approved_by": "marcus",
                "approved_at": "2026-07-12T12:00:00Z",
            },
        }
    )
    with pytest.raises(ValueError, match="governed activation"):
        repository.create_revision(active, created_by="marcus")

    child = _document("rev-child")
    child["parent_revision_id"] = "rev-missing"
    with pytest.raises(ValueError, match="does not exist in this environment"):
        repository.create_revision(child, created_by="marcus")


def test_revision_queries_are_tenant_scoped(tmp_path: Path):
    repository = _repository(tmp_path)
    repository.create_revision(_document(org_id="org-a"), created_by="a")
    repository.create_revision(_document(org_id="org-b"), created_by="b")

    assert repository.list_revisions("org-a", "pilot-a")["returned"] == 1
    assert repository.list_revisions("org-missing", "pilot-a")["returned"] == 0
    with pytest.raises(KeyError):
        repository.get_revision("org-b", "pilot-a", "missing")


def test_revision_list_is_bounded_and_does_not_return_model_blob(tmp_path: Path):
    repository = _repository(tmp_path)
    for index in range(4):
        repository.create_revision(_document(f"rev-{index:03d}"), created_by="marcus")

    first = repository.list_revisions("org-default", "pilot-a", limit=2)
    assert first["returned"] == 2
    assert first["next_cursor"]
    assert all("model" not in revision for revision in first["revisions"])
    second = repository.list_revisions(
        "org-default", "pilot-a", limit=2, cursor=str(first["next_cursor"])
    )
    assert second["returned"] == 2
    assert not ({item["revision_id"] for item in first["revisions"]} & {item["revision_id"] for item in second["revisions"]})


def test_network_model_api_forces_principal_tenant_and_opens_no_devices(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    document = _document(org_id="org-attacker")

    response = client.post("/api/network-model/revisions", json=document)
    assert response.status_code == 200
    assert response.json()["revision"]["org_id"] == "org_default"

    page = client.get("/api/network-model/revisions", params={"environment_id": "pilot-a"})
    assert page.status_code == 200
    assert page.json()["returned"] == 1
    assert page.json()["device_connections_opened"] == 0

    entities = client.get(
        "/api/network-model/entities",
        params={"environment_id": "pilot-a", "revision_id": "rev-001", "limit": 1},
    )
    assert entities.status_code == 200
    assert entities.json()["returned"] == 1
    assert entities.json()["device_connections_opened"] == 0


def test_ten_thousand_device_queries_are_bounded_and_summary_skips_model_blob(tmp_path: Path):
    repository = _repository(tmp_path)
    document = _document("rev-10k")
    document["model"]["devices"] = {
        f"edge-{index:05d}": {
            "site": f"site-{index // 100:03d}",
            "role": "edge",
            "platform": "arista_eos",
        }
        for index in range(10_000)
    }
    document["status"] = "approved"
    document["approval"] = {
        "status": "approved",
        "approved_by": "marcus",
        "approved_at": "2026-07-12T12:00:00Z",
    }
    repository.create_revision(document, created_by="marcus")
    repository.activate_revision("org-default", "pilot-a", "rev-10k")

    started = time.perf_counter()
    page = repository.list_entities(
        "org-default", "pilot-a", "rev-10k", entity_type="devices", limit=50
    )
    summary = repository.active_revision_summary("org-default", "pilot-a")
    elapsed = time.perf_counter() - started

    assert page["returned"] == 50
    assert page["next_cursor"]
    assert summary["revision_id"] == "rev-10k"
    assert "model" not in summary
    assert elapsed < 2.0


def test_conflicts_are_tenant_scoped_resolvable_and_reject_secret_shaped_resolution(tmp_path: Path):
    repository = _repository(tmp_path)
    repository.record_conflict(
        org_id="org-a",
        environment_id="pilot-a",
        conflict_id="identity-1",
        domain="identity",
        subject_id="edge-1",
        severity="high",
        details={"claimants": ["edge-1", "edge-one"]},
    )
    assert repository.list_conflicts("org-b", "pilot-a")["returned"] == 0
    with pytest.raises(ValueError, match="credential-shaped"):
        repository.resolve_conflict(
            "org-a",
            "pilot-a",
            "identity-1",
            resolved_by="marcus",
            resolution={"password": "must-not-enter-model"},
        )

    resolved = repository.resolve_conflict(
        "org-a",
        "pilot-a",
        "identity-1",
        resolved_by="marcus",
        resolution={"canonical_device_id": "edge-1"},
    )
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "marcus"
