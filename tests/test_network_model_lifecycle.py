from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA, NetworkModelError
from netcode.network_model_lifecycle import (
    activate_verified_revision,
    approve_change_candidates,
    approve_with_git,
    assert_change_model_rollback_is_current,
    create_candidate_for_change_intent,
    create_candidate_from_change,
    model_patch_from_intent,
    rollback_active_revision,
    rollback_change_candidates,
)
from netcode.gitflow import setup_git_workspace
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


def _approved_reviewed_revision(
    revision_id: str = "reviewed-002",
    *,
    source_type: str = "manual_review",
    org_id: str = "org_default",
    environment_id: str = "pilot-a",
) -> dict:
    revision = copy.deepcopy(_baseline())
    revision.update(
        {
            "org_id": org_id,
            "environment_id": environment_id,
            "revision_id": revision_id,
            "status": "approved",
            "source": {"type": source_type, "reference": f"approved:{revision_id}"},
            "approval": {
                "status": "approved",
                "approved_by": "design-reviewer",
                "approved_at": "2026-07-20T03:00:00Z",
            },
        }
    )
    revision["authority_bindings"] = {
        domain: {"source": source_type, "mode": "authoritative"}
        for domain in revision["coverage"]["domains"]
    }
    revision["model"]["sites"]["site-101"]["reviewed_marker"] = revision_id
    return revision


def _persist_approved_review(
    repository: NetworkModelRepository,
    workspace: WorkspacePaths,
    *,
    revision_id: str = "reviewed-002",
    source_type: str = "manual_review",
) -> None:
    repository.create_revision(
        _approved_reviewed_revision(revision_id=revision_id, source_type=source_type),
        created_by="usr_admin",
    )
    approve_with_git(
        repository,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id=revision_id,
        approved_by="usr_admin",
        git_root=workspace.git_workspace,
    )


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

    store.update_change(change.id, "completed", {"rollback": "pass"}, workflow_state="rolled_back")
    restored = rollback_change_candidates(
        repository,
        store,
        org_id="org_default",
        change_id=change.id,
        actor="reviewer",
        git_root=workspace.git_workspace,
    )
    assert restored[0]["revision"]["revision_id"] == "baseline-001"
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"


def test_model_rollback_rejects_unrelated_change_and_non_parent_target(tmp_path: Path):
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
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", actor="reviewer", git_root=workspace.git_workspace)
    unrelated = _change(store, workspace, "unrelated-rollback")
    store.update_change(unrelated.id, "completed", {"rollback": "pass"}, workflow_state="rolled_back")

    with pytest.raises(NetworkModelError, match="not linked"):
        rollback_active_revision(
            repository,
            store,
            org_id="org_default",
            environment_id="pilot-a",
            target_revision_id="baseline-001",
            rollback_change_id=unrelated.id,
            actor="reviewer",
            git_root=workspace.git_workspace,
        )


def test_stale_change_rollback_is_blocked_after_newer_model_activation(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="marcus", git_root=workspace.git_workspace, initial_baseline=True)

    first = _change(store, workspace, "first")
    create_candidate_from_change(repository, store, org_id="org_default", environment_id="pilot-a", parent_revision_id="baseline-001", revision_id="candidate-002", change_id=first.id, domains=["routing"], model_patch={"first": True}, created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", approved_by="reviewer", git_root=workspace.git_workspace)
    store.update_change(first.id, "completed", {"verify": "pass"}, workflow_state="verified")
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="candidate-002", actor="reviewer", git_root=workspace.git_workspace)

    second = _change(store, workspace, "second")
    create_candidate_from_change(repository, store, org_id="org_default", environment_id="pilot-a", parent_revision_id="candidate-002", revision_id="candidate-003", change_id=second.id, domains=["routing"], model_patch={"second": True}, created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="candidate-003", approved_by="reviewer", git_root=workspace.git_workspace)
    store.update_change(second.id, "completed", {"verify": "pass"}, workflow_state="verified")
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="candidate-003", actor="reviewer", git_root=workspace.git_workspace)

    with pytest.raises(NetworkModelError, match="superseded Network Model"):
        assert_change_model_rollback_is_current(
            repository,
            org_id="org_default",
            change_id=first.id,
        )


