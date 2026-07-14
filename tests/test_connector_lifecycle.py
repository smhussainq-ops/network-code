from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import poll_for_job
from netcode.store import DEFAULT_ORG_ID, JobQueueFullError, PlatformStore


def _store(tmp_path: Path) -> PlatformStore:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    return PlatformStore(workspace)


def _runner(store: PlatformStore, name: str = "connector-1", *, org_id: str = DEFAULT_ORG_ID):
    return store.create_runner(
        name,
        "pilot",
        hashlib.sha256(name.encode("utf-8")).hexdigest(),
        f"{name}-hmac",
        org_id=org_id,
    )


def test_queue_capacity_rejects_only_new_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETCODE_MAX_QUEUED_JOBS", "100")
    store = _store(tmp_path)
    runner = _runner(store)
    first = store.create_read_job(
        DEFAULT_ORG_ID,
        runner.pool,
        "verify",
        {"device_id": "edge-0"},
        idempotency_key="stable-read-operation",
    )
    for index in range(1, 100):
        store.create_read_job(
            DEFAULT_ORG_ID,
            runner.pool,
            "verify",
            {"device_id": f"edge-{index}"},
        )

    repeated = store.create_read_job(
        DEFAULT_ORG_ID,
        runner.pool,
        "verify",
        {"device_id": "edge-0"},
        idempotency_key="stable-read-operation",
    )
    assert repeated.id == first.id
    assert store.queue_metrics(DEFAULT_ORG_ID)["queued"] == 100
    with pytest.raises(JobQueueFullError, match="100/100"):
        store.create_read_job(
            DEFAULT_ORG_ID,
            runner.pool,
            "verify",
            {"device_id": "edge-overflow"},
        )


def test_queue_metrics_alert_on_oldest_waiting_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETCODE_QUEUE_AGE_ALERT_SECONDS", "30")
    store = _store(tmp_path)
    runner = _runner(store)
    job = store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": "edge-1"})
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    with store._connect() as conn:  # noqa: SLF001 - inject queue age at the durable boundary.
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (old, job.id))

    metrics = store.queue_metrics(DEFAULT_ORG_ID)
    assert metrics["queued"] == 1
    assert metrics["oldest_age_seconds"] >= 89
    assert metrics["age_alert"] is True


def test_admin_drain_survives_heartbeat_and_blocks_new_claims(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    job = store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": "edge-1"})

    drained = store.set_runner_drain(runner.id, DEFAULT_ORG_ID, requested=True)
    assert drained.drain_requested is True
    assert drained.status == "draining"
    heartbeat = store.heartbeat_runner(runner.id, version="test", state="online")
    assert heartbeat.drain_requested is True
    assert heartbeat.status == "draining"
    assert poll_for_job(store, heartbeat, wait_seconds=0) is None
    assert store.get_job(job.id).status == "queued"

    resumed = store.set_runner_drain(runner.id, DEFAULT_ORG_ID, requested=False)
    assert resumed.drain_requested is False
    assert resumed.status == "enrolled"
    claimed = poll_for_job(store, resumed, wait_seconds=0)
    assert claimed is not None and claimed.id == job.id


def test_graceful_runner_state_clears_on_next_online_heartbeat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)

    draining = store.heartbeat_runner(runner.id, state="draining")
    assert draining.status == "draining"
    assert draining.draining_at
    online = store.heartbeat_runner(runner.id, state="online")
    assert online.status == "online"
    assert online.draining_at is None


def test_only_unclaimed_job_can_be_cancelled_and_org_scope_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    queued = store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": "edge-1"})
    cancelled = store.cancel_job_for_org(
        queued.id,
        DEFAULT_ORG_ID,
        actor="marcus",
        reason="maintenance window closed",
    )
    assert cancelled.status == "cancelled"
    assert "marcus" in cancelled.message

    claimed_job = store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": "edge-2"})
    assert store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id) is not None
    with pytest.raises(RuntimeError, match="cannot be cancelled blindly"):
        store.cancel_job_for_org(claimed_job.id, DEFAULT_ORG_ID, actor="marcus", reason="stop")
    with pytest.raises(ValueError, match="Unknown job"):
        store.cancel_job_for_org(claimed_job.id, "org-other", actor="intruder", reason="stop")


def test_read_idempotency_key_cannot_be_rebound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    store.create_read_job(
        DEFAULT_ORG_ID,
        runner.pool,
        "verify",
        {"device_id": "edge-1", "intent": "approved-a"},
        idempotency_key="caller-read-key",
    )

    with pytest.raises(ValueError, match="different read operation"):
        store.create_read_job(
            DEFAULT_ORG_ID,
            runner.pool,
            "verify",
            {"device_id": "edge-2", "intent": "approved-b"},
            idempotency_key="caller-read-key",
        )


def test_shell_termination_is_runner_scoped_and_persists_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _runner(store, "connector-a")
    second = _runner(store, "connector-b")
    for session_id, runner_id in (("shell-a", first.id), ("shell-b", second.id)):
        store.create_shell_session(
            session_id=session_id,
            org_id=DEFAULT_ORG_ID,
            device_id=f"edge-{session_id[-1]}",
            display_id=f"edge-{session_id[-1]}",
            platform="arista_eos",
            runner_id=runner_id,
            runner_pool="pilot",
            transcript_path=str(tmp_path / f"{session_id}.jsonl"),
            status="active",
        )

    terminated = store.terminate_active_shell_sessions(
        runner_id=first.id,
        reason="connector_disconnected",
    )
    assert [item["id"] for item in terminated] == ["shell-a"]
    assert terminated[0]["status"] == "terminated"
    assert terminated[0]["ended_at"]
    assert terminated[0]["end_reason"] == "connector_disconnected"
    assert store.get_shell_session("shell-b")["status"] == "active"
