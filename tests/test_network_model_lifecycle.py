from __future__ import annotations

from pathlib import Path
import subprocess

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
