from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from netcode import runner_agent
from netcode.operation_ledger import RunnerOperationLedger


def _job(key: str = "nop_operation_1") -> dict:
    return {
        "id": "job-1",
        "change_id": "change-1",
        "action": "lab_apply",
        "device_id": "edge-1",
        "idempotency_key": key,
        "payload": {
            "action": "apply",
            "change_id": "change-1",
            "device": {"id": "edge-1"},
            "intent_yaml": "change_type: custom_config\n",
        },
    }


def test_ledger_replays_completed_result_without_second_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_agent, "OPERATION_LEDGER_FILE", tmp_path / "operations.db")
    calls = []

    def execute(job, progress=None):  # noqa: ANN001, ARG001
        calls.append(job["id"])
        return {"status": "pass", "action": "apply", "device_id": "edge-1", "message": "applied"}

    monkeypatch.setattr(runner_agent, "_execute_job_inner", execute)
    first = runner_agent._execute_job(_job())
    replay = runner_agent._execute_job(_job())

    assert first == replay
    assert calls == ["job-1"]
    assert RunnerOperationLedger(tmp_path / "operations.db").get("nop_operation_1")["status"] == "completed"


def test_ledger_never_reexecutes_an_interrupted_operation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_agent, "OPERATION_LEDGER_FILE", tmp_path / "operations.db")
    calls = []

    def explode(job, progress=None):  # noqa: ANN001, ARG001
        calls.append(job["id"])
        raise ConnectionError("device outcome unknown")

    monkeypatch.setattr(runner_agent, "_execute_job_inner", explode)
    first = runner_agent._execute_job(_job())
    replay = runner_agent._execute_job(_job())

    assert first["status"] == "reconcile_required"
    assert replay == first
    assert calls == ["job-1"]


def test_operation_key_cannot_be_reused_with_changed_payload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_agent, "OPERATION_LEDGER_FILE", tmp_path / "operations.db")
    monkeypatch.setattr(
        runner_agent,
        "_execute_job_inner",
        lambda job, progress=None: {"status": "pass", "message": "applied"},
    )
    assert runner_agent._execute_job(_job())["status"] == "pass"
    changed = _job()
    changed["payload"] = {**changed["payload"], "intent_yaml": "change_type: custom_config\ncustom: changed\n"}

    rejected = runner_agent._execute_job(changed)
    assert rejected["status"] == "fail"
    assert rejected["error"] == "operation_key_conflict"


def test_missing_operation_key_is_rejected_before_device_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_agent, "OPERATION_LEDGER_FILE", tmp_path / "operations.db")
    calls = []
    monkeypatch.setattr(runner_agent, "_execute_job_inner", lambda job, progress=None: calls.append(job))
    job = _job()
    job.pop("idempotency_key")

    result = runner_agent._execute_job(job)
    assert result["error"] == "missing_operation_key"
    assert calls == []


def test_concurrent_begin_allows_only_one_executor(tmp_path: Path) -> None:
    ledger = RunnerOperationLedger(tmp_path / "operations.db")
    request = {"job_action": "lab_apply", "change_id": "change-1", "device_id": "edge-1", "payload": {}}
    barrier = threading.Barrier(2)

    def begin():
        barrier.wait()
        return ledger.begin(
            "nop_concurrent",
            request,
            action="lab_apply",
            change_id="change-1",
            device_id="edge-1",
        ).mode

    with ThreadPoolExecutor(max_workers=2) as executor:
        modes = [future.result() for future in (executor.submit(begin), executor.submit(begin))]

    assert sorted(modes) == ["execute", "reconcile_required"]


def test_ledger_commit_failure_never_reports_a_successful_write_as_retryable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_agent, "OPERATION_LEDGER_FILE", tmp_path / "operations.db")
    monkeypatch.setattr(
        runner_agent,
        "_execute_job_inner",
        lambda job, progress=None: {"status": "pass", "message": "device accepted commit"},
    )
    monkeypatch.setattr(
        RunnerOperationLedger,
        "complete",
        lambda self, operation_key, result: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = runner_agent._execute_job(_job())

    assert result["status"] == "reconcile_required"
    assert "could not be persisted" in result["message"]
