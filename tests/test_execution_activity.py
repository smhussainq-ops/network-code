from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import sign_result, submit_job_progress, submit_job_result
from netcode.store import DEFAULT_ORG_ID, PlatformStore


def _workspace(tmp_path: Path, monkeypatch) -> tuple[WorkspacePaths, PlatformStore, TestClient]:
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    return paths, PlatformStore(paths), TestClient(api.app)


def test_signed_progress_is_idempotent_append_only_and_redacted(tmp_path: Path, monkeypatch) -> None:
    paths, store, _client = _workspace(tmp_path, monkeypatch)
    intent_path = paths.intents / "progress.yaml"
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text("change_type: custom_config\n", encoding="utf-8")
    change = store.create_change(intent_path, "edge-1")
    runner = store.create_runner(
        "runner-1",
        "store-lab",
        hashlib.sha256(b"token").hexdigest(),
        "runner-hmac",
    )
    job = store.queue_job(
        change.id,
        "lab_apply",
        "store-lab",
        {"action": "apply", "device": {"id": "edge-1"}},
        target_runner_id=runner.id,
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, "store-lab", runner.id)
    assert claimed is not None

    event = {
        "event_id": str(uuid.uuid4()),
        "sequence": 2,
        "phase": "apply",
        "stage": "commands_applied",
        "status": "running",
        "message": "Accepted command 1 of 2.",
        "device_id": "edge-1",
        "current_step": 1,
        "total_steps": 2,
        "command": "snmp-server community hunter2 ro",
    }
    accepted = submit_job_progress(
        store, runner, job.id, event, sign_result("runner-hmac", event), claimed.lease_token
    )
    replay = submit_job_progress(
        store, runner, job.id, event, sign_result("runner-hmac", event), claimed.lease_token
    )

    assert accepted["ok"] is True
    assert replay["ok"] is True
    events = store.list_execution_events(change.id)
    assert [item.stage for item in events] == ["claimed", "commands_applied"]
    assert events[-1].command == "snmp-server <redacted>"
    assert store.get_change(change.id).workflow_state == "draft"

    terminal = dict(
        event,
        event_id=str(uuid.uuid4()),
        sequence=3,
        stage="passed",
        status="passed",
        message="Apply passed.",
        command=None,
        current_step=None,
        total_steps=None,
    )
    assert submit_job_progress(
        store, runner, job.id, terminal, sign_result("runner-hmac", terminal), claimed.lease_token
    )["ok"]
    result = {"status": "pass", "message": "Apply passed."}
    assert submit_job_result(
        store, runner, job.id, result, sign_result("runner-hmac", result), claimed.lease_token
    )["ok"]
    assert [item.stage for item in store.list_execution_events(change.id)].count("passed") == 1


def test_progress_rejects_bad_signature_and_wrong_phase(tmp_path: Path, monkeypatch) -> None:
    paths, store, _client = _workspace(tmp_path, monkeypatch)
    intent_path = paths.intents / "progress.yaml"
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text("change_type: custom_config\n", encoding="utf-8")
    change = store.create_change(intent_path, "edge-1")
    runner = store.create_runner("runner-1", "store-lab", "token-hash", "runner-hmac")
    job = store.queue_job(
        change.id,
        "lab_dry-run",
        "store-lab",
        {"action": "dry-run", "device": {"id": "edge-1"}},
        target_runner_id=runner.id,
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, "store-lab", runner.id)
    assert claimed is not None
    event = {
        "event_id": str(uuid.uuid4()),
        "sequence": 2,
        "phase": "apply",
        "stage": "connected",
        "status": "running",
        "message": "connected",
        "device_id": "edge-1",
    }

    assert submit_job_progress(store, runner, job.id, event, "bad", claimed.lease_token)["ok"] is False
    assert submit_job_progress(
        store, runner, job.id, event, sign_result("runner-hmac", event), claimed.lease_token
    )["ok"] is False
    malformed_id = dict(event, event_id="not-a-uuid", phase="dry-run")
    malformed_result = submit_job_progress(
        store,
        runner,
        job.id,
        malformed_id,
        sign_result("runner-hmac", malformed_id),
        claimed.lease_token,
    )
    assert malformed_result["ok"] is False
    assert "UUID" in malformed_result["message"]
    assert [item.stage for item in store.list_execution_events(change.id)] == ["claimed"]