def test_typed_change_lifecycle_uses_human_approval_and_live_verification(tmp_path: Path, monkeypatch):
    workspace, store, repository = _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
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
    intent_path = workspace.intents / "restore-edge1.yaml"
    intent = {
        "change_type": "interface_config",
        "site": "site-101",
        "targets": {"device_ids": ["edge-1"]},
        "interface": {"name": "Ethernet1", "enabled": True, "apply_scope": "admin_state"},
        "metadata": {"source": "rez_rca", "raw_commands": "must-not-enter-model"},
    }
    write_yaml(intent_path, intent)
    change = store.create_change(intent_path, "edge-1", requested_by="rez-rca")
    store.update_change(change.id, "validated", {"source": "rez_rca"}, workflow_state="dry_run_passed")
    candidate = create_candidate_for_change_intent(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        parent_revision_id="baseline-001",
        change_id=change.id,
        intent=intent,
        device_id="edge-1",
        created_by="rez-rca",
    )

    assert candidate is not None
    modeled = candidate["model"]["sites"]["site-101"]["devices"]["edge-1"]["intent"]
    assert modeled["topology"]["interfaces"]["Ethernet1"]["enabled"] is True
    assert "raw_commands" not in str(modeled)
    dependencies = candidate["model"]["sites"]["site-101"]["operational_dependencies"]
    assert dependencies == [
        {
            "id": "edge-1:interface:ethernet1",
            "device_id": "edge-1",
            "kind": "interface",
            "domain": "topology",
            "identity": {"interface": "Ethernet1"},
            "expected": {"admin_state": "up", "oper_state": "up"},
            "atom_ids": ["L1_INTERFACE_ADMIN_DOWN", "L1_INTERFACE_OPER_DOWN"],
            "remediation": {
                "root_atom_id": "L1_INTERFACE_ADMIN_DOWN",
                "change_type": "interface_config",
                "values": {
                    "interface": "Ethernet1",
                    "enabled": True,
                    "apply_scope": "admin_state",
                },
                "interface": {
                    "name": "Ethernet1",
                    "enabled": True,
                    "apply_scope": "admin_state",
                },
            },
        }
    ]

    client = TestClient(api.app)
    approval = client.post(f"/api/change/{change.id}/approve", json={"approved_by": "reviewer"})
    assert approval.status_code == 200, approval.text
    assert repository.get_revision("org_default", "pilot-a", candidate["revision_id"])["status"] == "approved"

    lifecycle = api._persist_intent_verification(
        store,
        change_id=change.id,
        verification={"ok": True, "message": "Interface state verified live."},
        passed=True,
        actor="reviewer",
    )
    assert lifecycle["ok"] is True
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == candidate["revision_id"]
    assert store.get_change(change.id).workflow_state == "verified"


def test_failed_live_verification_cannot_activate_typed_candidate(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="marcus", git_root=workspace.git_workspace, initial_baseline=True)
    intent_path = workspace.intents / "failed-edge1.yaml"
    intent = {
        "change_type": "interface_config",
        "site": "site-101",
        "targets": {"device_ids": ["edge-1"]},
        "interface": {"name": "Ethernet1", "enabled": True, "apply_scope": "admin_state"},
    }
    write_yaml(intent_path, intent)
    change = store.create_change(intent_path, "edge-1", requested_by="rez-rca")
    store.update_change(change.id, "validated", {}, workflow_state="dry_run_passed")
    candidate = create_candidate_for_change_intent(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        parent_revision_id="baseline-001",
        change_id=change.id,
        intent=intent,
        device_id="edge-1",
        created_by="rez-rca",
    )
    assert candidate is not None
    approve_change_candidates(
        repository,
        org_id="org_default",
        change_id=change.id,
        approved_by="reviewer",
        git_root=workspace.git_workspace,
    )

    lifecycle = api._persist_intent_verification(
        store,
        change_id=change.id,
        verification={"ok": False, "message": "Expected state was not observed."},
        passed=False,
        actor="reviewer",
    )

    assert lifecycle["ok"] is True
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"
    assert repository.get_revision("org_default", "pilot-a", candidate["revision_id"])["status"] == "approved"


def test_unstructured_cli_never_becomes_network_model_intent():
    patch, domains = model_patch_from_intent(
        {
            "change_type": "custom_config",
            "site": "site-101",
            "custom": {"config_lines": "username hidden secret value"},
        },
        device_id="edge-1",
    )
    assert patch == {}
    assert domains == []


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


