"""Structured handoff from Netcode verification failures to Rez Diagnostics."""

from __future__ import annotations

from typing import Any


PASS_STATUSES = {"pass", "passed", "ok", "success", "true"}


def change_environment_binding(change: Any) -> str:
    """Return one unambiguous environment persisted on a change record."""
    result = change.result if isinstance(getattr(change, "result", None), dict) else {}
    candidates: set[str] = set()
    for path in (
        ("environment_id",),
        ("network_model", "environment_id"),
        ("pipeline", "network_model", "environment_id"),
        ("plan", "environment_id"),
        ("service_assurance", "environment_id"),
    ):
        value: Any = result
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        normalized = str(value or "").strip()
        if normalized:
            candidates.add(normalized)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _labeled_sentence(label: str, value: str) -> str:
    text = value.strip()
    return f" {label}: {text}{'' if text.endswith(('.', '!', '?')) else '.'}"


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
    org_id: str = "",
    environment_id: str = "",
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
        question += _labeled_sentence("Expected", expected_value)
    if actual_value:
        question += _labeled_sentence("Actual", actual_value)

    context = {
        "source": "netcode_verification",
        "device_id": device,
        "check": normalized_check,
        "expected": expected_value,
        "actual": actual_value,
        "verification": verification,
        "change_id": str(change_id or ""),
        "intent_path": str(intent_path or ""),
        "org_id": str(org_id or "").strip(),
        "environment_id": str(environment_id or "").strip(),
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
    try:
        change = store.get_change(change_id)
    except Exception:
        return None
    handoff = build_verification_handoff(
        device_id=device_id,
        check=check,
        expected=expected,
        actual=actual,
        verification=verification,
        change_id=change_id,
        intent_path=intent_path,
        org_id=str(change.org_id or ""),
        environment_id=change_environment_binding(change),
    )
    from netcode.diagnostics_dispatch import dispatch_verification_handoff

    handoff["dispatch"] = dispatch_verification_handoff(handoff)
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
