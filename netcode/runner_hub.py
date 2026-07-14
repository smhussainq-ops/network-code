"""Control-plane side of the SaaS/runner split.

The runner is a pure outbound client: it enrolls with a single-use join token,
long-polls for queued jobs, executes them next to the devices, and uploads
HMAC-signed results. The control plane never dials the runner and never sees
device credentials.

Phase 0 signing note: results are HMAC-SHA256 signed with a per-runner secret
issued at enrollment. Phase 1 upgrades this to runner-held asymmetric keys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from typing import Any

from netcode.entitlements import EntitlementError, enforce_capacity
from netcode.network_model import NetworkModelError
from netcode.network_model_lifecycle import rollback_change_candidates
from netcode.network_model_store import NetworkModelRepository
from netcode.store import (
    JobRecord,
    PlatformStore,
    RunnerRecord,
    TERMINAL_JOB_STATUSES,
    execution_phase_for_job,
    record_to_dict,
)
from netcode.workflow import state_after_lab_action


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sign_result(secret: str, result: dict[str, Any]) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_json(result).encode("utf-8"), hashlib.sha256).hexdigest()


def mint_join_token(store: PlatformStore, pool: str, org_id: str = "org_default") -> dict[str, Any]:
    pool = pool.strip() or "default"
    token = f"njt_{secrets.token_urlsafe(32)}"
    store.create_join_token(_hash(token), pool, org_id=org_id)
    return {
        "ok": True,
        "join_token": token,
        "pool": pool,
        "message": f"Single-use join token for pool '{pool}'. It is shown once — copy it now.",
    }


def enroll_runner(store: PlatformStore, join_token: str, name: str) -> dict[str, Any]:
    name = name.strip() or "runner"
    claim = store.consume_join_token(_hash(join_token.strip()))
    if claim is None:
        return {"ok": False, "message": "Join token is invalid or already used. Mint a new one."}
    pool, org_id = claim["pool"], claim["org_id"]
    try:
        enforce_capacity(
            "connectors",
            current=len(store.list_runners(org_id=org_id)),
            additional=1,
            org_id=org_id,
        )
    except EntitlementError as exc:
        return {"ok": False, "message": str(exc), "error": "connector_limit_reached"}
    runner_token = f"nrt_{secrets.token_urlsafe(32)}"
    hmac_secret = secrets.token_urlsafe(32)
    # The runner's tenant is decided exactly once, here, from the join token's org.
    runner = store.create_runner(name=name, pool=pool, token_hash=_hash(runner_token), hmac_secret=hmac_secret, org_id=org_id)
    return {
        "ok": True,
        "runner_id": runner.id,
        "runner_token": runner_token,
        "hmac_secret": hmac_secret,
        "pool": pool,
        "message": f"Runner '{name}' enrolled into pool '{pool}'.",
    }


def authenticate_runner(store: PlatformStore, bearer_token: str) -> RunnerRecord | None:
    token = bearer_token.strip()
    if not token:
        return None
    return store.runner_by_token_hash(_hash(token))


def poll_for_job(store: PlatformStore, runner: RunnerRecord, wait_seconds: float = 20.0) -> JobRecord | None:
    """Long-poll: claim the next queued job for the runner's pool, holding up to wait_seconds."""
    wait_seconds = max(0.0, min(float(wait_seconds), 25.0))
    deadline = time.monotonic() + wait_seconds
    store.touch_runner(runner.id, status="online")
    while True:
        job = store.claim_next_job(runner.org_id, runner.pool, runner.id)
        if job is not None:
            return job
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


_PROGRESS_STATUSES = {"queued", "running", "passed", "failed"}
_SENSITIVE_COMMAND = re.compile(
    r"\b(?:password|passwd|secret|community|token|api[-_ ]?key|private[-_ ]?key|"
    r"pre-shared-key|key-string)\b",
    re.IGNORECASE,
)


def _safe_progress_command(value: object) -> str | None:
    command = str(value or "").strip()
    if not command:
        return None
    command = command[:2000]
    if _SENSITIVE_COMMAND.search(command):
        return f"{command.split(maxsplit=1)[0]} <redacted>"
    return command


def _safe_progress_message(value: object) -> str:
    message = str(value or "").strip()[:500]
    if _SENSITIVE_COMMAND.search(message):
        return "Progress detail redacted because it may contain sensitive data."
    return message


