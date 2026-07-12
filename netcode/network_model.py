"""Authority contracts for the Rezonance operational network model.

The network model keeps approved intent separate from live observations.  This
module deliberately has no persistence concerns so every storage and import
path can share the same fail-closed validation boundary.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Mapping


NETWORK_MODEL_SCHEMA = "rezonance.network-model.v1"
NETWORK_OBSERVATION_SCHEMA = "rezonance.network-observation.v1"

MODEL_STATUSES = {"proposed", "in_review", "approved", "active", "rejected", "superseded"}
APPROVED_STATUSES = {"approved", "active", "superseded"}
OBSERVATION_ONLY_SOURCES = {"discovery", "incident", "telemetry", "device", "controller_observation"}
AUTHORITY_MODES = {"authoritative", "propose", "observe"}
BUILTIN_DOMAINS = {
    "identity",
    "sites",
    "topology",
    "address_plan",
    "routing",
    "route_propagation",
    "reachability",
    "sdwan",
    "qos",
    "security_policy",
    "vpn",
    "ha",
    "golden_standards",
}

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SENSITIVE_PARTS = {
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "community_string",
    "api_key",
}


class NetworkModelError(ValueError):
    """Raised when a model document crosses an authority or safety boundary."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise NetworkModelError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NetworkModelError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise NetworkModelError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _IDENTIFIER.fullmatch(normalized):
        raise NetworkModelError(f"{field} must be a stable lowercase identifier")
    return normalized


