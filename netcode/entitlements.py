"""Server-side platform entitlement checks for the Netcode control plane.

Rez is the license authority. Netcode receives only public plan limits through
an authenticated service-to-service endpoint; no device credentials or device
state are sent to the licensing path.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class EntitlementError(RuntimeError):
    pass


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class PlatformEntitlements:
    plan_id: str
    platform_available: bool
    max_devices: int
    max_connectors: int
    max_workflow_packs: int
    production_writes: bool
    source: str
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "platform_available": self.platform_available,
            "max_devices": self.max_devices,
            "max_connectors": self.max_connectors,
            "max_workflow_packs": self.max_workflow_packs,
            "netcode_production_writes": self.production_writes,
            "source": self.source,
            "stale": self.stale,
        }


_LOCK = threading.Lock()
_CACHE: tuple[float, PlatformEntitlements] | None = None


def enforcement_enabled() -> bool:
    return _truthy("NETCODE_LICENSE_ENFORCEMENT")


def _development_entitlements() -> PlatformEntitlements:
    return PlatformEntitlements(
        plan_id="development",
        platform_available=True,
        max_devices=100_000,
        max_connectors=1_000,
        max_workflow_packs=1_000,
        production_writes=True,
        source="development_bypass",
    )


def _parse(payload: dict[str, Any], *, stale: bool = False) -> PlatformEntitlements:
    values = payload.get("entitlements") if isinstance(payload.get("entitlements"), dict) else {}
    result = PlatformEntitlements(
        plan_id=str(payload.get("plan_id") or values.get("plan_id") or "unknown"),
        platform_available=bool(payload.get("platform_available", False)),
        max_devices=max(0, int(values.get("max_devices", 0) or 0)),
        max_connectors=max(0, int(values.get("max_connectors", 0) or 0)),
        max_workflow_packs=max(0, int(values.get("max_workflow_packs", 0) or 0)),
        production_writes=bool(values.get("netcode_production_writes", False)),
        source="rez_license_authority",
        stale=stale,
    )
    if not result.platform_available:
        raise EntitlementError("The platform license is not active.")
    return result


def get_entitlements(*, force: bool = False) -> PlatformEntitlements:
    global _CACHE
    if not enforcement_enabled():
        return _development_entitlements()

    url = os.environ.get("NETCODE_ENTITLEMENT_URL", "").strip()
    token = os.environ.get("NETCODE_ENTITLEMENT_TOKEN", "").strip()
    if not url or not token:
        raise EntitlementError("Netcode entitlement enforcement is enabled but the authority is not configured.")

    now = time.monotonic()
    ttl = max(5, int(os.environ.get("NETCODE_ENTITLEMENT_CACHE_SECONDS", "60") or 60))
    stale_grace = max(ttl, int(os.environ.get("NETCODE_ENTITLEMENT_STALE_SECONDS", "300") or 300))
    with _LOCK:
        cached = _CACHE
    if not force and cached and now - cached[0] <= ttl:
        return cached[1]

    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - operator-configured internal URL.
            payload = json.loads(response.read().decode("utf-8"))
        result = _parse(payload)
        with _LOCK:
            _CACHE = (now, result)
        return result
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        if cached and now - cached[0] <= stale_grace:
            value = cached[1]
            return PlatformEntitlements(**{**value.__dict__, "stale": True})
        raise EntitlementError("The entitlement authority is unavailable; protected operations fail closed.") from exc


def require_production_writes() -> PlatformEntitlements:
    entitlements = get_entitlements()
    if not entitlements.production_writes:
        raise EntitlementError(
            f"Production writes are not included in the {entitlements.plan_id} plan. Planning, dry-run, and verification remain available."
        )
    return entitlements


def job_requires_production_writes(action: str) -> bool:
    normalized = str(action or "").strip().lower()
    if normalized in {"arista_full_run", "lab_apply", "lab_rollback", "ansible_apply", "ansible_canary", "ansible_rollback"}:
        return True
    if normalized.startswith("manager_"):
        return normalized.removeprefix("manager_") in {"lock", "stage", "deploy", "discard", "unlock", "rollback"}
    return False


def enforce_capacity(resource: str, *, current: int, additional: int = 1) -> PlatformEntitlements:
    entitlements = get_entitlements()
    limits = {
        "devices": entitlements.max_devices,
        "connectors": entitlements.max_connectors,
        "workflow_packs": entitlements.max_workflow_packs,
    }
    if resource not in limits:
        raise ValueError(f"Unknown entitlement resource: {resource}")
    limit = limits[resource]
    if additional > 0 and current + additional > limit:
        raise EntitlementError(
            f"The {entitlements.plan_id} plan allows {limit} {resource}; this operation would use {current + additional}."
        )
    return entitlements


def reset_cache_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None