def test_discovery_read_job_accepts_signed_discovery_progress(tmp_path: Path, monkeypatch) -> None:
    _paths, store, _client = _workspace(tmp_path, monkeypatch)
    runner = store.create_runner("runner-1", "store-lab", "token-hash", "runner-hmac")
    job = store.create_read_job(
        DEFAULT_ORG_ID,
        runner.pool,
        "rez_discover_network",
        {"seed_node": "192.0.2.10", "depth": 0},
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None
    event = {
        "event_id": str(uuid.uuid4()),
        "sequence": 2,
        "phase": "discovery",
        "stage": "scope_validated",
        "status": "running",
        "message": "Discovery scope validated.",
        "device_id": "",
    }

    accepted = submit_job_progress(
        store,
        runner,
        job.id,
        event,
        sign_result("runner-hmac", event),
        claimed.lease_token,
    )

    assert accepted["ok"] is True
    assert store.list_execution_events("__read__")[-1].stage == "scope_validated"

    skipped = {
        **event,
        "event_id": str(uuid.uuid4()),
        "sequence": 3,
        "stage": "device_skipped",
        "status": "skipped",
        "message": "No reachable endpoint was found at this sweep address.",
    }
    skipped_result = submit_job_progress(
        store,
        runner,
        job.id,
        skipped,
        sign_result("runner-hmac", skipped),
        claimed.lease_token,
    )

    assert skipped_result["ok"] is True
    assert store.list_execution_events("__read__")[-1].status == "skipped"


def test_change_activity_and_paginated_fleet_search(tmp_path: Path, monkeypatch) -> None:
    paths, store, client = _workspace(tmp_path, monkeypatch)
    rollout = store.create_rollout(
        description="Fleet change",
        change_type="custom_config",
        values={"config_lines": "description TEST"},
        canary_size=1,
        batch_size=2,
    )
    change_ids: dict[str, str] = {}
    for index, (device, status) in enumerate((
        ("edge-01", "running"),
        ("edge-02", "passed"),
        ("edge-03", "failed"),
        ("edge-04", "pending"),
    )):
        intent_path = paths.intents / f"{device}.yaml"
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text("change_type: custom_config\n", encoding="utf-8")
        change = store.create_change(intent_path, device)
        change_ids[device] = change.id
        store.add_rollout_target(rollout["id"], device, 0 if index == 0 else 1, change.id, str(intent_path))
        store.update_rollout_target(rollout["id"], device, status=status, stage="apply")
    job = store.create_job(change_ids["edge-01"], "lab_apply")
    for sequence in range(35):
        store.record_execution_event(
            event_id=str(uuid.uuid4()),
            job_id=job.id,
            change_id=change_ids["edge-01"],
            org_id=DEFAULT_ORG_ID,
            device_id="edge-01",
            phase="apply",
            stage="connected" if sequence == 0 else f"check_{sequence}",
            status="running",
            message="connected" if sequence == 0 else f"check {sequence}",
            sequence=sequence,
        )

    activity = client.get(f"/api/change/{change_ids['edge-01']}/activity")
    assert activity.status_code == 200
    assert activity.json()["events"][0]["stage"] == "connected"

    page = client.get(
        f"/api/fleet/rollouts/{rollout['id']}/activity",
        params={"status": "running", "q": "edge", "limit": 1, "include_events": "true"},
    )
    assert page.status_code == 200
    body = page.json()
    assert body["page"] == {
        "limit": 1,
        "offset": 0,
        "returned": 1,
        "filtered_total": 1,
        "has_more": False,
    }
    assert body["rollout"]["category_counts"] == {
        "all": 4,
        "running": 1,
        "passed": 1,
        "failed": 1,
        "untouched": 1,
    }
    assert body["targets"][0]["device_id"] == "edge-01"
    assert len(body["events_by_device"]["edge-01"]) == 35
    assert body["events_by_device"]["edge-01"][0]["stage"] == "connected"