def test_admin_reviewed_intent_update_activates_with_git_audit_and_exact_revision_ids(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
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
        actor="usr_marcus",
        git_root=workspace.git_workspace,
        initial_baseline=True,
    )
    _persist_approved_review(repository, workspace)

    result = activate_verified_revision(
        repository,
        store,
        org_id="org_default",
        environment_id="pilot-a",
        revision_id="reviewed-002",
        actor="usr_admin",
        actor_display="admin@example.com",
        git_root=workspace.git_workspace,
        reviewed_intent_update=True,
        expected_current_revision_id="baseline-001",
    )

    assert result["revision"]["status"] == "active"
    assert result["verification"]["reviewed_intent_update"] is True
    assert result["verification"]["verified_by"] == "usr_admin"
    assert result["verification"]["verified_by_display"] == "admin@example.com"
    assert result["verification"]["previous_revision_id"] == "baseline-001"
    assert result["verification"]["activated_revision_id"] == "reviewed-002"
    assert result["verification"]["approved_source"] == {
        "type": "manual_review",
        "reference": "approved:reviewed-002",
    }
    assert result["verification"]["approval"]["status"] == "approved"
    assert result["git"]["commit"]
    link = next(
        item
        for item in repository.list_links("org_default", "pilot-a", "reviewed-002")
        if item["link_type"] == "git_verification"
    )
    assert link["external_id"] == result["git"]["commit"]
    assert link["metadata"]["previous_revision_id"] == "baseline-001"
    verification_path = (
        workspace.git_workspace
        / "network-model"
        / "pilot-a"
        / "revisions"
        / "reviewed-002"
        / "verification.json"
    )
    assert json.loads(verification_path.read_text())["activated_revision_id"] == "reviewed-002"


def test_reviewed_intent_update_rejects_stale_head_and_preserves_active_revision(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="usr_marcus", git_root=workspace.git_workspace, initial_baseline=True)
    _persist_approved_review(repository, workspace)

    with pytest.raises(NetworkModelError, match="changed from stale-001 to baseline-001"):
        activate_verified_revision(
            repository,
            store,
            org_id="org_default",
            environment_id="pilot-a",
            revision_id="reviewed-002",
            actor="usr_admin",
            git_root=workspace.git_workspace,
            reviewed_intent_update=True,
            expected_current_revision_id="stale-001",
        )

    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"
    assert repository.get_revision("org_default", "pilot-a", "reviewed-002")["status"] == "approved"


def test_reviewed_intent_update_rejects_revision_tampered_after_approval(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="usr_marcus", git_root=workspace.git_workspace, initial_baseline=True)
    _persist_approved_review(repository, workspace)

    tampered = repository.get_revision("org_default", "pilot-a", "reviewed-002")["model"]
    tampered["sites"]["site-101"]["reviewed_marker"] = "tampered-after-approval"
    with store._connect() as conn:
        conn.execute(
            "UPDATE network_model_revisions SET model_json = ? "
            "WHERE org_id = ? AND environment_id = ? AND revision_id = ?",
            (json.dumps(tampered, sort_keys=True), "org_default", "pilot-a", "reviewed-002"),
        )

    with pytest.raises(NetworkModelError, match="content-integrity check"):
        activate_verified_revision(
            repository,
            store,
            org_id="org_default",
            environment_id="pilot-a",
            revision_id="reviewed-002",
            actor="usr_admin",
            git_root=workspace.git_workspace,
            reviewed_intent_update=True,
            expected_current_revision_id="baseline-001",
        )
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"


def test_atomic_head_compare_and_swap_rejects_a_racing_activation(tmp_path: Path):
    workspace, _store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    repository.activate_revision("org_default", "pilot-a", "baseline-001")
    repository.create_revision(_approved_reviewed_revision(), created_by="usr_admin")

    with pytest.raises(ValueError, match="changed from stale-001 to baseline-001"):
        repository.activate_revision(
            "org_default",
            "pilot-a",
            "reviewed-002",
            expected_current_revision_id="stale-001",
        )

    assert repository.active_revision("org_default", "pilot-a")["revision_id"] == "baseline-001"

    repository.activate_revision(
        "org_default",
        "pilot-a",
        "reviewed-002",
        expected_current_revision_id="baseline-001",
    )
    with pytest.raises(ValueError, match="changed from baseline-001 to reviewed-002"):
        repository.activate_revision(
            "org_default",
            "pilot-a",
            "reviewed-002",
            expected_current_revision_id="baseline-001",
        )


