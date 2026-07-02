"""Drift and compliance helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netcode.models import load_intent
from netcode.paths import WorkspacePaths
from netcode.verification import verify_vlan_state


def vlan_drift_report(paths: WorkspacePaths, intent_path: Path, state_result: dict[str, Any]) -> dict[str, Any]:
    intent = load_intent(intent_path)
    intended = {
        "change_type": intent.change_type,
        "site": intent.site,
        "vlan_id": intent.vlan.id,
        "name": intent.vlan.name,
        "subnet": intent.vlan.subnet,
        "targets": intent.targets.model_dump(),
    }
    verification = verify_vlan_state(state_result, intent.vlan.id, intent.vlan.name, present=True)
    if verification["status"] == "pass":
        status = "in_sync"
        severity = "none"
        message = "Live state matches the intended VLAN state."
    elif verification["status"] == "unsupported":
        status = "unknown"
        severity = "warning"
        message = "Live state could not be collected, so drift is unknown."
    else:
        status = "drifted"
        severity = "high"
        message = "Live state does not match the intended VLAN state."
    return {
        "ok": status == "in_sync",
        "status": status,
        "severity": severity,
        "message": message,
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
