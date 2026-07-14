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


def test_expired_apply_queues_one_read_only_reconciliation_and_keeps_human_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    job = store.queue_job(
        change.id,
        "lab_apply",
        runner.pool,
        {
            "action": "apply",
            "device": {"id": "edge-1"},
            "intent_yaml": "change_type: custom_config\ntargets:\n  device_ids: [edge-1]\ncustom:\n  config: test\n",
        },
        target_runner_id=runner.id,
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None and claimed.lease_token
    _expire(store, job.id)

    assert store.recover_expired_jobs()["reconcile_required"] == 1
    blocked = store.get_change(change.id)
    reconciliation_job_id = blocked.result["connector_reconciliation"]["verification_job_id"]
    reconciliation = store.get_job(reconciliation_job_id)
    assert reconciliation.action == "read_verify"
    assert reconciliation.payload["present"] is True
    assert reconciliation.payload["reconciliation_for_job_id"] == job.id
    assert store.queue_reconciliation_read(store.get_job(job.id)).id == reconciliation.id

    read_claim = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert read_claim is not None and read_claim.id == reconciliation.id and read_claim.lease_token
    result = {"ok": True, "status": "pass", "message": "desired state is present"}
    accepted = submit_job_result(
        store,
        runner,
        reconciliation.id,
        result,
        sign_result(f"{runner.name}-hmac", result),
        read_claim.lease_token,
    )

    assert accepted["ok"] is True
    reviewed = store.get_change(change.id)
    assert reviewed.workflow_state == "blocked"
    proof = reviewed.result["connector_reconciliation"]
    assert proof["verification_result"]["status"] == "pass"
    assert proof["operator_review_required"] is True
    assert store.list_workflow_events(change.id)[-1].action == "connector_reconciliation_completed"


def test_uncertain_rollback_reconciliation_checks_previous_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    job = store.queue_job(
        change.id,
        "lab_rollback",
        runner.pool,
        {
            "action": "rollback",
            "device": {"id": "edge-1"},
            "intent_yaml": "change_type: custom_config\ntargets:\n  device_ids: [edge-1]\ncustom:\n  config: test\n",
        },
    )
    assert store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id) is not None
    _expire(store, job.id)
    store.recover_expired_jobs()

    reconciliation = store.get_job(
        store.get_change(change.id).result["connector_reconciliation"]["verification_job_id"]
    )
    assert reconciliation.payload["present"] is False


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


def test_duplicate_operation_requests_create_one_durable_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    payload = {
        "action": "apply",
        "device": {"id": "EDGE-1", "host": "192.0.2.10", "platform": "arista_eos"},
        "intent_yaml": "change_type: custom_config\n",
        "rendered_config": "interface Ethernet1\n description pilot\n",
    }
    barrier = threading.Barrier(2)

    def queue():
        barrier.wait()
        return store.queue_job(
            change.id,
            "lab_apply",
            runner.pool,
            payload,
            target_runner_id=runner.id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [future.result() for future in (executor.submit(queue), executor.submit(queue))]

    assert jobs[0].id == jobs[1].id
    assert jobs[0].device_id == "edge-1"
    assert jobs[0].idempotency_key and jobs[0].idempotency_key.startswith("nop_")
    with store._connect() as conn:  # noqa: SLF001 - assert the durable uniqueness boundary.
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE change_id = ? AND action = 'lab_apply'",
            (change.id,),
        ).fetchone()["count"]
    assert count == 1


def test_safe_action_can_retry_after_terminal_failure_without_losing_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    payload = {
        "action": "dry-run",
        "device": {"id": "edge-1", "host": "192.0.2.10", "platform": "arista_eos"},
    }

    first = store.queue_job(
        change.id,
        "lab_dry-run",
        runner.pool,
        payload,
        target_runner_id=runner.id,
        retry_terminal=True,
    )
    duplicate = store.queue_job(
        change.id,
        "lab_dry-run",
        runner.pool,
        payload,
        target_runner_id=runner.id,
        retry_terminal=True,
    )
    assert duplicate.id == first.id

    store.update_job(first.id, "reconcile_required", "outcome uncertain", {"status": "reconcile_required"})
    retry = store.queue_job(
        change.id,
        "lab_dry-run",
        runner.pool,
        payload,
        target_runner_id=runner.id,
        retry_terminal=True,
    )

    assert retry.id != first.id
    assert retry.idempotency_key == f"{first.idempotency_key}:retry:1"
    assert store.get_job(first.id).status == "reconcile_required"


def test_write_action_does_not_retry_after_terminal_uncertainty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    payload = {
        "action": "apply",
        "device": {"id": "edge-1", "host": "192.0.2.10", "platform": "arista_eos"},
    }

    first = store.queue_job(
        change.id,
        "lab_apply",
        runner.pool,
        payload,
        target_runner_id=runner.id,
    )
    store.update_job(first.id, "reconcile_required", "outcome uncertain", {"status": "reconcile_required"})
    replay = store.queue_job(
        change.id,
        "lab_apply",
        runner.pool,
        payload,
        target_runner_id=runner.id,
    )

    assert replay.id == first.id