def test_atomic_head_compare_and_swap_allows_only_one_concurrent_winner(tmp_path: Path):
    workspace, _store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    repository.activate_revision("org_default", "pilot-a", "baseline-001")
    for revision_id in ("reviewed-a", "reviewed-b"):
        repository.create_revision(
            _approved_reviewed_revision(revision_id=revision_id),
            created_by="usr_admin",
        )

    ready = threading.Barrier(2)

    def activate(revision_id: str):
        ready.wait(timeout=5)
        return repository.activate_revision(
            "org_default",
            "pilot-a",
            revision_id,
            expected_current_revision_id="baseline-001",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(activate, revision_id) for revision_id in ("reviewed-a", "reviewed-b")]
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except Exception as exc:  # The losing compare-and-swap must fail closed.
                outcomes.append(exc)

    winners = [item for item in outcomes if isinstance(item, dict)]
    losers = [item for item in outcomes if isinstance(item, ValueError)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert "changed from baseline-001" in str(losers[0])
    assert repository.active_revision("org_default", "pilot-a")["revision_id"] in {
        "reviewed-a",
        "reviewed-b",
    }


def test_netcode_change_revision_cannot_bypass_verified_change_evidence(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="usr_marcus", git_root=workspace.git_workspace, initial_baseline=True)
    _persist_approved_review(repository, workspace, source_type="netcode_change")

    with pytest.raises(NetworkModelError, match="approved Git or manual-review source"):
        activate_verified_revision(
            repository,
            store,
            org_id="org_default",
            environment_id="pilot-a",
            revision_id="reviewed-002",
            actor="usr_admin",
            git_root=workspace.git_workspace,
            reviewed_intent_update=True,
            expected_current_revision_id="baseline-001",
        )


def test_device_observation_cannot_be_imported_as_approved_intent(tmp_path: Path):
    _workspace, _store, repository = _setup(tmp_path)
    with pytest.raises(NetworkModelError, match="observation-only source 'device' authoritative"):
        repository.create_revision(
            _approved_reviewed_revision(source_type="device"),
            created_by="usr_admin",
        )


def test_reviewed_intent_update_rejects_unapproved_and_cross_tenant_revisions(tmp_path: Path):
    workspace, store, repository = _setup(tmp_path)
    repository.create_revision(_baseline(), created_by="marcus")
    approve_with_git(repository, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", approved_by="marcus", git_root=workspace.git_workspace)
    activate_verified_revision(repository, store, org_id="org_default", environment_id="pilot-a", revision_id="baseline-001", actor="usr_marcus", git_root=workspace.git_workspace, initial_baseline=True)
    proposed = _baseline()
    proposed["revision_id"] = "proposed-002"
    repository.create_revision(proposed, created_by="usr_admin")

    with pytest.raises(NetworkModelError, match="only an approved revision"):
        activate_verified_revision(
            repository,
            store,
            org_id="org_default",
            environment_id="pilot-a",
            revision_id="proposed-002",
            actor="usr_admin",
            git_root=workspace.git_workspace,
            reviewed_intent_update=True,
            expected_current_revision_id="baseline-001",
        )
    with pytest.raises(KeyError, match="Unknown network model revision"):
        activate_verified_revision(
            repository,
            store,
            org_id="org_other",
            environment_id="pilot-a",
            revision_id="proposed-002",
            actor="usr_admin",
            git_root=workspace.git_workspace,
            reviewed_intent_update=True,
            expected_current_revision_id="baseline-001",
        )


def test_reviewed_activation_api_binds_actor_to_authenticated_admin_identity(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}
    principal = api.Principal(
        kind="user",
        org_id="org_default",
        role="admin",
        user_id="usr_admin",
        email="admin@example.com",
    )
    monkeypatch.setattr(api, "_request_principal", lambda _request: principal)

    def activate(*_args, **kwargs):
        captured.update(kwargs)
        return {"revision": {"revision_id": "reviewed-002", "status": "active"}}

    monkeypatch.setattr(api, "activate_verified_revision", activate)
    response = TestClient(api.app).post(
        "/api/network-model/revisions/reviewed-002/activate",
        json={
            "environment_id": "pilot-a",
            "reviewed_intent_update": True,
            "expected_current_revision_id": "baseline-001",
            "reviewed_by": "spoofed-client-actor",
        },
    )

    assert response.status_code == 200, response.text
    assert captured["actor"] == "usr_admin"
    assert captured["actor_display"] == "admin@example.com"
    assert captured["org_id"] == "org_default"
    assert captured["expected_current_revision_id"] == "baseline-001"


@pytest.mark.parametrize(
    ("principal", "status"),
    [
        (api.Principal(kind="user", org_id="org_default", role="operator", user_id="usr_operator"), 403),
        (api.Principal(kind="system", org_id="org_default", role="admin"), 401),
    ],
)
def test_reviewed_activation_api_rejects_non_admin_or_unstable_identity(
    principal: api.Principal,
    status: int,
    tmp_path: Path,
    monkeypatch,
):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "_request_principal", lambda _request: principal)
    response = TestClient(api.app).post(
        "/api/network-model/revisions/reviewed-002/activate",
        json={
            "environment_id": "pilot-a",
            "reviewed_intent_update": True,
            "expected_current_revision_id": "baseline-001",
        },
    )
    assert response.status_code == status


def test_change_history_is_an_isolated_repo_even_inside_parent_worktree(tmp_path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    history = tmp_path / ".netcode" / "change-history"

    result = setup_git_workspace(history)

    assert result["ok"] is True
    assert (history / ".git").exists()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=history,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(top) == history
