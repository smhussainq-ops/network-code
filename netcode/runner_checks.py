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

from netcode.change_types import redistribution_items, spec_for
from netcode.models import Intent, RenderResult
from netcode.validation import StaticValidator

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


def _typed_redistribution_lines(intent: Intent) -> set[str]:
    if intent.change_type != "routing_redistribution":
        return set()
    expected: set[str] = set()
    for item in redistribution_items(intent):
        for index, prefix in enumerate(item.prefixes, start=1):
            expected.add(
                f"ip prefix-list {item.prefix_list} seq {index * 10} permit {prefix} le 32".lower()
            )
        expected.add(f"route-map {item.route_map} permit 10".lower())
        expected.add(f"match ip address prefix-list {item.prefix_list}".lower())
        if item.to_protocol == "ospf":
            expected.add(f"set tag {item.route_tag}".lower())
            expected.add(f"router ospf {item.target_process}".lower())
            expected.add(f"redistribute {item.from_protocol} route-map {item.route_map}".lower())
        else:
            expected.add(f"router bgp {item.target_process}".lower())
            expected.add("address-family ipv4")
            expected.add(f"redistribute {item.from_protocol} route-map {item.route_map}".lower())
    return expected


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

    # The signed intent is parsed locally before this function is called. Resolve
    # its registry contract here as the runner's built-in source of truth so new
    # typed change types cannot silently fall back to an obsolete generic policy.
    spec = spec_for(intent)

    # Allowed prefixes: local wins, then payload, then the typed registry contract.
    key = f"{change_type}_allowed_prefixes"
    allowed_list = local_scope.get(key) or payload_scope.get(key) or spec.allow_prefixes
    allowed = tuple(allowed_list)

    # Blocked fragments: hardcoded floor ∪ local ∪ payload (union = strictly safer).
    immutable_blocked = {frag.lower() for frag in _ALWAYS_BLOCKED}
    blocked = set(immutable_blocked)
    blocked |= {str(v).lower() for v in payload_scope.get("blocked_fragments", [])}
    blocked |= {str(v).lower() for v in local_scope.get("blocked_fragments", [])}
    blocked = list(blocked)
    # Apply the exact typed carve-outs used by the control-plane validator. The
    # immutable credential/AAA floor intentionally has no registry carve-outs.
    carveouts = {
        fragment.lower()
        for fragment in spec.block_carveouts
        if fragment.lower() not in immutable_blocked
    }
    blocked = [fragment for fragment in blocked if fragment not in carveouts]

    blocked_lines: list[str] = []
    unexpected_lines: list[str] = []
    exact_redistribution_lines = _typed_redistribution_lines(intent)
    for line in render.config.splitlines():
        if not line.strip():
            continue
        # Collapse whitespace runs the way device CLI parsers tokenize, so
        # 'enable  secret' / 'username\tadmin' can't dodge the single-space
        # floor fragments. (Red-team confirmed the raw-substring match was
        # trivially bypassable.)
        lower = " ".join(line.split()).lower()
        if any(fragment in lower for fragment in blocked):
            blocked_lines.append(line)
        if allowed and not line.startswith(allowed):
            unexpected_lines.append(line)
        if exact_redistribution_lines and lower not in exact_redistribution_lines:
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
