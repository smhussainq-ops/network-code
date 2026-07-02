"""Synchronous job runner with durable state.

The interface is job-shaped now so it can be moved behind a queue/worker pool
without changing the API/UI contract later.
"""

from __future__ import annotations

from pathlib import Path

from netcode.lab import run_arista_end_to_end, run_lab_action
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore, record_to_dict
from netcode.workflow import require_action_allowed, state_after_lab_action


class JobRunner:
    def __init__(self, paths: WorkspacePaths, store: PlatformStore | None = None):
        self.paths = paths
        self.store = store or PlatformStore(paths)

    def run_full_arista(self, intent_path: Path, device_id: str | None, apply: bool = True) -> dict[str, object]:
        change = self.store.create_change(intent_path, device_id)
        job = self.store.create_job(change.id, "arista_full_run")
        self.store.update_job(job.id, "running", "Running static validation and Arista lab phases")
        try:
            result = run_arista_end_to_end(self.paths, intent_path, device_id, apply=apply)
            result_payload = result.model_dump()
            status = "completed" if result.status == "pass" else "failed"
            self.store.update_change(change.id, status, result_payload)
            final_job = self.store.update_job(job.id, status, f"Full run {result.status}", result_payload)
            return {
                "ok": result.status == "pass",
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": result_payload,
            }
        except Exception as exc:
            error = {"error": f"{type(exc).__name__}: {exc}"}
            self.store.update_change(change.id, "failed", error)
            final_job = self.store.update_job(job.id, "failed", str(exc), error)
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": error,
            }

    def run_lab_action(self, intent_path: Path, action: str, device_id: str | None, change_id: str | None = None) -> dict[str, object]:
        change = self.store.get_change(change_id) if change_id else self.store.get_or_create_change(intent_path, device_id)
        try:
            require_action_allowed(change.workflow_state, action)
        except Exception as exc:
            blocked = {"status": "fail", "message": str(exc), "workflow_state": change.workflow_state}
            self.store.record_workflow_event(
                change.id,
                action,
                change.workflow_state,
                "blocked",
                str(exc),
                {"blocked": True, "action": action},
            )
            self.store.update_change(change.id, "blocked", blocked, workflow_state="blocked")
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": None,
                "result": blocked,
            }

        job = self.store.create_job(change.id, f"lab_{action}")
        self.store.update_job(job.id, "running", f"Running lab {action}")
        try:
            result = run_lab_action(self.paths, intent_path, action, device_id)  # type: ignore[arg-type]
            status = "completed" if result.get("status") == "pass" else "failed"
            workflow = state_after_lab_action(action, result.get("status") == "pass")
            self.store.update_change(change.id, status, result, workflow_state=workflow.state)
            self.store.record_workflow_event(
                change.id,
                action,
                change.workflow_state,
                workflow.state,
                str(result.get("message", "")),
                {"job_id": job.id, "status": status},
            )
            final_job = self.store.update_job(job.id, status, str(result.get("message", "")), result)
            return {
                "ok": result.get("status") == "pass",
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": result,
            }
        except Exception as exc:
            error = {"error": f"{type(exc).__name__}: {exc}"}
            self.store.update_change(change.id, "failed", error, workflow_state="failed")
            self.store.record_workflow_event(change.id, action, change.workflow_state, "failed", str(exc), error)
            final_job = self.store.update_job(job.id, "failed", str(exc), error)
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": error,
            }
