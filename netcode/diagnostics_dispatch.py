"""Authenticated delivery of failed verification evidence to Rez Diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any


def dispatch_verification_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    """Start Rez asynchronously and return the delivery acknowledgement.

    Verification itself must never depend on Rez availability, so missing
    deployment configuration disables delivery and network errors are recorded
    on the change rather than raised into the rollout path.
    """
    base_url = os.environ.get("NETCODE_REZ_TRIGGER_URL", "").strip().rstrip("/")
    token = os.environ.get("NETCODE_REZ_TRIGGER_TOKEN", "").strip()
    environment = os.environ.get("NETCODE_REZ_ENVIRONMENT_ID", "").strip()
    if not base_url or not token or not environment:
        return {
            "status": "disabled",
            "reason": "NETCODE_REZ_TRIGGER_URL, NETCODE_REZ_TRIGGER_TOKEN, and NETCODE_REZ_ENVIRONMENT_ID are required",
        }

    dispatch_id = hashlib.sha256(
        json.dumps(handoff, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    payload = {
        "dispatch_id": dispatch_id,
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
