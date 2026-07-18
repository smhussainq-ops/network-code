"""Governed Git, change, activation, and rollback lifecycle for Network Model revisions."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from netcode.gitflow import commit_change_artifacts, materialize_model_revision, setup_git_workspace
from netcode.network_model import (
    NETWORK_MODEL_SCHEMA,
    NetworkModelError,
    prepare_reviewed_approval,
    utc_now,
)
from netcode.network_model_store import NetworkModelRepository
from netcode.store import PlatformStore


VERIFIED_CHANGE_STATES = {"verified", "completed"}
ROLLBACK_CHANGE_STATES = {"rolled_back"}

CHANGE_MODEL_DOMAINS = {
    "add_vlan": "topology",
    "interface_config": "topology",
    "bgp_neighbor": "routing",
    "acl_rule": "security_policy",
    "site_device_intent": "identity",
    "ntp_standardize": "golden_standards",
    "routing_redistribution": "route_propagation",
}


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


def model_patch_from_intent(
    intent: Mapping[str, Any],
    *,
    device_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Translate typed intent into model data without importing raw CLI.

    Custom configuration intentionally has no mapping. A raw command blob is an
    execution artifact, not approved operational intent.
    """
    change_type = str(intent.get("change_type") or "").strip()
    domain = CHANGE_MODEL_DOMAINS.get(change_type)
    site = str(intent.get("site") or "").strip()
    if not domain or not site or not device_id:
        return {}, []

    if change_type == "add_vlan":
        vlan = _dict(intent.get("vlan"))
        identity = str(vlan.get("id") or vlan.get("vlan_id") or "").strip()
        domain_intent = {"vlans": {identity: vlan}} if identity and vlan else {}
    elif change_type == "interface_config":
        interface = _dict(intent.get("interface"))
        identity = str(interface.get("name") or "").strip()
        domain_intent = {"interfaces": {identity: interface}} if identity and interface else {}
    elif change_type == "bgp_neighbor":
        bgp = _dict(intent.get("bgp"))
        identity = str(bgp.get("neighbor_ip") or bgp.get("neighbor") or "").strip()
        domain_intent = {"bgp_neighbors": {identity: bgp}} if identity and bgp else {}
    elif change_type == "acl_rule":
        acl = _dict(intent.get("acl"))
        identity = str(acl.get("name") or acl.get("acl_name") or "").strip()
        domain_intent = {"acl_rules": {identity: acl}} if identity and acl else {}
    elif change_type == "site_device_intent":
        device = _dict(intent.get("device"))
        domain_intent = {"device": device} if device else {}
    elif change_type == "ntp_standardize":
        ntp = _dict(intent.get("ntp"))
        domain_intent = {"ntp": ntp} if ntp else {}
    elif change_type == "routing_redistribution":
        redistribution = _dict(intent.get("redistribution"))
        identity = ":".join(
            str(redistribution.get(key) or "").strip().lower()
            for key in ("from_protocol", "to_protocol", "target_process")
        ).strip(":")
        if not identity or not redistribution:
            domain_intent = {}
        else:
            boundary = copy.deepcopy(redistribution)
            if isinstance(intent.get("reverse_redistribution"), Mapping):
                boundary["reverse_redistribution"] = _dict(intent.get("reverse_redistribution"))
            if isinstance(intent.get("reachability_checks"), (list, tuple)):
                boundary["reachability_checks"] = copy.deepcopy(list(intent["reachability_checks"]))
            domain_intent = {"redistribution_boundaries": {identity: boundary}}
    else:
        domain_intent = {}

    if not domain_intent:
        return {}, []
    return {
        "sites": {
            site: {
                "devices": {
                    device_id: {
                        "intent": {domain: domain_intent},
                    }
                }
            }
        }
    }, [domain]


def _interface_operational_dependency(
    intent: Mapping[str, Any],
    *,
    device_id: str,
) -> tuple[str, dict[str, Any]] | None:
    """Translate a verified admin-state repair into future RCA intent."""
    if str(intent.get("change_type") or "").strip() != "interface_config":
        return None
    interface = _dict(intent.get("interface"))
    if interface.get("enabled") is not True or str(interface.get("apply_scope") or "") != "admin_state":
        return None
    name = str(interface.get("name") or "").strip()
    site = str(intent.get("site") or "").strip()
    if not name or not site or not device_id:
        return None
    identity_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    dependency = {
        "id": f"{device_id}:interface:{identity_slug}",
        "device_id": device_id,
        "kind": "interface",
        "domain": "topology",
        "identity": {"interface": name},
        "expected": {"admin_state": "up", "oper_state": "up"},
        "atom_ids": ["L1_INTERFACE_ADMIN_DOWN", "L1_INTERFACE_OPER_DOWN"],
        "remediation": {
            "root_atom_id": "L1_INTERFACE_ADMIN_DOWN",
            "change_type": "interface_config",
            "values": {
                "interface": name,
                "enabled": True,
                "apply_scope": "admin_state",
            },
            "interface": {
                "name": name,
                "enabled": True,
                "apply_scope": "admin_state",
            },
        },
    }
    return site, dependency


