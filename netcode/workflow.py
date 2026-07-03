"""Workflow state machine for safe network-as-code actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WorkflowState = Literal[
    "draft",
    "intent_created",
    "rendered",
    "validated",
    "state_collected",
    "dry_run_passed",
    "approval_required",
    "approved",
    "applying",
    "verified",
    "completed",
    "rollback_available",
    "rolling_back",
    "rolled_back",
    "failed",
    "blocked",
]


ACTION_REQUIREMENTS: dict[str, list[WorkflowState]] = {
    "check_safety": [
        "draft",
        "intent_created",
        "rendered",
        "validated",
        "dry_run_passed",
        "rollback_available",
        "rolled_back",
        "failed",
        "blocked",
    ],
    "collect_state": ["validated", "dry_run_passed", "rollback_available", "rolled_back"],
    "dry_run": ["validated", "state_collected", "dry_run_passed", "rollback_available", "rolled_back"],
    "apply": ["dry_run_passed", "approved"],
    "rollback": ["rollback_available", "verified", "completed"],
}


ACTION_ALIASES = {
    "check-safety": "check_safety",
    "check_safety": "check_safety",
    "collect-state": "collect_state",
    "collect_state": "collect_state",
    "dry-run": "dry_run",
    "dry_run": "dry_run",
    "apply": "apply",
    "rollback": "rollback",
}


@dataclass(frozen=True)
class WorkflowSnapshot:
    state: WorkflowState
    allowed_actions: list[str]
    blocked_actions: dict[str, str]
    required_evidence: list[str]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "allowed_actions": self.allowed_actions,
            "blocked_actions": self.blocked_actions,
            "required_evidence": self.required_evidence,
            "message": self.message,
        }


def workflow_snapshot(state: WorkflowState) -> WorkflowSnapshot:
    allowed = [action for action, states in ACTION_REQUIREMENTS.items() if state in states]
    blocked = {
        action: _blocked_reason(action, state)
        for action in ACTION_REQUIREMENTS
        if action not in allowed
    }
    return WorkflowSnapshot(
        state=state,
        allowed_actions=allowed,
        blocked_actions=blocked,
        required_evidence=_required_evidence(state),
        message=_message(state),
    )


def normalize_action(action: str) -> str:
    normalized = ACTION_ALIASES.get(action)
    if not normalized:
        raise KeyError(f"Unknown workflow action {action}")
    return normalized


def is_action_allowed(state: WorkflowState, action: str) -> bool:
    normalized = normalize_action(action)
    return state in ACTION_REQUIREMENTS[normalized]


def require_action_allowed(state: str, action: str) -> WorkflowSnapshot:
    snapshot = workflow_snapshot(state)  # type: ignore[arg-type]
    normalized = normalize_action(action)
    if normalized not in snapshot.allowed_actions:
        reason = snapshot.blocked_actions.get(normalized, "Action is not valid in this workflow state.")
        raise ValueError(f"{action} is blocked in workflow state {state}: {reason}")
    return snapshot


def state_after_static_validation(passed: bool) -> WorkflowSnapshot:
    return workflow_snapshot("validated" if passed else "blocked")


def state_after_lab_action(action: str, passed: bool) -> WorkflowSnapshot:
    if not passed:
        return workflow_snapshot("failed")
    if action == "dry-run":
        return workflow_snapshot("dry_run_passed")
    if action == "apply":
        return workflow_snapshot("rollback_available")
    if action == "rollback":
        return workflow_snapshot("rolled_back")
    return workflow_snapshot("failed")


def _required_evidence(state: WorkflowState) -> list[str]:
    requirements = {
        "draft": ["intent request"],
        "intent_created": ["rendered config", "static validation"],
        "rendered": ["static validation"],
        "validated": ["lab dry-run proof"],
        "state_collected": ["lab dry-run proof"],
        "dry_run_passed": ["approval or lab apply authorization"],
        "approval_required": ["human approval"],
        "approved": ["apply execution evidence"],
        "applying": ["post-change verification"],
        "verified": ["rollback plan", "audit report"],
        "completed": ["audit report"],
        "rollback_available": ["rollback action if needed"],
        "rolling_back": ["rollback verification"],
        "rolled_back": ["rollback audit report"],
        "failed": ["operator review"],
        "blocked": ["fix failed validation or missing evidence"],
    }
    return requirements[state]


def _message(state: WorkflowState) -> str:
    messages = {
        "draft": "No safe network action is available until an intent is checked.",
        "intent_created": "Intent exists, but validation has not completed.",
        "rendered": "Candidate config exists, but validation has not completed.",
        "validated": "Static validation passed. Lab dry-run is the next required proof.",
        "state_collected": "Live state was collected. Lab dry-run is still required before apply.",
        "dry_run_passed": "The candidate was accepted in dry-run. Apply can be considered.",
        "approval_required": "Approval is required before production apply.",
        "approved": "Approval is present. Apply is allowed.",
        "applying": "Apply is in progress. Wait for verification.",
        "verified": "The change was verified. Rollback remains available.",
        "completed": "The workflow completed with evidence.",
        "rollback_available": "The change was applied and verified. Rollback is available.",
        "rolling_back": "Rollback is in progress. Wait for verification.",
        "rolled_back": "Rollback completed and was verified.",
        "failed": "The workflow failed. Later actions are blocked until reviewed.",
        "blocked": "The workflow is blocked by failed or missing evidence.",
    }
    return messages[state]


def _blocked_reason(action: str, state: WorkflowState) -> str:
    if action == "dry_run":
        return "Static validation must pass first." if state in {"draft", "intent_created", "rendered", "blocked"} else "Action is not valid in this workflow state."
    if action == "apply":
        return "Dry-run proof is required before apply."
    if action == "rollback":
        return "Rollback is only available after a successful apply."
    if action == "collect_state":
        return "State collection requires a validated target."
    return "Action is not valid in this workflow state."
