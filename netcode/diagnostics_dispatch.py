"""Authenticated delivery of failed verification evidence to Rez Diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
import re
from typing import Any


_ORG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENVIRONMENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")


def dispatch_verification_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    """Start Rez asynchronously and return the delivery acknowledgement.

    Verification itself must never depend on Rez availability, so missing
    deployment configuration disables delivery and network errors are recorded
    on the change rather than raised into the rollout path.
    """
    base_url = os.environ.get("NETCODE_REZ_TRIGGER_URL", "").strip().rstrip("/")
    token = os.environ.get("NETCODE_REZ_TRIGGER_TOKEN", "").strip()
    if not base_url or not token:
        return {
            "status": "disabled",
            "reason": "NETCODE_REZ_TRIGGER_URL and NETCODE_REZ_TRIGGER_TOKEN are required",
        }
    context = handoff.get("context") if isinstance(handoff.get("context"), dict) else {}
    organization = str(context.get("org_id") or "").strip()
    environment = str(context.get("environment_id") or "").strip()
    if not _ORG_ID.fullmatch(organization) or not _ENVIRONMENT_ID.fullmatch(environment):
        return {
            "status": "disabled",
            "reason": "A persisted change organization and environment binding are required",
        }

    dispatch_id = hashlib.sha256(
        json.dumps(handoff, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    payload = {
        "dispatch_id": dispatch_id,
        "organization_binding": organization,
        "environment_binding": environment,
        "handoff": handoff,
    }
    request = urllib.request.Request(
        f"{base_url}/api/integrations/netcode/verification-failure",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Rez-Integration-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - operator-configured Rez URL.
            body = json.loads(response.read().decode("utf-8") or "{}")
            return {
                "status": "accepted" if body.get("ok") else "rejected",
                "dispatch_id": dispatch_id,
                "investigation_id": body.get("investigation_id"),
                "response": body,
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "dispatch_id": dispatch_id,
            "error": f"rez_http_{exc.code}",
            "detail": detail[:1000],
        }
    except Exception as exc:  # network availability must not break verification
        return {
            "status": "failed",
            "dispatch_id": dispatch_id,
            "error": f"rez_unavailable:{exc}",
        }