def _attach_operational_dependency(
    patch: Mapping[str, Any],
    base_model: Mapping[str, Any],
    intent: Mapping[str, Any],
    *,
    device_id: str,
) -> dict[str, Any]:
    result = _dict(patch)
    built = _interface_operational_dependency(intent, device_id=device_id)
    if built is None:
        return result
    site, dependency = built
    existing_site = _dict(_dict(base_model.get("sites")).get(site))
    dependencies = [
        copy.deepcopy(dict(item))
        for item in existing_site.get("operational_dependencies") or []
        if isinstance(item, Mapping) and str(item.get("id") or "") != dependency["id"]
    ]
    dependencies.append(dependency)
    dependencies.sort(key=lambda item: str(item.get("id") or ""))
    sites = result.setdefault("sites", {})
    site_patch = sites.setdefault(site, {})
    site_patch["operational_dependencies"] = dependencies
    return result


def create_candidate_for_change_intent(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    parent_revision_id: str,
    change_id: str,
    intent: Mapping[str, Any],
    device_id: str,
    created_by: str,
) -> dict[str, Any] | None:
    """Create one deterministic candidate for a typed change.

    The explicit parent prevents a stale UI or Rez incident from attaching a
    proposal to whichever environment happens to be active later.
    """
    active = repository.active_revision(org_id, environment_id)
    if active is None:
        raise NetworkModelError("a change candidate requires an active approved Network Model")
    if active["revision_id"] != parent_revision_id:
        raise NetworkModelError(
            f"Network Model revision changed from {parent_revision_id} to {active['revision_id']}"
        )
    patch, domains = model_patch_from_intent(intent, device_id=device_id)
    patch = _attach_operational_dependency(
        patch,
        active["model"],
        intent,
        device_id=device_id,
    )
    if not patch or not domains:
        return None
    revision_id = f"change-{change_id.lower()}"
    try:
        existing = repository.get_revision(org_id, environment_id, revision_id)
    except KeyError:
        existing = None
    if existing is not None:
        if str(existing.get("source", {}).get("reference") or "") != f"change:{change_id}":
            raise NetworkModelError(f"revision {revision_id} is already owned by another source")
        return existing
    return create_candidate_from_change(
        repository,
        platform_store,
        org_id=org_id,
        environment_id=environment_id,
        parent_revision_id=parent_revision_id,
        revision_id=revision_id,
        change_id=change_id,
        domains=domains,
        model_patch=patch,
        created_by=created_by,
    )


def create_candidate_for_change_set(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    parent_revision_id: str,
    revision_id: str,
    change_ids: Sequence[str],
    intent: Mapping[str, Any],
    device_ids: Sequence[str],
    created_by: str,
) -> dict[str, Any] | None:
    """Create one aggregate candidate linked to every per-device fleet change."""
    changes = [str(item).strip() for item in change_ids if str(item).strip()]
    devices = [str(item).strip() for item in device_ids if str(item).strip()]
    if not changes or len(changes) != len(devices):
        raise NetworkModelError("fleet model candidate requires one change ID per target device")
    active = repository.active_revision(org_id, environment_id)
    if active is None or active["revision_id"] != parent_revision_id:
        current = active["revision_id"] if active else "none"
        raise NetworkModelError(
            f"Network Model revision changed from {parent_revision_id} to {current}"
        )
    combined: dict[str, Any] = {}
    domains: set[str] = set()
    for device_id in devices:
        patch, patch_domains = model_patch_from_intent(intent, device_id=device_id)
        patch = _attach_operational_dependency(
            patch,
            _deep_merge(active["model"], combined),
            intent,
            device_id=device_id,
        )
        combined = _deep_merge(combined, patch)
        domains.update(patch_domains)
    if not combined or not domains:
        return None
    try:
        existing = repository.get_revision(org_id, environment_id, revision_id)
    except KeyError:
        existing = None
    if existing is None:
        existing = create_candidate_from_change(
            repository,
            platform_store,
            org_id=org_id,
            environment_id=environment_id,
            parent_revision_id=parent_revision_id,
            revision_id=revision_id,
            change_id=changes[0],
            domains=sorted(domains),
            model_patch=combined,
            created_by=created_by,
        )
    for change_id in changes[1:]:
        change = platform_store.get_change(change_id)
        if change.org_id != org_id:
            raise NetworkModelError("fleet change and model revision must belong to the same organization")
        repository.link_revision(
            org_id,
            environment_id,
            revision_id,
            link_type="change",
            external_id=change_id,
            metadata={"workflow_state": change.workflow_state, "fleet": True},
        )
    return existing


