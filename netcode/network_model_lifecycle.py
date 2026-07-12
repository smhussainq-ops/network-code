"""Governed Git, change, activation, and rollback lifecycle for Network Model revisions."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from netcode.gitflow import commit_change_artifacts, materialize_model_revision, setup_git_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA, NetworkModelError, utc_now
from netcode.network_model_store import NetworkModelRepository
from netcode.store import PlatformStore


VERIFIED_CHANGE_STATES = {"verified", "completed"}
ROLLBACK_CHANGE_STATES = {"rolled_back", "verified", "completed"}


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = _dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def model_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append({"path": child, "action": "add", "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child, "action": "remove", "before": before[key], "after": None})
            else:
                changes.extend(model_diff(before[key], after[key], child))
        return changes
    if before != after:
        return [{"path": path or "model", "action": "replace", "before": before, "after": after}]
    return []


def create_candidate_from_change(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    parent_revision_id: str,
    revision_id: str,
    change_id: str,
    domains: Sequence[str],
    model_patch: Mapping[str, Any],
    created_by: str,
) -> dict[str, Any]:
    parent = repository.get_revision(org_id, environment_id, parent_revision_id)
    if parent["status"] not in {"approved", "active"}:
        raise NetworkModelError("candidate parent must be approved or active")
    change = platform_store.get_change(change_id)
    if change.org_id != org_id:
        raise NetworkModelError("change and model revision must belong to the same organization")
    changed_domains = {str(item).strip().lower() for item in domains if str(item).strip()}
    if not changed_domains:
        raise NetworkModelError("candidate revision requires at least one changed domain")
    authority = copy.deepcopy(parent["authority_bindings"])
    for domain in changed_domains:
        authority[domain] = {"source": "netcode_change", "mode": "propose"}
    coverage = sorted(set(parent["coverage"]["domains"]) | changed_domains)
    candidate = {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": org_id,
        "environment_id": environment_id,
        "revision_id": revision_id,
        "parent_revision_id": parent_revision_id,
        "status": "proposed",
        "source": {"type": "netcode_change", "reference": f"change:{change_id}"},
        "coverage": {"domains": coverage},
        "authority_bindings": authority,
        "model": _deep_merge(parent["model"], model_patch),
    }
    created = repository.create_revision(candidate, created_by=created_by)
    repository.link_revision(
        org_id,
        environment_id,
        revision_id,
        link_type="change",
        external_id=change_id,
        metadata={"workflow_state": change.workflow_state},
    )
    return created


def approve_with_git(
    repository: NetworkModelRepository,
    *,
    org_id: str,
    environment_id: str,
    revision_id: str,
    approved_by: str,
    git_root,
) -> dict[str, Any]:
    current = repository.get_revision(org_id, environment_id, revision_id)
    existing_approval = _dict(current.get("approval"))
    approval_time = str(existing_approval.get("approved_at") or utc_now())
    approval_actor = str(existing_approval.get("approved_by") or approved_by)
    preview = copy.deepcopy(current)
    preview["status"] = "approved"
    preview["approval"] = {
        "status": "approved",
        "approved_by": approval_actor,
        "approved_at": approval_time,
    }
    preview["authority_bindings"] = {
        domain: {**binding, "mode": "authoritative"}
        for domain, binding in preview["authority_bindings"].items()
    }
    parent_model: dict[str, Any] = {}
    if preview.get("parent_revision_id"):
        parent_model = repository.get_revision(
            org_id, environment_id, str(preview["parent_revision_id"])
        )["model"]
    setup = setup_git_workspace(git_root)
    if not setup.get("ok"):
        raise NetworkModelError("Git change-history workspace could not be initialized")
    artifact = materialize_model_revision(
        git_root,
        revision=preview,
        diff=model_diff(parent_model, preview["model"]),
    )
    checkpoint = commit_change_artifacts(
        git_root,
        f"Approve network model {revision_id}",
        list(artifact["paths"]),
    )
    if not checkpoint.get("ok") or not checkpoint.get("commit"):
        raise NetworkModelError(str(checkpoint.get("message") or "Git model checkpoint failed"))
    approved = repository.approve_revision(
        org_id,
        environment_id,
        revision_id,
        approved_by=approval_actor,
        approved_at=approval_time,
    )
    repository.link_revision(
        org_id,
        environment_id,
        revision_id,
        link_type="git_approval",
        external_id=str(checkpoint["commit"]),
        metadata={"branch": checkpoint.get("branch"), "paths": artifact["paths"]},
    )
    return {"revision": approved, "git": checkpoint, "artifact": artifact}


def _linked_change_evidence(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    revision_id: str,
    accepted_states: set[str],
) -> list[dict[str, Any]]:
    links = [
        link for link in repository.list_links(org_id, environment_id, revision_id) if link["link_type"] == "change"
    ]
    evidence: list[dict[str, Any]] = []
    for link in links:
        change = platform_store.get_change(link["external_id"])
        if change.org_id != org_id:
            raise NetworkModelError("linked change belongs to a different organization")
        evidence.append({"change_id": change.id, "workflow_state": change.workflow_state, "status": change.status})
    if links and any(item["workflow_state"] not in accepted_states for item in evidence):
        raise NetworkModelError("all linked changes must have fresh successful verification before model activation")
    return evidence


def activate_verified_revision(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    revision_id: str,
    actor: str,
    git_root,
    initial_baseline: bool = False,
) -> dict[str, Any]:
    revision = repository.get_revision(org_id, environment_id, revision_id)
    if revision["status"] != "approved":
        raise NetworkModelError("only an approved revision can become active")
    current = repository.active_revision(org_id, environment_id)
    evidence = _linked_change_evidence(
        repository,
        platform_store,
        org_id=org_id,
        environment_id=environment_id,
        revision_id=revision_id,
        accepted_states=VERIFIED_CHANGE_STATES,
    )
    if not evidence and not (initial_baseline and current is None):
        raise NetworkModelError("activation requires verified linked changes or an explicit first baseline review")
    verification = {
        "schema": "rezonance.network-model-verification.v1",
        "verified_by": actor,
        "verified_at": utc_now(),
        "initial_baseline": bool(initial_baseline and current is None),
        "changes": evidence,
    }
    parent_model = (
        repository.get_revision(org_id, environment_id, str(revision.get("parent_revision_id")))["model"]
        if revision.get("parent_revision_id")
        else {}
    )
    artifact = materialize_model_revision(
        git_root,
        revision=revision,
        diff=model_diff(parent_model, revision["model"]),
        verification=verification,
    )
    checkpoint = commit_change_artifacts(
        git_root,
        f"Verify network model {revision_id}",
        list(artifact["paths"]),
    )
    if not checkpoint.get("ok") or not checkpoint.get("commit"):
        raise NetworkModelError(str(checkpoint.get("message") or "Git verification checkpoint failed"))
    active = repository.activate_revision(org_id, environment_id, revision_id)
    repository.link_revision(
        org_id,
        environment_id,
        revision_id,
        link_type="git_verification",
        external_id=str(checkpoint["commit"]),
        metadata=verification,
    )
    return {"revision": active, "verification": verification, "git": checkpoint}


def rollback_active_revision(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    target_revision_id: str,
    rollback_change_id: str,
    actor: str,
    git_root,
) -> dict[str, Any]:
    current = repository.active_revision(org_id, environment_id)
    if current is None or current["revision_id"] == target_revision_id:
        raise NetworkModelError("rollback requires a different currently active revision")
    target = repository.get_revision(org_id, environment_id, target_revision_id)
    if target["status"] != "superseded":
        raise NetworkModelError("rollback target must be a previously approved superseded revision")
    change = platform_store.get_change(rollback_change_id)
    if change.org_id != org_id or change.workflow_state not in ROLLBACK_CHANGE_STATES:
        raise NetworkModelError("rollback model activation requires a verified rollback change")
    verification = {
        "schema": "rezonance.network-model-rollback.v1",
        "rolled_back_by": actor,
        "rolled_back_at": utc_now(),
        "from_revision": current["revision_id"],
        "to_revision": target_revision_id,
        "change_id": rollback_change_id,
        "workflow_state": change.workflow_state,
    }
    artifact = materialize_model_revision(
        git_root,
        revision=target,
        diff=model_diff(current["model"], target["model"]),
        verification=verification,
    )
    checkpoint = commit_change_artifacts(
        git_root,
        f"Rollback network model to {target_revision_id}",
        list(artifact["paths"]),
    )
    if not checkpoint.get("ok") or not checkpoint.get("commit"):
        raise NetworkModelError(str(checkpoint.get("message") or "Git rollback checkpoint failed"))
    restored = repository.activate_revision(
        org_id, environment_id, target_revision_id, allow_superseded=True
    )
    repository.link_revision(
        org_id,
        environment_id,
        target_revision_id,
        link_type="rollback_change",
        external_id=rollback_change_id,
        metadata=verification,
    )
    return {"revision": restored, "verification": verification, "git": checkpoint}
