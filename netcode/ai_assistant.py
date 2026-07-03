"""Guarded AI-assistant layer.

This first implementation is deterministic and offline. It provides the product
contract for an AI layer without letting natural language bypass source of truth,
policy validation, dry-run, approval, or evidence gates.
"""

from __future__ import annotations

import re
from typing import Any


def assistant_response(prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    lowered = prompt.lower()
    if any(word in lowered for word in ("apply", "commit", "push", "deploy", "execute")):
        mode = "guardrail"
        answer = "I can explain or draft structured intent, but I cannot execute network changes. Run the deterministic workflow gates."
    elif "risk" in lowered or "safe" in lowered:
        mode = "risk_summary"
        answer = _risk_summary(context)
    elif "intent" in lowered or "vlan" in lowered:
        mode = "proposed_intent"
        answer = "I drafted a proposed structured intent. It still must pass source-of-truth, policy, rendering, dry-run, and verification gates."
    else:
        mode = "explanation"
        answer = _explain_platform(context)
    return {
        "ok": True,
        "mode": mode,
        "answer": answer,
        "proposed_intent": _propose_vlan_intent(prompt),
        "guardrails": [
            "AI cannot apply changes.",
            "AI output must become structured intent.",
            "Deterministic validators enforce policy.",
            "Lab dry-run proof is required before apply.",
            "Human approval can be inserted before production apply.",
        ],
    }


def _propose_vlan_intent(prompt: str) -> dict[str, Any] | None:
    vlan = re.search(r"\bvlan\s+(\d{1,4})\b", prompt, flags=re.IGNORECASE)
    subnet = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b", prompt)
    name = re.search(r"\bname\s+([A-Za-z0-9_-]{2,32})\b", prompt, flags=re.IGNORECASE)
    device = re.search(r"\b(?:device|switch|target)\s+([A-Za-z0-9_.-]+)\b", prompt, flags=re.IGNORECASE)
    site = re.search(r"\bsite\s+([A-Za-z0-9_.-]+)\b", prompt, flags=re.IGNORECASE)
    if not vlan:
        return None
    return {
        "change_type": "add_vlan",
        "site": site.group(1) if site else "store-1842",
        "device_id": device.group(1) if device else "v2-store1",
        "vlan_id": int(vlan.group(1)),
        "name": name.group(1).upper() if name else "NEW_VLAN",
        "subnet": subnet.group(1) if subnet else "10.42.90.0/24",
        "purpose": "guest" if "guest" in prompt.lower() else "general",
        "requires_validation": True,
    }


def _risk_summary(context: dict[str, Any]) -> str:
    workflow = context.get("workflow") or {}
    state = workflow.get("state", "unknown") if isinstance(workflow, dict) else "unknown"
    if state in {"draft", "intent_created", "rendered", "validated"}:
        return "Risk is controlled only up to static validation. Lab dry-run proof is still required before apply."
    if state == "dry_run_passed":
        return "Risk is reduced because EOS accepted the candidate in an abortable session. Apply still requires controlled execution and verification."
    if state == "rollback_available":
        return "Change was applied and verified. Rollback evidence is available if service impact is detected."
    return "Risk summary depends on current workflow state, source-of-truth evidence, validation checks, and lab proof."


def _explain_platform(context: dict[str, Any]) -> str:
    return (
        "Netcode turns a requested network outcome into structured intent, renders candidate config, "
        "runs deterministic policy checks, proves the candidate in lab, shows exact device commands, "
        "verifies live state, and records evidence. Rez supplies multi-vendor read/state drivers."
    )