def approve_change_candidates(
    repository: NetworkModelRepository,
    *,
    org_id: str,
    change_id: str,
    approved_by: str,
    git_root,
) -> list[dict[str, Any]]:
    linked = repository.revisions_for_link(org_id, link_type="change", external_id=change_id)
    if len(linked) > 1:
        raise NetworkModelError("a single-device change may link to only one Network Model candidate")
    approved: list[dict[str, Any]] = []
    for revision in linked:
        status = str(revision.get("status") or "")
        if status in {"proposed", "in_review"}:
            approved.append(
                approve_with_git(
                    repository,
                    org_id=org_id,
                    environment_id=str(revision["environment_id"]),
                    revision_id=str(revision["revision_id"]),
                    approved_by=approved_by,
                    git_root=git_root,
                )
            )
        elif status in {"approved", "active"}:
            approved.append({"revision": revision, "git": None})
        else:
            raise NetworkModelError(f"linked Network Model candidate is {status}, not reviewable")
    return approved


def activate_change_candidates(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    change_id: str,
    actor: str,
    git_root,
) -> list[dict[str, Any]]:
    linked = repository.revisions_for_link(org_id, link_type="change", external_id=change_id)
    if len(linked) > 1:
        raise NetworkModelError("a single-device change may link to only one Network Model candidate")
    activated: list[dict[str, Any]] = []
    for revision in linked:
        status = str(revision.get("status") or "")
        if status == "approved":
            change_links = [
                link
                for link in repository.list_links(
                    org_id,
                    str(revision["environment_id"]),
                    str(revision["revision_id"]),
                )
                if link["link_type"] == "change"
            ]
            pending = [
                link["external_id"]
                for link in change_links
                if platform_store.get_change(link["external_id"]).workflow_state
                not in VERIFIED_CHANGE_STATES
            ]
            if pending:
                activated.append(
                    {
                        "revision": revision,
                        "verification": None,
                        "git": None,
                        "pending_change_ids": pending,
                    }
                )
                continue
            activated.append(
                activate_verified_revision(
                    repository,
                    platform_store,
                    org_id=org_id,
                    environment_id=str(revision["environment_id"]),
                    revision_id=str(revision["revision_id"]),
                    actor=actor,
                    git_root=git_root,
                )
            )
        elif status == "active":
            activated.append({"revision": revision, "verification": None, "git": None})
        else:
            raise NetworkModelError(f"linked Network Model candidate is {status}, not approved")
    return activated


def assert_change_model_rollback_is_current(
    repository: NetworkModelRepository,
    *,
    org_id: str,
    change_id: str,
) -> list[dict[str, Any]]:
    """Block a stale device rollback before it can invalidate newer intent."""
    linked = repository.revisions_for_link(
        org_id,
        link_type="change",
        external_id=change_id,
    )
    if len(linked) > 1:
        raise NetworkModelError("a device change may link to only one Network Model candidate")
    for revision in linked:
        if str(revision.get("status") or "") == "superseded":
            raise NetworkModelError(
                "this change belongs to a superseded Network Model revision; "
                "build a new reviewed rollback plan from the current model"
            )
    return linked


def rollback_change_candidates(
    repository: NetworkModelRepository,
    platform_store: PlatformStore,
    *,
    org_id: str,
    change_id: str,
    actor: str,
    git_root,
) -> list[dict[str, Any]]:
    """Restore the direct parent when a linked, active change is rolled back.

    Candidates that never became active require no model mutation. A
    superseded candidate is rejected because reverting it would discard newer
    approved intent.
    """
    linked = assert_change_model_rollback_is_current(
        repository,
        org_id=org_id,
        change_id=change_id,
    )
    restored: list[dict[str, Any]] = []
    for revision in linked:
        status = str(revision.get("status") or "")
        if status == "active":
            parent_revision_id = str(revision.get("parent_revision_id") or "").strip()
            if not parent_revision_id:
                raise NetworkModelError("the active change revision has no rollback parent")
            restored.append(
                rollback_active_revision(
                    repository,
                    platform_store,
                    org_id=org_id,
                    environment_id=str(revision["environment_id"]),
                    target_revision_id=parent_revision_id,
                    rollback_change_id=change_id,
                    actor=actor,
                    git_root=git_root,
                )
            )
        elif status in {"proposed", "in_review", "approved"}:
            restored.append(
                {
                    "revision": revision,
                    "verification": None,
                    "git": None,
                    "model_unchanged": True,
                }
            )
        else:
            raise NetworkModelError(f"linked Network Model candidate is {status}, not rollback-safe")
    return restored


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
    preview = prepare_reviewed_approval(
        current,
        approved_by=approval_actor,
        approved_at=approval_time,
    )
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
    if str(current.get("parent_revision_id") or "") != target_revision_id:
        raise NetworkModelError("rollback target must be the active revision's direct parent")
    target = repository.get_revision(org_id, environment_id, target_revision_id)
    if target["status"] != "superseded":
        raise NetworkModelError("rollback target must be a previously approved superseded revision")
    change = platform_store.get_change(rollback_change_id)
    if change.org_id != org_id or change.workflow_state not in ROLLBACK_CHANGE_STATES:
        raise NetworkModelError("rollback model activation requires a verified rollback change")
    linked = repository.revisions_for_link(
        org_id,
        link_type="change",
        external_id=rollback_change_id,
        environment_id=environment_id,
    )
    if not any(str(item.get("revision_id") or "") == current["revision_id"] for item in linked):
        raise NetworkModelError("rollback change is not linked to the currently active model revision")
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
