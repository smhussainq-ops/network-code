from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA, NetworkModelError
from netcode.network_model_lifecycle import (
    activate_verified_revision,
    approve_with_git,
    create_candidate_from_change,
    rollback_active_revision,
)
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore
from netcode.yamlio import write_yaml


def _setup(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    return workspace, store, NetworkModelRepository(store)


def _baseline() -> dict:
    return {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": "org_default",
        "environment_id": "pilot-a",
        "revision_id": "baseline-001",
        "status": "proposed",
        "source": {"type": "manual_review", "reference": "initial-review"},
        "coverage": {"domains": ["identity", "sites", "routing"]},
        "authority_bindings": {
            domain: {"source": "manual_review", "mode": "propose"}
            for domain in ("identity", "sites", "routing")
        },
        "model": {
            "sites": {"site-101": {"devices": {"edge-1": {"role": "edge"}}}},
            "devices": {"edge-1": {"site": "site-101", "role": "edge"}},
        },
    }


def _change(store: PlatformStore, workspace: WorkspacePaths, name: str = "change"):
    intent = workspace.intents / f"{name}.yaml"
    write_yaml(
        intent,
        {
            "change_type": "custom_config",
            "site": "site-101",
            "targets": {"device_ids": ["edge-1"]},
            "custom": {"config_lines": "router bgp 65001"},
            "metadata": {"source": "netcode", "title": name},
        },
    )
    return store.create_change(intent, "edge-1", requested_by="marcus")


def test_first_baseline_requires_review_and_creates_git_checkpoint(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")

    approved = approve_with_git(
        repository,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="baseline-001",
        approved_by="marcus",
        git_root=workspace.git_workspace,
    )
    active = activate_verified_revision(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="baseline-001",
        actor="marcus",
        git_root=workspace.git_workspace,
        initial_baseline=True,
    )

    assert approved["revision"]["status"] == "approved"
    assert approved["git"]["commit"]
    assert active["revision"]["status"] == "active"
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"
    revision_dir = workspace.git_workspace / "network-model" / "pilot-a" / "revisions" / "baseline-001"
    assert (revision_dir / "model.json").is_file()
    assert (revision_dir / "verification.json").is_file()


def test_model_checkpoint_upgrades_legacy_gitignore(tmp_path: Path):
    workspace, _store, repository = _setup(tmp_path)
    workspace.git_workspace.mkdir(parents=True, exist_ok=True)
    (workspace.git_workspace / ".gitignore").write_text(
        "*\n!.gitignore\n!README.md\n!changes/\n!changes/**\n"
    )
    repository.create_revision(_baseline(), created_by="marcus")

    result = approve_with_git(
        repository,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="baseline-001",
        approved_by="marcus",
        git_root=workspace.git_workspace,
    )

    assert result["git"]["commit"]
    assert "!network-model/**" in (workspace.git_workspace / ".gitignore").read_text()


def test_failed_or_unverified_change_cannot_promote_candidate(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="marcus", git_root=workspace.git_workspace, initial_baseline=True)
    change = _change(store, workspace)
    create_candidate_from_change(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        parent_revision_id="baseline-001",
        revision_id="candidate-002",
        change_id=change.id,
        domains=["routing"],
        model_patch={"devices": {"edge-1": {"intent": {"routing": {"bgp": {"asn": 65001}}}}}},
        created_by="marcus",
    )
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", approved_by="reviewer", git_root=workspace.git_workspace)

    with pytest.raises(NetworkModelError, match="fresh successful verification"):
        activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", actor="reviewer", git_root=workspace.git_workspace)
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"

    store.update_change(change.id, "failed", {"error": "verify failed"}, workflow_state="failed")
    with pytest.raises(NetworkModelError, match="fresh successful verification"):
        activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", actor="reviewer", git_root=workspace.git_workspace)
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"


def test_verified_change_promotes_and_verified_rollback_restores_previous_model(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="marcus", git_root=workspace.git_workspace, initial_baseline=True)
    change = _change(store, workspace, "routing-update")
    create_candidate_from_change(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        parent_revision_id="baseline-001",
        revision_id="candidate-002",
        change_id=change.id,
        domains=["routing"],
        model_patch={"devices": {"edge-1": {"intent": {"routing": {"bgp": {"asn": 65001}}}}}},
        created_by="marcus",
    )
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", approved_by="reviewer", git_root=workspace.git_workspace)
    store.update_change(change.id, "completed", {"verify": "pass"}, workflow_state="verified")
    activated = activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", actor="reviewer", git_root=workspace.git_workspace)

    assert activated["revision"]["status"] == "active"
    assert repository.get_revision("org_default", "pilot-a", "baseline-001")["status"] == "superseded"

    rollback_change = _change(store, workspace, "rollback-routing")
    store.update_change(rollback_change.id, "completed", {"rollback": "pass"}, workflow_state="rolled_back")
    restored = rollback_active_revision(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        target_revision_id="baseline-001",
        rollback_change_id=rollback_change.id,
        actor="reviewer",
        git_root=workspace.git_workspace,
    )
    assert restored["revision"]["revision_id"] == "baseline-001"
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"


def test_active_model_drives_scoped_plan_without_returning_full_model_on_summary(tmp_path: Path, monkeypatch):
    workspace, store, repository = _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    baseline = _baseline()
    baseline["model"]["organization_standard"] = {"topology": {"enabled": True}}
    baseline["coverage"]["domains"].append("topology")
    baseline["authority_bindings"]["topology"] = {
        "source": "manual_review",
        "mode": "propose",
    }
    repository.create_revision(baseline, created_by="marcus")
    approve_with_git(
        repository,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="baseline-001",
        approved_by="marcus",
        git_root=workspace.git_workspace,
    )
    activate_verified_revision(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="baseline-001",
        actor="marcus",
        git_root=workspace.git_workspace,
        initial_baseline=True,
    )
    client = TestClient(api.app)

    summary = client.get("/api/network-model/active", params={"environment_id": "pilot-a"})
    assert summary.status_code == 200
    assert summary.json()["revision"]["revision_id"] == "baseline-001"
    assert "model" not in summary.json()["revision"]

    plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "interface_config",
            "site": "site-101",
            "device_id": "edge-1",
            "requested_by": "marcus",
            "environment_id": "pilot-a",
            "model_revision_id": "baseline-001",
            "values": {
                "name": "Ethernet1",
                "description": "WAN uplink",
                "enabled": True,
                "mode": "routed",
                "ip_address": "192.0.2.1/30",
            },
        },
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["network_model"]["revision_id"] == "baseline-001"
    assert plan.json()["network_model"]["device_id"] == "edge-1"


def test_rez_bridge_token_can_read_model_but_cannot_approve_it(monkeypatch):
    monkeypatch.setenv("NETCODE_REZ_BRIDGE_TOKEN", "bridge-secret")
    authorization = "Bearer bridge-secret"
    assert api._is_rez_bridge_request("/api/network-model/active/rez-design", authorization) is True
    assert api._is_rez_bridge_request("/api/network-model/revisions/rev-1/approve", authorization) is False