def test_concurrent_safe_retries_create_one_new_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    payload = {
        "action": "verify",
        "device": {"id": "edge-1", "host": "192.0.2.10", "platform": "arista_eos"},
    }
    first = store.queue_job(
        change.id,
        "lab_verify",
        runner.pool,
        payload,
        target_runner_id=runner.id,
        retry_terminal=True,
    )
    store.update_job(first.id, "failed", "read failed", {"status": "fail"})
    barrier = threading.Barrier(2)

    def retry():
        barrier.wait()
        return store.queue_job(
            change.id,
            "lab_verify",
            runner.pool,
            payload,
            target_runner_id=runner.id,
            retry_terminal=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        retries = [future.result() for future in (executor.submit(retry), executor.submit(retry))]

    assert retries[0].id == retries[1].id
    assert retries[0].id != first.id


def test_idempotency_key_cannot_be_rebound_to_another_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    first_change = _change(store, tmp_path)
    second_intent = tmp_path / "intents" / "lease-test-2.yaml"
    second_intent.write_text("change_type: custom_config\n", encoding="utf-8")
    second_change = store.create_change(second_intent, "edge-2")
    shared_key = "caller-supplied-operation-key"
    store.queue_job(
        first_change.id,
        "lab_apply",
        runner.pool,
        {"action": "apply", "device": {"id": "edge-1"}},
        idempotency_key=shared_key,
    )

    with pytest.raises(ValueError, match="different device operation"):
        store.queue_job(
            second_change.id,
            "lab_apply",
            runner.pool,
            {"action": "apply", "device": {"id": "edge-2"}},
            idempotency_key=shared_key,
        )
    assert second_change.last_job_id is None


def test_same_device_is_serialized_across_distinct_connectors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_runner = _runner(store, "connector-a")
    second_runner = _runner(store, "connector-b")
    first_change = _change(store, tmp_path)
    second_intent = tmp_path / "intents" / "lease-test-2.yaml"
    second_intent.write_text("change_type: custom_config\n", encoding="utf-8")
    second_change = store.create_change(second_intent, "edge-1")
    payload = {"action": "apply", "device": {"id": "EDGE-1"}}
    first_job = store.queue_job(
        first_change.id,
        "lab_apply",
        first_runner.pool,
        payload,
        target_runner_id=first_runner.id,
    )
    second_job = store.queue_job(
        second_change.id,
        "lab_apply",
        second_runner.pool,
        payload,
        target_runner_id=second_runner.id,
    )

    first_claim = store.claim_next_job(DEFAULT_ORG_ID, first_runner.pool, first_runner.id)
    assert first_claim is not None and first_claim.id == first_job.id
    assert store.claim_next_job(DEFAULT_ORG_ID, second_runner.pool, second_runner.id) is None

    store.update_job(first_job.id, "completed", "first operation completed", {"status": "pass"})
    second_claim = store.claim_next_job(DEFAULT_ORG_ID, second_runner.pool, second_runner.id)
    assert second_claim is not None and second_claim.id == second_job.id


def test_distinct_devices_can_run_on_distinct_connectors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_runner = _runner(store, "connector-a")
    second_runner = _runner(store, "connector-b")
    first_change = _change(store, tmp_path)
    second_intent = tmp_path / "intents" / "lease-test-2.yaml"
    second_intent.write_text("change_type: custom_config\n", encoding="utf-8")
    second_change = store.create_change(second_intent, "edge-2")
    store.queue_job(
        first_change.id,
        "lab_apply",
        first_runner.pool,
        {"action": "apply", "device": {"id": "edge-1"}},
        target_runner_id=first_runner.id,
    )
    store.queue_job(
        second_change.id,
        "lab_apply",
        second_runner.pool,
        {"action": "apply", "device": {"id": "edge-2"}},
        target_runner_id=second_runner.id,
    )

    assert store.claim_next_job(DEFAULT_ORG_ID, first_runner.pool, first_runner.id) is not None
    assert store.claim_next_job(DEFAULT_ORG_ID, second_runner.pool, second_runner.id) is not None


def test_read_jobs_do_not_require_a_fake_change_foreign_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    job = store.create_read_job(
        DEFAULT_ORG_ID,
        runner.pool,
        "verify",
        {"device_id": "edge-1"},
    )
    assert job.change_id == "__read__"
    with store._connect() as conn:  # noqa: SLF001 - schema contract for Postgres parity.
        foreign_keys = conn.execute("PRAGMA foreign_key_list(jobs)").fetchall()
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(jobs)").fetchall()}
    assert not foreign_keys
    assert "idx_jobs_one_active_device" in indexes
    assert "idx_jobs_org_idempotency" in indexes


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


def test_runner_reported_uncertain_outcome_blocks_without_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = _runner(store)
    change = _change(store, tmp_path)
    job = store.queue_job(
        change.id,
        "lab_apply",
        runner.pool,
        {"action": "apply", "device": {"id": "edge-1"}},
    )
    claimed = store.claim_next_job(DEFAULT_ORG_ID, runner.pool, runner.id)
    assert claimed is not None and claimed.lease_token
    result = {
        "status": "reconcile_required",
        "action": "apply",
        "device_id": "edge-1",
        "message": "connection ended after commit started",
        "operation_key": job.idempotency_key,
    }

    accepted = submit_job_result(
        store,
        runner,
        job.id,
        result,
        sign_result(f"{runner.name}-hmac", result),
        claimed.lease_token,
    )

    assert accepted["ok"] is True and accepted["workflow_state"] == "blocked"
    assert store.get_job(job.id).status == "reconcile_required"
    assert store.get_change(change.id).workflow_state == "blocked"
    assert store.list_execution_events(change.id)[-1].stage == "reconcile_required"
    assert store.list_workflow_events(change.id)[-1].action == "connector_reconciliation_required"


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
