from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NetworkModelError
from netcode.network_model_import import (
    _public_devices,
    import_approved_network_design,
    import_catalog_candidate,
)
from netcode.network_model_lifecycle import approve_with_git
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.runner_hub import enroll_runner, mint_join_token
from netcode.store import PlatformStore


def _store(tmp_path: Path) -> PlatformStore:
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    return PlatformStore(paths)


def _runner(store: PlatformStore):
    join = mint_join_token(store, "pilot")
    enrolled = enroll_runner(store, join["join_token"], "connector")
    return store.get_runner(enrolled["runner_id"])


def _device(index: int) -> dict:
    return {
        "id": f"edge-{index:03d}",
        "hostname": f"EDGE-{index:03d}",
        "host": f"192.0.2.{index + 1}",
        "platform": "arista_eos" if index % 2 == 0 else "junos",
        "site": f"site-{index // 10:03d}",
        "role": "edge",
        "groups": ["production"],
    }


def test_catalog_import_pages_without_connecting_and_is_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    runner = _runner(store)
    store.sync_runner_devices(runner, [_device(index) for index in range(120)], revision="catalog-120")

    first = import_catalog_candidate(
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="catalog-001",
        created_by="marcus",
    )
    second = import_catalog_candidate(
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="catalog-001",
        created_by="marcus",
    )

    assert first["created"] is True
    assert second["created"] is False
    assert len(first["revision"]["model"]["devices"]) == 120
    assert first["revision"]["status"] == "proposed"
    assert first["revision"]["authority_bindings"]["identity"]["mode"] == "propose"


def test_public_device_import_rejects_ambiguous_management_identity():
    with pytest.raises(NetworkModelError, match="claimed by both"):
        _public_devices(
            [
                {"id": "edge-a", "host": "router.example.net", "platform": "iosxe"},
                {"id": "edge-b", "host": "ROUTER.EXAMPLE.NET", "platform": "junos"},
            ],
            source_name="csv",
        )


def test_approved_rez_design_import_preserves_coverage_and_approval(tmp_path: Path):
    store = _store(tmp_path)
    repository = NetworkModelRepository(store)
    design = {
        "schema": "rez.network-design.v1",
        "namespace": "pilot-a",
        "revision": "design-001",
        "source": {"type": "git", "reference": "config/network_design.yaml"},
        "approval": {
            "status": "approved",
            "approved_by": "marcus",
            "approved_at": "2026-07-12T12:00:00Z",
        },
        "coverage": {"domains": ["topology", "routing", "reachability"]},
        "sites": {
            "site-101": {
                "archetype": "dual-edge-branch",
                "devices": {"edge-1": {"role": "edge"}},
                "reachability": [
                    {"id": "to-app", "source_device": "edge-1", "destination": "203.0.113.10"}
                ],
            }
        },
    }

    result = import_approved_network_design(
        repository,
        design,
        org_id="org_default",
        environment_id="pilot-a",
        created_by="marcus",
    )

    revision = result["revision"]
    assert revision["status"] == "approved"
    assert revision["approval"]["approved_by"] == "marcus"
    assert revision["coverage"]["domains"] == ["reachability", "routing", "topology"]
    assert revision["model"]["sites"]["site-101"]["archetype"] == "dual-edge-branch"


def test_same_revision_id_with_different_import_content_fails(tmp_path: Path):
    store = _store(tmp_path)
    runner = _runner(store)
    store.sync_runner_devices(runner, [_device(1)], revision="first")
    import_catalog_candidate(
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="catalog-001",
        created_by="marcus",
    )
    store.sync_runner_devices(runner, [_device(1), _device(2)], revision="second")

    with pytest.raises(NetworkModelError, match="different content"):
        import_catalog_candidate(
            store,
            org_id="org_default",
            environment_id="pilot-a",
            revision_id="catalog-001",
            created_by="marcus",
        )


