"""Runner-side fail-closed policy re-check.

This is the second safety gate. The control plane already validated the intent,
but the runner does NOT trust the control plane: before touching a device it
re-runs the render-scope guard (allow-list + blocked fragments) against the
config it is about to push, using policy shipped in the job. A compromised or
buggy control plane therefore still cannot make the runner push forbidden config
(credentials, management, out-of-scope features).

Kept dependency-light and independent of the full StaticValidator so it can be
audited on its own.
"""

from __future__ import annotations

import io
from typing import Any

import yaml

from netcode.models import Intent, RenderResult
from netcode.validation import StaticValidator


# Mirror of validation.StaticValidator._render_scope default allow-lists, kept here
# so the runner gate is self-contained and auditable.
_DEFAULT_ALLOWED = {
    "add_vlan": ["vlan ", "   name ", "interface Vlan", "   description ", "   ip address "],
    "interface_config": ["interface ", "   description ", "   switchport ", "   no switchport", "   ip address ", "   shutdown", "   no shutdown"],
    "bgp_neighbor": ["router bgp ", "   router-id ", "   neighbor ", "   no neighbor "],
    "acl_rule": ["ip access-list ", "   remark ", "   permit ", "   deny "],
    "site_device_intent": ["! "],
    "custom_config": [],
}


def local_policy_gate(intent: Intent, render: RenderResult, policy_yaml: str) -> dict[str, Any]:
    """Return {ok, message, blocked_lines, unexpected_lines}. ok=False blocks execution."""
    try:
        policy = yaml.safe_load(io.StringIO(policy_yaml)) or {}
    except Exception as exc:  # noqa: BLE001 — malformed policy must fail closed
        return {"ok": False, "message": f"Local policy could not be parsed (fail-closed): {exc}"}

    scope = policy.get("render_scope", {}) if isinstance(policy, dict) else {}
    change_type = intent.change_type
    allowed = tuple(scope.get(f"{change_type}_allowed_prefixes", _DEFAULT_ALLOWED.get(change_type, [])))
    blocked = [str(v).lower() for v in scope.get("blocked_fragments", [])]
    # Same per-change-type carve-outs the control plane uses, applied locally.
    if change_type == "bgp_neighbor":
        blocked = [fragment for fragment in blocked if fragment != "router bgp"]
    if change_type == "acl_rule":
        blocked = [fragment for fragment in blocked if fragment != "ip access-list"]

    blocked_lines: list[str] = []
    unexpected_lines: list[str] = []
    for line in render.config.splitlines():
        if not line.strip():
            continue
        lower = line.lower()
        if any(fragment in lower for fragment in blocked):
            blocked_lines.append(line)
        if allowed and not line.startswith(allowed):
            unexpected_lines.append(line)

    if blocked_lines:
        return {
            "ok": False,
            "message": f"Config contains blocked fragments: {blocked_lines[:3]}",
            "blocked_lines": blocked_lines,
            "unexpected_lines": unexpected_lines,
        }
    if unexpected_lines:
        return {
            "ok": False,
            "message": f"Config has {len(unexpected_lines)} line(s) outside the allowed {change_type} scope.",
            "blocked_lines": blocked_lines,
            "unexpected_lines": unexpected_lines,
        }
    return {"ok": True, "message": "Local policy gate passed.", "blocked_lines": [], "unexpected_lines": []}


def full_local_revalidation(paths, intent: Intent, render: RenderResult):
    """Optional stronger gate: run the whole StaticValidator locally when the runner
    has the workspace (inventory/policy) available. Returns a ValidationReport."""
    return StaticValidator(paths).validate(intent, render)
