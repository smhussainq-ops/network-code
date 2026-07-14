from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from netcode import runner_agent
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import sign_result, submit_job_result
from netcode.store import DEFAULT_ORG_ID, PlatformStore, job_is_retry_safe, record_to_dict


def _store(tmp_path: Path) -> PlatformStore:
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    return PlatformStore(paths)


def _runner(store: PlatformStore, name: str = "connector-1"):
    return store.create_runner(
        name,
        "pilot",
        hashlib.sha256(name.encode("utf-8")).hexdigest(),
        f"{name}-hmac",
    )


def _change(store: PlatformStore, tmp_path: Path):
    intent = tmp_path / "intents" / "lease-test.yaml"
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text("change_type: custom_config\n", encoding="utf-8")
    return store.create_change(intent, "edge-1")


def _expire(store: PlatformStore, job_id: str) -> None:
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with store._connect() as conn:  # noqa: SLF001 - failure injection at the persistence boundary.
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (expired, job_id))


def test_expired_read_requeues_with_new_claim_and_rejects_stale_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    job = store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": "edge-1"})
    first = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert first is not None and first.id == job.id and first.lease_token
    assert first.attempt_count == 1
    assert "lease_token" not in record_to_dict(first)

    _expire(store, job.id)
    assert store.recover_expired_jobs() == {"requeued": 1, "failed": 0, "reconcile_required": 0}
    second = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert second is not None and second.lease_token and second.lease_token != first.lease_token
    assert second.attempt_count == 2
    result = {"ok": True, "status": "pass", "message": "fresh read"}
    stale = submit_job_result(
        store, runner, job.id, result, sign_result(f"{runner.name}-hmac", result), first.lease_token
    )
    assert stale["ok"] is False and "lease" in stale["message"].lower()
    accepted = submit_job_result(
        store, runner, job.id, result, sign_result(f"{runner.name}-hmac", result), second.lease_token
    )
    assert accepted["ok"] is True
    assert store.get_job(job.id).status == "completed"


def test_expired_write_never_requeues_and_blocks_change_for_reconciliation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    job = store.queue_job(
        change.id,
        "lab_apply",
        runner.pool,
        {"action": "apply", "device": {"id": "edge-1"}},
        target_runner_id=runner.id,
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None and claimed.lease_token
    _expire(store, job.id)

    assert store.recover_expired_jobs() == {"requeued": 0, "failed": 0, "reconcile_required": 1}
    assert store.get_job(job.id).status == "reconcile_required"
    blocked = store.get_change(change.id)
    assert blocked.status == "blocked" and blocked.workflow_state == "blocked"
    assert blocked.result["connector_reconciliation"]["job_id"] == job.id
    assert store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id) is None
    assert store.list_execution_events(change.id)[-1].stage == "reconcile_required"
    assert store.list_workflow_events(change.id)[-1].action == "connector_lease_expired"

    result = {"status": "pass", "message": "late apply result"}
    late = submit_job_result(
        store, runner, job.id, result, sign_result(f"{runner.name}-hmac", result), claimed.lease_token
    )
    assert late["ok"] is False
    assert store.get_change(change.id).workflow_state == "blocked"


def test_expired_ansible_check_fails_closed_because_check_mode_can_be_overridden(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    job = store.queue_job(change.id, "ansible_check", runner.pool, {"action": "ansible_pack"})
    assert store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id) is not None
    _expire(store, job.id)
    store.recover_expired_jobs()
    assert job_is_retry_safe("ansible_check") is False
    assert store.get_job(job.id).status == "reconcile_required"