def test_approved_design_cannot_invent_manual_authority_when_source_is_missing(tmp_path: Path):
    repository = NetworkModelRepository(_store(tmp_path))
    design = {
        "schema": "rez.network-design.v1",
        "namespace": "pilot-a",
        "revision": "design-unsafe",
        "approval": {
            "status": "approved",
            "approved_by": "marcus",
            "approved_at": "2026-07-12T12:00:00Z",
        },
        "coverage": {"domains": ["routing"]},
        "sites": {"site-101": {"devices": {"edge-1": {"role": "edge"}}}},
    }
    with pytest.raises(NetworkModelError, match="exact source.type"):
        import_approved_network_design(
            repository,
            design,
            org_id="org_default",
            environment_id="pilot-a",
            created_by="marcus",
        )


def test_human_approval_converts_discovery_proposal_to_reviewed_intent(tmp_path: Path):
    store = _store(tmp_path)
    runner = _runner(store)
    store.sync_runner_devices(runner, [_device(1)], revision="first")
    imported = import_catalog_candidate(
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="catalog-reviewed",
        created_by="marcus",
    )

    approved = approve_with_git(
        NetworkModelRepository(store),
        org_id="org_default",
        environment_id="pilot-a",
        revision_id=imported["revision"]["revision_id"],
        approved_by="marcus",
        git_root=WorkspacePaths(tmp_path).git_workspace,
    )["revision"]

    assert approved["status"] == "approved"
    assert approved["source"]["type"] == "manual_review"
    assert approved["source"]["reference"].startswith("reviewed:discovery:device-catalog:")
    assert approved["authority_bindings"]["identity"] == {
        "source": "manual_review",
        "mode": "authoritative",
    }


def test_catalog_import_records_conflicts_without_approving_ambiguous_identity(tmp_path: Path):
    store = _store(tmp_path)
    runner = _runner(store)
    first = _device(1)
    second = _device(2)
    first["host"] = "duplicate.example.net"
    second["host"] = "DUPLICATE.EXAMPLE.NET"
    second.pop("site")
    store.sync_runner_devices(runner, [first, second], revision="ambiguous")

    imported = import_catalog_candidate(
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="catalog-conflicts",
        created_by="marcus",
    )
    conflicts = NetworkModelRepository(store).list_conflicts(
        "org_default", "pilot-a", status="open"
    )["conflicts"]

    assert imported["revision"]["status"] == "proposed"
    assert imported["conflicts"] == 2
    assert {item["domain"] for item in conflicts} == {"identity", "sites"}
    assert all(item["status"] == "open" for item in conflicts)
    assert all("host" not in device for device in imported["revision"]["model"]["devices"].values())


def test_catalog_api_generates_revision_and_preserves_delegated_reviewer(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path)
    runner = _runner(store)
    store.sync_runner_devices(runner, [_device(1)], revision="clean")
    client = TestClient(api.app)

    imported = client.post(
        "/api/network-model/import/catalog",
        json={"environment_id": "pilot-a", "reviewed_by": "marcus"},
    )
    assert imported.status_code == 200, imported.text
    revision_id = imported.json()["revision"]["revision_id"]
    assert revision_id.startswith("catalog-")

    approved = client.post(
        f"/api/network-model/revisions/{revision_id}/approve",
        json={"environment_id": "pilot-a", "reviewed_by": "marcus"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["revision"]["approval"]["approved_by"] == "marcus"


def test_conflicted_catalog_revision_cannot_be_approved_even_after_acknowledgement(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path)
    runner = _runner(store)
    device = _device(1)
    device.pop("site")
    store.sync_runner_devices(runner, [device], revision="missing-site")
    client = TestClient(api.app)

    imported = client.post(
        "/api/network-model/import/catalog",
        json={"environment_id": "pilot-a", "revision_id": "catalog-needs-review"},
    )
    assert imported.status_code == 200
    conflicts = client.get(
        "/api/network-model/conflicts",
        params={"environment_id": "pilot-a"},
    ).json()["conflicts"]
    assert len(conflicts) == 1
    resolved = client.post(
        f"/api/network-model/conflicts/{conflicts[0]['conflict_id']}/resolve",
        json={
            "environment_id": "pilot-a",
            "resolution": {"action": "corrected_in_source"},
        },
    )
    assert resolved.status_code == 200

    blocked = client.post(
        "/api/network-model/revisions/catalog-needs-review/approve",
        json={"environment_id": "pilot-a"},
    )
    assert blocked.status_code == 409
    assert "fresh proposal" in blocked.json()["detail"]