def _assert_no_secrets(value: Any, path: str = "document") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if any(part in normalized for part in _SENSITIVE_PARTS):
                raise NetworkModelError(f"{path}.{key} is credential-shaped and cannot enter the network model")
            _assert_no_secrets(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secrets(item, f"{path}[{index}]")


def assert_no_secrets(value: Any, path: str = "document") -> None:
    """Public guard for auxiliary model records such as conflict resolutions."""
    _assert_no_secrets(value, path)


def validate_authority_bindings(value: Any, coverage: set[str]) -> dict[str, dict[str, str]]:
    """Validate domain-specific authority without imposing a provider catalog."""
    bindings = _dict(value)
    normalized: dict[str, dict[str, str]] = {}
    for raw_domain, raw_binding in bindings.items():
        domain = _identifier(raw_domain, "authority_bindings domain")
        binding = _dict(raw_binding)
        source = _identifier(binding.get("source"), f"authority_bindings.{domain}.source")
        mode = str(binding.get("mode") or "").strip().lower()
        if mode not in AUTHORITY_MODES:
            raise NetworkModelError(
                f"authority_bindings.{domain}.mode must be one of {sorted(AUTHORITY_MODES)}"
            )
        if mode == "authoritative" and source in OBSERVATION_ONLY_SOURCES:
            raise NetworkModelError(
                f"authority_bindings.{domain} cannot make observation-only source {source!r} authoritative"
            )
        normalized[domain] = {"source": source, "mode": mode}

    missing = sorted(domain for domain in coverage if domain not in normalized)
    if missing:
        raise NetworkModelError(f"authority_bindings missing covered domains: {', '.join(missing)}")
    return normalized


def validate_model_revision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized defensive copy of one candidate or approved revision."""
    document = copy.deepcopy(_dict(value))
    if document.get("schema") != NETWORK_MODEL_SCHEMA:
        raise NetworkModelError(f"schema must be {NETWORK_MODEL_SCHEMA}")

    document["org_id"] = _identifier(document.get("org_id"), "org_id")
    document["environment_id"] = _identifier(document.get("environment_id"), "environment_id")
    document["revision_id"] = _identifier(document.get("revision_id"), "revision_id")
    if document.get("parent_revision_id"):
        document["parent_revision_id"] = _identifier(document["parent_revision_id"], "parent_revision_id")

    status = str(document.get("status") or "").strip().lower()
    if status not in MODEL_STATUSES:
        raise NetworkModelError(f"status must be one of {sorted(MODEL_STATUSES)}")
    document["status"] = status

    source = _dict(document.get("source"))
    source_type = _identifier(source.get("type"), "source.type")
    source_reference = str(source.get("reference") or "").strip()
    if not source_reference:
        raise NetworkModelError("source.reference is required")
    source["type"] = source_type
    source["reference"] = source_reference
    document["source"] = source

    coverage = {
        _identifier(domain, "coverage.domains")
        for domain in _list(_dict(document.get("coverage")).get("domains"))
    }
    if not coverage:
        raise NetworkModelError("coverage.domains must declare what this revision covers")
    document["coverage"] = {"domains": sorted(coverage)}
    document["authority_bindings"] = validate_authority_bindings(document.get("authority_bindings"), coverage)

    model = _dict(document.get("model"))
    if not model:
        raise NetworkModelError("model must contain at least one modeled fact")
    document["model"] = model

    if status in APPROVED_STATUSES:
        if source_type in OBSERVATION_ONLY_SOURCES:
            raise NetworkModelError(f"{source_type} is observation-only and cannot authorize approved intent")
        non_authoritative = sorted(
            domain
            for domain in coverage
            if document["authority_bindings"][domain]["mode"] != "authoritative"
        )
        if non_authoritative:
            raise NetworkModelError(
                "approved revisions require authoritative bindings for covered domains: "
                + ", ".join(non_authoritative)
            )
        approval = _dict(document.get("approval"))
        if str(approval.get("status") or "").strip().lower() != "approved":
            raise NetworkModelError("approved revisions require approval.status=approved")
        if not str(approval.get("approved_by") or "").strip():
            raise NetworkModelError("approved revisions require approval.approved_by")
        if not str(approval.get("approved_at") or "").strip():
            raise NetworkModelError("approved revisions require approval.approved_at")
        document["approval"] = approval
    elif _dict(document.get("approval")).get("status") == "approved":
        raise NetworkModelError("approval cannot be attached before a revision reaches approved state")

    _assert_no_secrets(document)
    return document


def prepare_reviewed_approval(
    value: Mapping[str, Any],
    *,
    approved_by: str,
    approved_at: str,
) -> dict[str, Any]:
    """Build the exact approved document produced by a human review action.

    Discovery and telemetry remain observation-only.  Approving a proposal does
    not pretend those collectors became authoritative; it records that a human
    reviewed their proposal and accepted it as manual intent while retaining the
    original source reference in the audit string.
    """
    revision = copy.deepcopy(_dict(value))
    original_source = _dict(revision.get("source"))
    original_type = str(original_source.get("type") or "").strip().lower()
    original_reference = str(original_source.get("reference") or "").strip()
    if original_type in OBSERVATION_ONLY_SOURCES:
        revision["source"] = {
            "type": "manual_review",
            "reference": f"reviewed:{original_type}:{original_reference}",
        }
    revision["status"] = "approved"
    revision["approval"] = {
        "status": "approved",
        "approved_by": str(approved_by or "").strip(),
        "approved_at": str(approved_at or "").strip(),
    }
    revision["authority_bindings"] = {
        domain: {
            **binding,
            "source": (
                "manual_review"
                if str(binding.get("source") or "").strip().lower() in OBSERVATION_ONLY_SOURCES
                else str(binding.get("source") or "").strip().lower()
            ),
            "mode": "authoritative",
        }
        for domain, binding in _dict(revision.get("authority_bindings")).items()
    }
    return validate_model_revision(revision)


def validate_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an append-only observation that can never carry model approval."""
    observation = copy.deepcopy(_dict(value))
    if observation.get("schema") != NETWORK_OBSERVATION_SCHEMA:
        raise NetworkModelError(f"schema must be {NETWORK_OBSERVATION_SCHEMA}")
    observation["org_id"] = _identifier(observation.get("org_id"), "org_id")
    observation["environment_id"] = _identifier(observation.get("environment_id"), "environment_id")
    observation["observation_id"] = _identifier(observation.get("observation_id"), "observation_id")
    observation["domain"] = _identifier(observation.get("domain"), "domain")
    observation["source"] = _identifier(observation.get("source"), "source")
    observation["subject_id"] = str(observation.get("subject_id") or "").strip()
    if observation.get("status") in APPROVED_STATUSES or observation.get("approval"):
        raise NetworkModelError("observations cannot carry model approval or an approved lifecycle state")
    observation["observed_at"] = _timestamp(observation.get("observed_at"), "observed_at")
    if observation.get("expires_at"):
        observation["expires_at"] = _timestamp(observation.get("expires_at"), "expires_at")
        if observation["expires_at"] <= observation["observed_at"]:
            raise NetworkModelError("expires_at must be later than observed_at")
    if not str(observation.get("collector_id") or "").strip():
        raise NetworkModelError("collector_id is required")
    if not _dict(observation.get("facts")):
        raise NetworkModelError("facts must contain at least one normalized observation")
    observation["validation_grade"] = str(observation.get("validation_grade") or "unknown").strip().lower()
    observation["metadata"] = _dict(observation.get("metadata"))
    _assert_no_secrets(observation)
    return observation