def test_read_with_scrubbed_one_time_credentials_is_not_replayed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    job = store.create_read_job(
        DEFAULT_ORG_ID,
        runner.pool,
        "discovery",
        {"host": "192.0.2.10", "username": "admin", "password": "one-time"},
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None and claimed.payload["password"] == "one-time"
    assert store.get_job(job.id).payload["password"] == "***redacted***"
    _expire(store, job.id)

    assert store.recover_expired_jobs() == {"requeued": 0, "failed": 1, "reconcile_required": 0}
    assert store.get_job(job.id).status == "failed"
    assert "start a new scan" in store.get_job(job.id).message


def test_lease_renewal_requires_current_runner_and_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    owner = _runner(store, "owner")
    other = _runner(store, "other")
    job = store.create_read_job(DEFAULT_ORG_ID, owner.pool, "verify", {"device_id": "edge-1"})
    claimed = store.claim_next_job(DEFAULT_ORG_ID, owner.pool, owner.id)
    assert claimed is not None and claimed.lease_token
    assert store.renew_job_lease(job.id, other.id, claimed.lease_token) is None
    assert store.renew_job_lease(job.id, owner.id, "jlt_wrong") is None
    assert store.renew_job_lease(job.id, owner.id, claimed.lease_token)
    _expire(store, job.id)
    assert store.renew_job_lease(job.id, owner.id, claimed.lease_token) is None


def test_duplicate_connector_processes_share_one_active_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    for device_id in ("edge-1", "edge-2"):
        store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": device_id})
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = [future.result() for future in (executor.submit(claim), executor.submit(claim))]

    assert sum(item is not None for item in claims) == 1
    assert store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id) is None


def test_duplicate_signed_result_is_acknowledged_without_second_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    job = store.create_read_job(DEFAULT_ORG_ID, runner.pool, "verify", {"device_id": "edge-1"})
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None and claimed.lease_token
    result = {"ok": True, "status": "pass", "message": "verified"}
    signature = sign_result(f"{runner.name}-hmac", result)

    first = submit_job_result(store, runner, job.id, result, signature, claimed.lease_token)
    replay = submit_job_result(store, runner, job.id, result, signature, claimed.lease_token)

    assert first["ok"] is True
    assert replay["ok"] is True and replay["replayed"] is True
    assert store.get_job(job.id).status == "completed"


def test_connector_renews_claim_in_background_during_blocking_work(monkeypatch) -> None:
    calls: list[tuple[str, dict, str | None]] = []

    def fake_post(server, path, body, token=None, timeout=40.0):  # noqa: ANN001, ARG001
        calls.append((path, body, token))
        return {"ok": True}

    monkeypatch.setattr(runner_agent, "_post", fake_post)
    renewer = runner_agent._JobLeaseRenewer(
        "https://control.example",
        "runner-token",
        {"id": "job-1", "lease_token": "jlt_claim", "lease_seconds": 30},
    )
    renewer.interval = 0.01
    renewer.start()
    time.sleep(0.04)
    renewer.stop()

    assert calls
    assert all(path == "/api/runner/jobs/job-1/lease" for path, _body, _token in calls)
    assert all(body == {"lease_token": "jlt_claim"} for _path, body, _token in calls)
    assert all(token == "runner-token" for _path, _body, token in calls)


def test_crash_during_result_completion_reconciles_a_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    job = store.queue_job(change.id, "lab_apply", runner.pool, {"action": "apply"})
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None and claimed.lease_token
    assert store.begin_job_completion(job.id, runner.id, claimed.lease_token) is True
    assert store.get_job(job.id).status == "completing"

    _expire(store, job.id)
    assert store.recover_expired_jobs()["reconcile_required"] == 1
    assert store.get_job(job.id).status == "reconcile_required"
    assert store.get_change(change.id).workflow_state == "blocked"


@pytest.mark.parametrize(
    ("action", "safe"),
    [
        ("read_rez_ssh_command", True),
        ("lab_verify", True),
        ("lab_dry-run", False),
        ("manager_preview", True),
        ("lab_apply", False),
        ("lab_rollback", False),
        ("manager_deploy", False),
        ("unknown_future_action", False),
    ],
)
def test_retry_policy_is_explicit_and_unknown_actions_fail_closed(action: str, safe: bool) -> None:
    assert job_is_retry_safe(action) is safe
