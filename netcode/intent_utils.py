"""Desired-state helpers — thin delegators over the change-type registry.

All type-specific behavior lives in netcode/change_types.py. These functions keep
their historical names/signatures so existing callers (api, lab, rendering,
orchestrator) don't change, but each is now a one-line registry lookup instead of
an isinstance ladder.
"""

from __future__ import annotations

from typing import Any

from netcode.change_types import REGISTRY, safe_name, spec_for
from netcode.models import Intent

CHANGE_TYPE_LABELS = {key: spec.label for key, spec in REGISTRY.items()}

__all__ = [
    "CHANGE_TYPE_LABELS", "safe_name", "intent_title", "intent_slug", "template_for_intent",
    "config_filename", "report_stem", "target_device_id", "intent_risk", "lab_write_supported",
    "production_write_supported", "rollback_config", "verification_hint", "rollback_confidence",
    "blast_radius", "pre_post_checks", "suggested_branch", "plan_metadata",
]


def intent_title(intent: Intent) -> str:
    return spec_for(intent).title(intent)


def intent_slug(intent: Intent) -> str:
    return spec_for(intent).slug(intent)


def template_for_intent(intent: Intent) -> str:
    return spec_for(intent).template


def config_filename(intent: Intent) -> str:
    return f"{report_stem(intent)}.eos"


def report_stem(intent: Intent) -> str:
    base = intent_slug(intent)
    instance_id = getattr(intent.metadata, "change_instance_id", None)
    return f"{base}-{safe_name(instance_id)}" if instance_id else base


def target_device_id(intent: Intent) -> str | None:
    return intent.targets.device_ids[0] if intent.targets.device_ids else None


def intent_risk(intent: Intent) -> str:
    return spec_for(intent).risk


def lab_write_supported(intent: Intent) -> bool:
    return spec_for(intent).lab_write


def production_write_supported(intent: Intent) -> bool:
    return spec_for(intent).production_write


def rollback_config(intent: Intent) -> str:
    return spec_for(intent).rollback(intent)


def rollback_confidence(intent: Intent) -> dict[str, str]:
    return spec_for(intent).rollback_confidence(intent)


def verification_hint(intent: Intent) -> dict[str, Any]:
    return spec_for(intent).verification_hint(intent)


def pre_post_checks(intent: Intent) -> dict[str, list[dict[str, Any]]]:
    return spec_for(intent).checks(intent)


def suggested_branch(intent: Intent) -> str:
    return f"change/{intent_slug(intent)}"


def blast_radius(intent: Intent) -> dict[str, Any]:
    devices = list(intent.targets.device_ids or [])
    if not devices and intent.targets.device_group:
        devices = [f"group:{intent.targets.device_group}"]
    return {
        "devices": devices,
        "device_count": len(devices),
        "objects": spec_for(intent).blast_objects(intent),
        "site": intent.site,
    }


def plan_metadata(intent: Intent) -> dict[str, Any]:
    spec = spec_for(intent)
    return {
        "change_type": intent.change_type,
        "label": spec.label,
        "title": spec.title(intent),
        "slug": report_stem(intent),
        "risk": spec.risk,
        "target_device_id": target_device_id(intent),
        "lab_write_supported": spec.lab_write,
        "production_write_supported": spec.production_write,
        "verification": spec.verification_hint(intent),
        "blast_radius": blast_radius(intent),
        "rollback": {"commands": spec.rollback(intent), "confidence": spec.rollback_confidence(intent)},
        "checks": spec.checks(intent),
        "suggested_branch": suggested_branch(intent),
    }
