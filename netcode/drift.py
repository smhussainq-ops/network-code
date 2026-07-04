"""Drift and compliance helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netcode.models import load_intent
from netcode.paths import WorkspacePaths
from netcode.verification import verify_vlan_state


def baseline_for_state(workflow_state: str | None) -> dict[str, Any]:
    """What the live device SHOULD look like, given where the change is in its lifecycle.

    Drift is only meaningful against a committed/applied baseline. A change that was
    rolled back should be ABSENT (its absence is correct, not drift); a change that was
    applied should be PRESENT; a change never applied is a preview, not a baseline.
    """
    applied = {"rollback_available", "completed", "verified", "applying"}
    if workflow_state == "rolled_back":
        return {"expected_present": False, "context": "rolled_back", "label": "rolled-back change (should be absent)"}
    if workflow_state in applied:
        return {"expected_present": True, "context": "applied", "label": "applied change (should be present)"}
    return {"expected_present": True, "context": "preview", "label": "proposed change (not yet applied — preview only)"}


def vlan_drift_report(
    paths: WorkspacePaths,
    intent_path: Path,
    state_result: dict[str, Any],
    expected_present: bool = True,
    baseline: str = "intended state",
    context: str = "applied",
) -> dict[str, Any]:
    intent = load_intent(intent_path)
    intended = {
        "change_type": intent.change_type,
        "site": intent.site,
        "vlan_id": intent.vlan.id,
        "name": intent.vlan.name,
        "subnet": intent.vlan.subnet,
        "targets": intent.targets.model_dump(),
    }
    # Compare live state against the EXPECTED presence for this baseline.
    verification = verify_vlan_state(state_result, intent.vlan.id, intent.vlan.name, present=expected_present)
    if verification["status"] == "pass":
        status = "in_sync"
        severity = "none"
        message = f"Live state matches the {baseline}."
    elif verification["status"] == "unsupported":
        status = "unknown"
        severity = "warning"
        message = "Live state could not be collected, so drift is unknown."
    elif context == "preview":
        # Not applied yet — a mismatch is expected, not an alarm.
        status = "preview_mismatch"
        severity = "info"
        message = f"This change is not applied yet; live state differs from the {baseline}, as expected."
    else:
        status = "drifted"
        severity = "high"
        if expected_present:
            message = f"Drift: the {baseline} is not present on the live device (an applied change is missing)."
        else:
            message = f"Drift: the {baseline} is still present on the live device (a rolled-back change reappeared out of band)."
    return {
        "ok": status in ("in_sync", "preview_mismatch"),
        "status": status,
        "severity": severity,
        "message": message,
        "baseline": baseline,
        "context": context,
        "expected_present": expected_present,
        "intended": intended,
        "verification": verification,
        "remediation": {
            "workflow": "approve_fix_apply_verify",
            "recommended_next_action": "run_safety_then_dry_run" if status == "drifted" else "collect_state_later",
        },
    }


def compliance_summary(paths: WorkspacePaths) -> dict[str, Any]:
    return {
        "ok": True,
        "scope": "lab_slice",
        "views": [
            {"id": "vlan_compliance", "status": "available", "source": "intent + Rez state"},
            {"id": "management_plane_compliance", "status": "planned", "source": "Rez state"},
            {"id": "routing_policy_compliance", "status": "planned", "source": "Rez routes/BGP"},
            {"id": "segmentation_compliance", "status": "available", "source": "static policy + source of truth"},
            {"id": "template_compliance", "status": "available", "source": "render scope validator"},
        ],
        "remediation_states": ["detect", "classify", "approve_fix", "apply", "verify"],
    }
