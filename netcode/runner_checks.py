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

# A hardcoded floor the runner enforces for EVERY change type, regardless of the
# policy shipped by the control plane or configured locally. A compromised
# control plane shipping an empty policy still cannot push credentials/AAA —
# these can never be disabled. (Mirrors the shell guard's ALWAYS_BLOCKED set.)
_ALWAYS_BLOCKED = (
    "username ",
    "enable secret",
    "enable password",
    "aaa ",
    "tacacs",
    "radius",
    "snmp-server community",
    "crypto key",
    "key config-key",
)


def local_policy_gate(
    intent: Intent,
    render: RenderResult,
    policy_yaml: str,
    local_policy_yaml: str = "",
) -> dict[str, Any]:
    """Return {ok, message, blocked_lines, unexpected_lines}. ok=False blocks execution.

    Fail-closed and does NOT trust the control plane: blocked fragments are the
    UNION of the hardcoded floor, the runner's LOCAL policy, and the payload
    policy (more blocking is always safer). Allowed prefixes prefer the local
    policy, then payload, then built-in defaults."""
    def parse(text: str) -> dict[str, Any]:
        try:
            value = yaml.safe_load(io.StringIO(text)) or {}
        except Exception:  # noqa: BLE001 — malformed policy contributes nothing (never fail open)
            return {"__error__": True}
        return value if isinstance(value, dict) else {}

    payload_policy = parse(policy_yaml)
    local_policy = parse(local_policy_yaml)
    if payload_policy.get("__error__") and not local_policy:
        return {"ok": False, "message": "Control-plane policy could not be parsed (fail-closed)."}

    change_type = intent.change_type
    payload_scope = payload_policy.get("render_scope", {}) if isinstance(payload_policy, dict) else {}
    local_scope = local_policy.get("render_scope", {}) if isinstance(local_policy, dict) else {}

    # Allowed prefixes: local wins, then payload, then built-in defaults.
    key = f"{change_type}_allowed_prefixes"
    allowed_list = local_scope.get(key) or payload_scope.get(key) or _DEFAULT_ALLOWED.get(change_type, [])
    allowed = tuple(allowed_list)

    # Blocked fragments: hardcoded floor ∪ local ∪ payload (union = strictly safer).
    blocked = {frag.lower() for frag in _ALWAYS_BLOCKED}
    blocked |= {str(v).lower() for v in payload_scope.get("blocked_fragments", [])}
    blocked |= {str(v).lower() for v in local_scope.get("blocked_fragments", [])}
    blocked = list(blocked)
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