def submit_job_progress(
    store: PlatformStore,
    runner: RunnerRecord,
    job_id: str,
    event: dict[str, Any],
    signature: str,
    lease_token: str,
) -> dict[str, Any]:
    """Accept signed display-only progress from the runner.

    Progress can never advance workflow state. It is an append-only audit feed
    scoped to the job already claimed by this runner.
    """
    try:
        job = store.get_job(job_id)
    except Exception:
        return {"ok": False, "message": f"Unknown job {job_id}."}
    lease_expires_at = store.renew_job_lease(job.id, runner.id, lease_token)
    if not lease_expires_at:
        return {"ok": False, "message": "Progress rejected because the connector job lease is missing, stale, or expired."}
    expected = sign_result(store.runner_hmac_secret(runner.id), event)
    if not hmac.compare_digest(expected, signature or ""):
        return {"ok": False, "message": "Progress signature verification failed."}
    phase = str(event.get("phase") or "").strip().lower()
    expected_phase = execution_phase_for_job(job.action)
    if not expected_phase or phase != expected_phase:
        return {"ok": False, "message": f"Progress phase {phase!r} does not match job {job.action!r}."}
    stage = str(event.get("stage") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", stage):
        return {"ok": False, "message": "Progress stage is invalid."}
    status = str(event.get("status") or "running").strip().lower()
    if status not in _PROGRESS_STATUSES:
        return {"ok": False, "message": "Progress status is invalid."}
    try:
        sequence = int(event.get("sequence"))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Progress sequence is required."}
    if not 2 <= sequence <= 100_000:
        return {"ok": False, "message": "Progress sequence is outside the accepted range."}
    try:
        event_id = str(uuid.UUID(str(event.get("event_id") or "")))
    except (TypeError, ValueError, AttributeError):
        return {"ok": False, "message": "Progress event_id must be a UUID."}
    payload = job.payload or {}
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    expected_device = str(payload.get("device_id") or device.get("id") or "").strip()
    device_id = str(event.get("device_id") or expected_device).strip()
    if expected_device and device_id.lower() != expected_device.lower():
        return {"ok": False, "message": "Progress device does not match the claimed job."}
    current = event.get("current_step")
    total = event.get("total_steps")
    try:
        current_step = int(current) if current is not None else None
        total_steps = int(total) if total is not None else None
    except (TypeError, ValueError):
        return {"ok": False, "message": "Progress counters must be integers."}
    if current_step is not None and not 0 <= current_step <= 100_000:
        return {"ok": False, "message": "Progress current_step is outside the accepted range."}
    if total_steps is not None and not 1 <= total_steps <= 100_000:
        return {"ok": False, "message": "Progress total_steps is outside the accepted range."}
    if current_step is not None and total_steps is not None and current_step > total_steps:
        return {"ok": False, "message": "Progress current_step cannot exceed total_steps."}
    change = store.get_change(job.change_id)
    if change.org_id != runner.org_id:
        return {"ok": False, "message": "Progress tenant does not match the job."}
    saved = store.record_execution_event(
        event_id=event_id,
        job_id=job.id,
        change_id=job.change_id,
        org_id=job.org_id,
        device_id=device_id,
        phase=phase,
        stage=stage,
        status=status,
        message=_safe_progress_message(event.get("message")),
        sequence=sequence,
        current_step=current_step,
        total_steps=total_steps,
        command=_safe_progress_command(event.get("command")),
    )
    store.touch_runner(runner.id, status="online")
    return {"ok": True, "event": record_to_dict(saved), "lease_expires_at": lease_expires_at}


def submit_job_result(
    store: PlatformStore,
    runner: RunnerRecord,
    job_id: str,
    result: dict[str, Any],
    signature: str,
    lease_token: str,
) -> dict[str, Any]:
    """Verify signature and ownership, then complete the job and advance the change workflow."""
    try:
        job = store.get_job(job_id)
    except Exception:
        return {"ok": False, "message": f"Unknown job {job_id}."}

    secret = store.runner_hmac_secret(runner.id)
    expected = sign_result(secret, result)
    if (
        job.status in TERMINAL_JOB_STATUSES
        and job.claimed_by == runner.id
        and job.signature
        and hmac.compare_digest(job.signature, signature or "")
        and canonical_json(job.result or {}) == canonical_json(result)
    ):
        response: dict[str, Any] = {
            "ok": True,
            "job": record_to_dict(job),
            "replayed": True,
            "message": "Previously accepted result acknowledged without replaying workflow state.",
        }
        if job.change_id != "__read__":
            change = store.get_change(job.change_id)
            response["change"] = record_to_dict(change)
            response["workflow_state"] = change.workflow_state
        return response
    if not store.job_lease_matches(job.id, runner.id, lease_token):
        return {"ok": False, "message": "Result rejected because the connector job lease is missing, stale, or expired."}
    if not hmac.compare_digest(expected, signature or ""):
        # Reject without changing job state: the runner holds both token and secret,
        # so a mismatch means corruption/bug — leave the job claimable for a retry
        # rather than bricking it.
        return {"ok": False, "message": "Result signature verification failed; result rejected."}
    if not store.begin_job_completion(job.id, runner.id, lease_token):
        return {"ok": False, "message": "Result rejected because another request already owns job completion."}

    phase = execution_phase_for_job(job.action)
    if job.change_id != "__read__" and phase:
        result_status = str(result.get("status") or "").strip().lower()
        passed = result.get("ok", result_status == "pass") is True
        payload = job.payload or {}
        device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
        terminal = "reconcile_required" if result_status == "reconcile_required" else "passed" if passed else "failed"
        terminal_status = "failed" if terminal == "reconcile_required" else terminal
        last_event = store.last_execution_event(job.id)
        if last_event is None or last_event.stage != terminal or last_event.status != terminal_status:
            store.record_execution_event(
                event_id=str(uuid.uuid4()),
                job_id=job.id,
                change_id=job.change_id,
                org_id=job.org_id,
                device_id=str(payload.get("device_id") or device.get("id") or ""),
                phase=phase,
                stage=terminal,
                status=terminal_status,
                message=_safe_progress_message(
                    result.get("message") or ("Execution passed." if passed else "Execution failed.")
                ),
                sequence=store.next_execution_sequence(job.id),
            )

    # Device-READ jobs (reachability/verify/drift/discovery) aren't change-scoped:
    # store the result and return; no workflow to advance.
    if job.action.startswith("read_"):
        read_ok = result.get("ok", result.get("status") == "pass")
        final_job = store.update_job(job.id, "completed" if read_ok else "failed", str(result.get("message", "read complete")), result)
        store.record_job_signature(job.id, signature)
        reconciliation_for = str((job.payload or {}).get("reconciliation_for_job_id") or "").strip()
        if reconciliation_for and job.change_id != "__read__":
            change = store.get_change(job.change_id)
            combined_result = dict(change.result or {})
            reconciliation = dict(combined_result.get("connector_reconciliation") or {})
            reconciliation.update({
                "verification_job_id": job.id,
                "verification_status": "completed" if read_ok else "failed",
                "verification_result": result,
                "reconciliation_for_job_id": reconciliation_for,
                "operator_review_required": True,
            })
            combined_result["connector_reconciliation"] = reconciliation
            store.update_change(change.id, "blocked", combined_result, workflow_state="blocked")
            store.record_workflow_event(
                change.id,
                "connector_reconciliation_completed",
                change.workflow_state,
                "blocked",
                "Read-only live-state reconciliation completed; operator review is still required.",
                reconciliation,
            )
        # The runner has used any discovery credentials in the payload; purge them
        # from the DB now so they never sit at rest in the control plane.
        store.scrub_job_payload_secrets(job.id)
        store.touch_runner(runner.id, status="online")
        return {"ok": True, "job": record_to_dict(final_job), "message": "Read result accepted."}

    if str(result.get("status") or "").strip().lower() == "reconcile_required":
        change = store.get_change(job.change_id)
        reconciliation_job = store.queue_reconciliation_read(job)
        evidence = {
            "job_id": job.id,
            "runner_id": runner.id,
            "action": job.action,
            "attempt_count": job.attempt_count,
            "reason": "runner_operation_outcome_uncertain",
            "result": result,
        }
        if reconciliation_job is not None:
            evidence["verification_job_id"] = reconciliation_job.id
            evidence["verification_status"] = reconciliation_job.status
        else:
            evidence["verification_status"] = "not_available_for_action"
        combined_result = dict(change.result or {})
        combined_result["connector_reconciliation"] = evidence
        store.update_change(change.id, "blocked", combined_result, workflow_state="blocked")
        store.record_workflow_event(
            change.id,
            "connector_reconciliation_required",
            change.workflow_state,
            "blocked",
            str(result.get("message") or "Live device state must be reconciled before retry."),
            evidence,
        )
        final_job = store.update_job(
            job.id,
            "reconcile_required",
            str(result.get("message") or "Live device state must be reconciled before retry."),
            result,
        )
        store.record_job_signature(job.id, signature)
        store.touch_runner(runner.id, status="online")
        return {
            "ok": True,
            "job": record_to_dict(final_job),
            "change": record_to_dict(store.get_change(change.id)),
            "workflow_state": "blocked",
            "message": "Uncertain operation recorded; change blocked pending read-only reconciliation.",
        }

    if job.action.startswith("manager_"):
        action = job.action.removeprefix("manager_")
        passed = result.get("status") == "pass"
        status = "completed" if passed else "failed"
        change = store.get_change(job.change_id)
        if not passed:
            next_state = "failed"
        elif action in {"preview", "validate"}:
            next_state = "dry_run_passed"
        elif action == "deploy":
            next_state = "rollback_available"
        elif action == "rollback":
            next_state = "rolled_back"
        else:
            next_state = change.workflow_state
        combined_result = dict(change.result or {})
        manager_results = list(combined_result.get("manager_results") or [])
        manager_results.append({"action": action, "job_id": job.id, "result": result})
        combined_result["manager_results"] = manager_results
        store.update_change(change.id, status, combined_result, workflow_state=next_state)
        store.record_workflow_event(
            change.id,
            f"manager_{action}",
            change.workflow_state,
            next_state,
            str(result.get("message", "")),
            {"job_id": job.id, "status": status, "runner_id": runner.id, "signature_valid": True},
        )
        final_job = store.update_job(job.id, status, str(result.get("message", "")), result)
        store.record_job_signature(job.id, signature)
        store.touch_runner(runner.id, status="online")
        return {
            "ok": True,
            "job": record_to_dict(final_job),
            "change": record_to_dict(store.get_change(change.id)),
            "workflow_state": next_state,
            "message": f"Manager result accepted; change moved to {next_state}.",
        }

    if job.action.startswith("ansible_"):
        ansible_mode = job.action.removeprefix("ansible_")
        transition_action = {
            "check": "dry-run",
            "canary": "apply",
            "apply": "apply",
            "rollback": "rollback",
        }.get(ansible_mode, "")
        event_action = job.action
    else:
        event_action = job.action.removeprefix("lab_")
        transition_action = event_action
    passed = result.get("status") == "pass"
    status = "completed" if passed else "failed"
    change = store.get_change(job.change_id)
    workflow = state_after_lab_action(transition_action, passed)
    store.update_change(change.id, status, result, workflow_state=workflow.state)
    store.record_workflow_event(
        change.id,
        event_action,
        change.workflow_state,
        workflow.state,
        str(result.get("message", "")),
        {"job_id": job.id, "status": status, "runner_id": runner.id, "signature_valid": True},
    )
    model_error = ""
    if transition_action == "rollback" and passed:
        try:
            model_rollbacks = rollback_change_candidates(
                NetworkModelRepository(store),
                store,
                org_id=change.org_id,
                change_id=change.id,
                actor=f"local-connector:{runner.id}",
                git_root=store.paths.git_workspace,
            )
            result = dict(result)
            result["network_model_rollback"] = {
                "ok": True,
                "revisions": [item["revision"]["revision_id"] for item in model_rollbacks],
            }
            store.update_change(change.id, status, result, workflow_state=workflow.state)
            store.record_workflow_event(
                change.id,
                "network_model_rollback",
                workflow.state,
                workflow.state,
                "Verified device rollback restored the linked Network Model parent.",
                result["network_model_rollback"],
            )
        except (KeyError, NetworkModelError, ValueError) as exc:
            model_error = str(exc)
            result = dict(result)
            result["network_model_rollback"] = {"ok": False, "error": model_error}
            store.update_change(change.id, "blocked", result, workflow_state=workflow.state)
            store.record_workflow_event(
                change.id,
                "network_model_rollback",
                workflow.state,
                workflow.state,
                "Device rollback passed, but the Network Model checkpoint failed.",
                result["network_model_rollback"],
            )
    final_job = store.update_job(job.id, status, str(result.get("message", "")), result)
    store.record_job_signature(job.id, signature)
    store.touch_runner(runner.id, status="online")
    return {
        "ok": True,
        "job": record_to_dict(final_job),
        "change": record_to_dict(store.get_change(change.id)),
        "workflow_state": workflow.state,
        "message": (
            f"Result accepted; change moved to {workflow.state}."
            if not model_error
            else "Device rollback was accepted, but Network Model reconciliation is blocked."
        ),
    }


def runner_summary(store: PlatformStore, org_id: str | None = None) -> dict[str, Any]:
    runners = [record_to_dict(runner) for runner in store.list_runners(org_id=org_id)]
    return {"ok": True, "runners": runners, "count": len(runners)}
