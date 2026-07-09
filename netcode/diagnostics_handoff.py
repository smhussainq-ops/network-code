"""Structured handoff from Netcode verification failures to Rez Diagnostics."""

from __future__ import annotations

from typing import Any


PASS_STATUSES = {"pass", "passed", "ok", "success", "true"}


def verification_failed(verification: dict[str, Any] | None) -> bool:
    """Return True only when verification evidence explicitly failed."""
    if not isinstance(verification, dict):
        return False
    if verification.get("failed") is True:
        return True
    if "ok" in verification:
        return not bool(verification.get("ok"))
    status = str(verification.get("status") or "").strip().lower()
    return bool(status) and status not in PASS_STATUSES


def build_verification_handoff(
    *,
    device_id: str,
    check: str,
    expected: str = "",
    actual: str = "",
    verification: dict[str, Any] | None = None,
    change_id: str = "",
    intent_path: str = "",
) -> dict[str, Any]:
    """Create a deterministic, read-only Rez Diagnostics handoff.

    This is a context builder only. It does not call a device, run Rez, create a
    remediation change, or bypass Netcode approval gates.
    """
    verification = verification if isinstance(verification, dict) else {}
    status = str(verification.get("status") or ("pass" if verification.get("ok") else "fail")).lower()
    failed = status not in PASS_STATUSES
    device = str(device_id or "").strip()
    normalized_check = str(check or "verification").strip() or "verification"
    expected_value = str(expected or verification.get("expected") or "")
    actual_value = str(actual or verification.get("actual") or verification.get("message") or "")

    question = (
        f"Netcode verification failed on {device} for check {normalized_check}. "
        "Use read-only live evidence through the runner to explain why expected state "
        "does not match actual state, and recommend the next safe Netcode remediation "
        "or rollback plan. Do not apply configuration."
    )
    if expected_value:
        question += f" Expected: {expected_value}."
    if actual_value:
        question += f" Actual: {actual_value}."

    context = {
        "source": "netcode_verification",
        "device_id": device,
        "check": normalized_check,
        "expected": expected_value,
        "actual": actual_value,
        "verification": verification,
        "change_id": str(change_id or ""),
        "intent_path": str(intent_path or ""),
        "failed": failed,
        "read_only": True,
    }
    return {
        "ok": True,
        "handoff_type": "verification_failure_to_rez",
        "question": question,
        "context": context,
        "remediation_plan": {
            "status": "not_created",
            "next_step": "Use the Rez finding to generate a Netcode remediation or rollback plan through normal gates.",
            "direct_write_allowed": False,
        },
        "safety": {
            "device_writes": "none",
            "rez_mode": "read_only_diagnostics",
            "netcode_remediation": "plan_only_until_approved",
        },
    }


def attach_verification_handoff(
    store: Any,
    *,
    change_id: str | None,
    device_id: str,
    check: str,
    verification: dict[str, Any] | None,
    expected: str = "",
    actual: str = "",
    intent_path: str = "",
) -> dict[str, Any] | None:
    """Attach a read-only Rez handoff to a failed change verification.

    This mutates only the Netcode change record and workflow event log. It does
    not call Rez, create a remediation change, enqueue a job, or perform device
    writes.
    """
    if not change_id or not verification_failed(verification):
        return None
    handoff = build_verification_handoff(
        device_id=device_id,
        check=check,
        expected=expected,
        actual=actual,
        verification=verification,
        change_id=change_id,
        intent_path=intent_path,
    )
    try:
        change = store.get_change(change_id)
    except Exception:
        return None
    result = dict(change.result or {})
    handoffs = list(result.get("diagnostics_handoffs") or [])
    handoffs.append(handoff)
    result["diagnostics_handoffs"] = handoffs
    store.update_change(change.id, change.status, result, workflow_state=change.workflow_state)
    store.record_workflow_event(
        change.id,
        "diagnostics_handoff",
        change.workflow_state,
        change.workflow_state,
        f"Attached read-only Rez Diagnostics handoff for failed {check} verification.",
        handoff,
    )
    return handoff
