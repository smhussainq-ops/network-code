"""Structured handoff from Netcode verification failures to Rez Diagnostics."""

from __future__ import annotations

from typing import Any


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
    failed = status not in {"pass", "passed", "ok", "success", "true"}
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
