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
import secrets
import time
from typing import Any

from netcode.store import JobRecord, PlatformStore, RunnerRecord, record_to_dict
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


def submit_job_result(
    store: PlatformStore,
    runner: RunnerRecord,
    job_id: str,
    result: dict[str, Any],
    signature: str,
) -> dict[str, Any]:
    """Verify signature and ownership, then complete the job and advance the change workflow."""
    try:
        job = store.get_job(job_id)
    except Exception:
        return {"ok": False, "message": f"Unknown job {job_id}."}
    if job.claimed_by != runner.id:
        return {"ok": False, "message": "This job is not claimed by this runner."}
    if job.status not in ("running",):
        return {"ok": False, "message": f"Job is {job.status}; results are only accepted for running jobs."}

    secret = store.runner_hmac_secret(runner.id)
    expected = sign_result(secret, result)
    if not hmac.compare_digest(expected, signature or ""):
        # Reject without changing job state: the runner holds both token and secret,
        # so a mismatch means corruption/bug — leave the job claimable for a retry
        # rather than bricking it.
        return {"ok": False, "message": "Result signature verification failed; result rejected."}

    action = job.action.removeprefix("lab_")
    passed = result.get("status") == "pass"
    status = "completed" if passed else "failed"
    change = store.get_change(job.change_id)
    workflow = state_after_lab_action(action, passed)
    store.update_change(change.id, status, result, workflow_state=workflow.state)
    store.record_workflow_event(
        change.id,
        action,
        change.workflow_state,
        workflow.state,
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
        "workflow_state": workflow.state,
        "message": f"Result accepted; change moved to {workflow.state}.",
    }


def runner_summary(store: PlatformStore, org_id: str | None = None) -> dict[str, Any]:
    runners = [record_to_dict(runner) for runner in store.list_runners(org_id=org_id)]
    return {"ok": True, "runners": runners, "count": len(runners)}
