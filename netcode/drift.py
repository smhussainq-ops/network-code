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


_APPLIED_STATES = {"rollback_available", "completed", "verified", "applying"}


def aggregate_device_vlans(device_changes: list[dict[str, Any]], load_intent_fn) -> list[dict[str, Any]]:
    """Build a device's expected VLAN baseline from its APPLIED (live, not rolled-back)
    changes — the committed source of truth for that device. Newest change wins per VLAN."""
    expected: dict[int, dict[str, Any]] = {}
    for change in device_changes:  # callers pass newest-first
        if change.get("workflow_state") not in _APPLIED_STATES:
            continue
        try:
            intent = load_intent_fn(Path(str(change.get("intent_path") or "")))
        except Exception:
            continue
        if intent.change_type != "add_vlan":
            continue
        if intent.vlan.id not in expected:
            expected[intent.vlan.id] = {"vlan_id": intent.vlan.id, "name": intent.vlan.name, "change_id": change.get("id")}
    return list(expected.values())


def device_drift_from_state(expected: list[dict[str, Any]], state_result: dict[str, Any], device_id: str) -> dict[str, Any]:
    """Compare live device state against the device's expected VLAN baseline."""
    rows: list[dict[str, Any]] = []
    drifted = 0
    unknown = state_result.get("ok") is False
    for entry in expected:
        verification = verify_vlan_state(state_result, entry["vlan_id"], entry["name"], present=True)
        present = verification["status"] == "pass"
        if not present:
            drifted += 1
        rows.append({**entry, "present": present, "status": "in_sync" if present else "drifted"})
    if unknown:
        status, severity = "unknown", "warning"
        message = f"Could not read {device_id}; device drift is unknown."
    elif not expected:
        status, severity = "in_sync", "none"
        message = f"No committed VLAN intents recorded for {device_id} yet — nothing to compare."
    elif drifted:
        status, severity = "drifted", "high"
        message = f"{len(expected) - drifted}/{len(expected)} committed VLAN intents present on {device_id}; {drifted} missing."
    else:
        status, severity = "in_sync", "none"
        message = f"All {len(expected)} committed VLAN intents are present on {device_id}."
    return {
        "ok": status in ("in_sync",),
        "device_id": device_id,
        "status": status,
        "severity": severity,
        "expected_count": len(expected),
        "drifted_count": drifted,
        "vlans": rows,
        "message": message,
        "baseline": "committed source of truth (all applied VLAN intents on this device)",
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
