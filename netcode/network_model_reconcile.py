"""Freshness-aware observed-versus-approved Network Model reconciliation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from netcode.network_model import NetworkModelError
from netcode.network_model_store import NetworkModelRepository


VALIDATED_GRADES = {"fresh", "validated", "device_authoritative", "controller_authoritative"}
DEPENDENCY_DOMAINS = {
    "interface": "topology",
    "lldp": "topology",
    "ospf": "routing",
    "bgp": "routing",
    "default_route": "routing",
    "sdwan": "sdwan",
    "qos": "qos",
    "firewall_policy": "security_policy",
    "nat": "security_policy",
    "vpn": "vpn",
    "ha": "ha",
    "vlan": "switching",
    "trunk": "switching",
    "stp": "switching",
    "lacp": "switching",
    "multi_chassis": "switching",
    "fhrp": "routing",
    "vrf": "routing",
    "dhcp_relay": "services",
    "wireless_controller": "wireless",
    "wireless_ap": "wireless",
    "nac": "security_policy",
    "multicast": "routing",
    "evpn_vxlan": "fabric",
}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _dependencies(model: Mapping[str, Any], *, site_id: str = "", device_id: str = "") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    top_level = model.get("operational_dependencies") or []
    if isinstance(top_level, Mapping):
        top_level = [{"id": key, **_dict(value)} for key, value in top_level.items()]
    for raw in top_level if isinstance(top_level, list) else []:
        item = _dict(raw)
        item.setdefault("site_id", str(item.get("site") or ""))
        if site_id and str(item.get("site_id") or "") != site_id:
            continue
        found.append(item)
    for raw_site_id, raw_site in _dict(model.get("sites")).items():
        if site_id and str(raw_site_id) != site_id:
            continue
        for raw in _dict(raw_site).get("operational_dependencies") or []:
            item = _dict(raw)
            item["site_id"] = str(raw_site_id)
            found.append(item)
    if device_id:
        found = [item for item in found if str(item.get("device_id") or "") == device_id]
    ids: set[str] = set()
    for item in found:
        dependency_id = str(item.get("id") or "").strip()
        if not dependency_id:
            raise NetworkModelError("modeled operational dependencies require stable ids")
        if dependency_id in ids:
            raise NetworkModelError(f"duplicate modeled dependency id {dependency_id}")
        ids.add(dependency_id)
    return sorted(found, key=lambda item: str(item.get("id")))


def _compare(expected: Any, actual: Any, path: str = "") -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if isinstance(expected, Mapping):
        actual_map = actual if isinstance(actual, Mapping) else {}
        for key, value in expected.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in actual_map:
                mismatches.append({"path": child, "expected": value, "actual": None})
            else:
                mismatches.extend(_compare(value, actual_map[key], child))
    elif expected != actual:
        mismatches.append({"path": path or "value", "expected": expected, "actual": actual})
    return mismatches


def reconcile_revision(
    repository: NetworkModelRepository,
    revision: Mapping[str, Any],
    *,
    site_id: str = "",
    device_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if str(revision.get("status") or "") not in {"approved", "active"}:
        raise NetworkModelError("reconciliation requires an approved or active model revision")
    dependencies = _dependencies(_dict(revision.get("model")), site_id=site_id, device_id=device_id)
    coverage = {
        str(item).strip().lower()
        for item in _dict(revision.get("coverage")).get("domains", [])
        if str(item).strip()
    }
    subjects: list[tuple[str, str]] = []
    for dependency in dependencies:
        domain = DEPENDENCY_DOMAINS.get(str(dependency.get("kind") or "").strip().lower(), "")
        if domain:
            subjects.append((domain, str(dependency["id"])))
            if dependency.get("device_id"):
                subjects.append((domain, str(dependency["device_id"])))
    observations = repository.current_observations(
        str(revision["org_id"]), str(revision["environment_id"]), subjects
    )
    current_time = now or datetime.now(timezone.utc)
    findings: list[dict[str, Any]] = []
    for dependency in dependencies:
        dependency_id = str(dependency["id"])
        device = str(dependency.get("device_id") or "")
        kind = str(dependency.get("kind") or "").strip().lower()
        domain = DEPENDENCY_DOMAINS.get(kind, "")
        base = {
            "dependency_id": dependency_id,
            "site_id": str(dependency.get("site_id") or ""),
            "device_id": device,
            "kind": kind,
            "domain": domain or "unknown",
        }
        if not domain or domain not in coverage:
            findings.append({**base, "status": "unknown", "reason": "domain_not_covered"})
            continue
        observation = observations.get((domain, dependency_id)) or observations.get((domain, device))
        if not observation:
            findings.append({**base, "status": "unknown", "reason": "no_fresh_observation"})
            continue
        expires = _parse_timestamp(observation.get("expires_at"))
        if not expires or expires <= current_time:
            findings.append(
                {
                    **base,
                    "status": "unknown",
                    "reason": "stale_or_unknown_freshness",
                    "observation_id": observation["observation_id"],
                }
            )
            continue
        if str(observation.get("validation_grade") or "") not in VALIDATED_GRADES:
            findings.append(
                {
                    **base,
                    "status": "unknown",
                    "reason": "observation_not_validated",
                    "observation_id": observation["observation_id"],
                }
            )
            continue
        facts = _dict(observation.get("facts"))
        if observation.get("subject_id") == device:
            dependencies = _dict(facts.get("dependencies"))
            if dependency_id not in dependencies:
                findings.append(
                    {
                        **base,
                        "status": "unknown",
                        "reason": "dependency_not_observed",
                        "observation_id": observation["observation_id"],
                    }
                )
                continue
            facts = _dict(dependencies.get(dependency_id))
        actual = _dict(facts.get("actual")) or facts
        expected = {**_dict(dependency.get("identity")), **_dict(dependency.get("expected"))}
        mismatches = _compare(expected, actual)
        findings.append(
            {
                **base,
                "status": "drift" if mismatches else "match",
                "reason": "expected_state_differs" if mismatches else "expected_state_observed",
                "observation_id": observation["observation_id"],
                "observed_at": observation["observed_at"],
                "expires_at": observation.get("expires_at"),
                "mismatches": mismatches,
            }
        )

    counts = {status: sum(1 for item in findings if item["status"] == status) for status in ("match", "drift", "unknown")}
    if not dependencies:
        overall = "unknown"
        reason = "no_modeled_dependencies"
    elif counts["drift"]:
        overall = "drift"
        reason = "approved_dependencies_differ"
    elif counts["unknown"]:
        overall = "unknown"
        reason = "insufficient_fresh_validated_evidence"
    else:
        overall = "match"
        reason = "all_modeled_dependencies_match"
    summary = {"status": overall, "reason": reason, "modeled_dependencies": len(dependencies), **counts}
    reconciliation_id = f"rec-{uuid.uuid4().hex[:16]}"
    return repository.save_reconciliation(
        org_id=str(revision["org_id"]),
        environment_id=str(revision["environment_id"]),
        reconciliation_id=reconciliation_id,
        revision_id=str(revision["revision_id"]),
        site_id=site_id,
        device_id=device_id,
        status=overall,
        summary=summary,
        findings=findings,
    )
