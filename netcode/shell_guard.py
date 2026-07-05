"""Shell Guard — the command-inspection brain of Netcode Shell.

Every byte an engineer types in a governed SSH session flows through this
module ON THE RUNNER (the trust boundary), never only in the browser.

Interactive model (how real jump-host command filters work):
- Non-Enter keystrokes are forwarded immediately, so the device's remote echo
  behaves normally while we mirror them into a line buffer.
- Enter is the gate. When CR arrives we classify the buffered line FIRST:
  allowed -> the CR is forwarded and the device executes; blocked -> the CR is
  swallowed and a kill-line (Ctrl-U) is sent instead, so the device's line
  buffer is wiped and NOTHING executes.
- Multi-line pastes are intercepted whole, before any byte is forwarded.

Fail-closed philosophy:
- Sessions start read-only; entering configuration mode is blocked until a
  change record is attached.
- Credential/AAA lines are blocked unconditionally (same invariant as the
  custom_config policy): the shell is never a credential push path.
- Dangerous lines (reload, write erase, ...) need an explicit re-Enter
  confirmation even with a change attached.
- History recall / line editing we cannot verify (escape sequences, cursor
  tricks) TAINTS the buffer: the Enter is swallowed and the engineer is asked
  to retype. Annoying is acceptable; fail-open is not.
- `device_touched` flips only when a line is actually forwarded while in
  configuration mode — the evidence attestation is earned, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KILL_LINE = "\x15"  # Ctrl-U: wipe the device-side line buffer

_CONFIG_ENTER_TOKENS = {
    # EOS/IOS accept unique prefixes; four chars of "configure" is unambiguous
    # there, and we fail closed on every prefix that could resolve to it.
    "configure", "configur", "configu", "config", "confi", "conf",
    "edit",  # junos
}

ALWAYS_BLOCKED_FRAGMENTS = (
    "username ",
    "enable secret",
    "enable password",
    "aaa ",
    "tacacs",
    "radius",
    "snmp-server community",
    "crypto key",
)

DANGEROUS_PREFIXES = (
    "reload",
    "write erase",
    "erase ",
    "format ",
    "delete ",
    "boot system",
    "no vlan",
    "no interface",
    "no router",
    "shutdown",
)

# Control bytes that keep the buffer trustworthy. Everything else (escape
# sequences, Ctrl-P/N history, Ctrl-W word kill, ...) taints it.
_SAFE_CONTROL = {"\r", "\n", "\t", "\x7f", "\x08", "\x03", "\x15"}


@dataclass
class ShellSessionState:
    """Guard-relevant state for one governed session."""

    mode: str = "read_only"  # read_only | change_attached
    change_id: str | None = None
    in_config: bool = False
    device_touched: bool = False
    pending_confirm: str | None = None
    line_buffer: str = ""
    tainted: bool = False  # buffer no longer provably matches the device line

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "change_id": self.change_id,
            "in_config": self.in_config,
            "device_touched": self.device_touched,
            "pending_confirm": self.pending_confirm,
        }


@dataclass
class GuardDecision:
    forward: str  # exact bytes to send to the device ("" = nothing)
    events: list[dict[str, Any]] = field(default_factory=list)


def _first_token(line: str) -> str:
    stripped = line.replace("\t", " ").strip().lower()
    return stripped.split(" ", 1)[0] if stripped else ""


def classify_line(line: str, in_config: bool) -> dict[str, Any]:
    """Classify one completed command line. Pure function; heavily tested."""
    normalized = line.replace("\t", " ").strip()
    lowered = normalized.lower()
    if not lowered:
        return {"kind": "empty"}
    for fragment in ALWAYS_BLOCKED_FRAGMENTS:
        if fragment in lowered + " ":
            return {"kind": "always_blocked", "fragment": fragment.strip()}
    token = _first_token(lowered)
    if token in _CONFIG_ENTER_TOKENS:
        return {"kind": "config_enter"}
    if in_config and lowered in ("end", "exit", "abort"):
        return {"kind": "config_exit"}
    for prefix in DANGEROUS_PREFIXES:
        config_only = prefix in ("no vlan", "no interface", "no router", "shutdown")
        if lowered.startswith(prefix) and (in_config or not config_only):
            return {"kind": "dangerous", "match": prefix}
    if in_config:
        return {"kind": "config_line"}
    return {"kind": "read"}


def looks_like_paste(chunk: str) -> bool:
    return chunk.count("\n") + chunk.count("\r") >= 2


# First tokens that are provably read-only; a pasted line with any OTHER
# token makes the whole paste config-ish (fail closed: unknown = intercept).
_PASTE_READ_TOKENS = {
    "show", "sh", "sho", "ping", "traceroute", "dir", "more", "enable", "en",
    "terminal", "exit", "quit", "bash", "watch",
}


def _paste_decision(state: ShellSessionState, chunk: str) -> GuardDecision | None:
    """Intercept multi-line pastes containing configuration content, before
    ANY byte reaches the device. Fail closed: a paste passes only when every
    line is provably read-only."""
    if not looks_like_paste(chunk):
        return None
    lines = [l for l in chunk.replace("\r", "\n").split("\n") if l.strip()]
    configish = False
    for line in lines:
        verdict = classify_line(line, state.in_config)
        if verdict["kind"] in ("config_enter", "config_line", "always_blocked", "dangerous"):
            configish = True
            break
        if line[:1].isspace():  # indented lines are config-block style
            configish = True
            break
        if _first_token(line) not in _PASTE_READ_TOKENS:
            configish = True
            break
    if not configish:
        return None  # multi-line show-command pastes pass through untouched
    return GuardDecision(forward="", events=[{
        "type": "guard",
        "action": "paste_intercepted",
        "line_count": len(lines),
        "lines": lines[:200],
        "message": (
            f"You pasted {len(lines)} configuration lines. Stage them as a "
            "governed change before sending to the device?"
        ),
        "options": ["stage_as_change", "cancel"],
    }])


def _enter_decision(state: ShellSessionState, enter_byte: str) -> GuardDecision:
    """The gate: decide the buffered line when Enter arrives."""
    line = state.line_buffer
    tainted = state.tainted
    state.line_buffer = ""
    state.tainted = False

    if tainted:
        return GuardDecision(forward=KILL_LINE, events=[{
            "type": "guard", "action": "blocked_unverifiable_line",
            "message": (
                "That line used history recall or editing the guard can't "
                "verify. Please retype the command. Nothing was sent."
            ),
        }])

    verdict = classify_line(line, state.in_config)
    kind = verdict["kind"]

    if kind == "always_blocked":
        return GuardDecision(forward=KILL_LINE, events=[{
            "type": "guard", "action": "blocked_credential_line",
            "fragment": verdict["fragment"],
            "message": (
                "Blocked: credential/AAA changes are never allowed through the "
                "shell (fail-closed invariant). No device config was touched."
            ),
        }])

    if kind == "config_enter":
        if state.mode != "change_attached":
            return GuardDecision(forward=KILL_LINE, events=[{
                "type": "guard", "action": "blocked_config_mode",
                "message": (
                    "Config mode is guarded. Attach a change record before this "
                    "session can change device configuration. No device config "
                    "was touched."
                ),
                "options": ["attach_change", "create_change", "cancel"],
            }])
        state.in_config = True
        state.device_touched = True
        return GuardDecision(forward=enter_byte, events=[{
            "type": "guard", "action": "config_mode_entered",
            "change_id": state.change_id,
            "message": f"Config mode entered under change {state.change_id}.",
        }])

    if kind == "dangerous":
        if state.pending_confirm == line:
            state.pending_confirm = None
            if state.in_config:
                state.device_touched = True
            return GuardDecision(forward=enter_byte, events=[{
                "type": "guard", "action": "dangerous_confirmed",
                "match": verdict["match"],
            }])
        state.pending_confirm = line
        # Keep the buffer so the SECOND Enter confirms the same line.
        state.line_buffer = line
        return GuardDecision(forward="", events=[{
            "type": "guard", "action": "dangerous_needs_confirm",
            "match": verdict["match"],
            "message": (
                f"'{line.strip()}' is classified dangerous ({verdict['match']}). "
                "Press Enter again to send it, or Ctrl-C to cancel. Nothing was "
                "sent to the device yet."
            ),
        }])

    if kind == "config_exit":
        state.in_config = False
        return GuardDecision(forward=enter_byte, events=[{
            "type": "guard", "action": "config_mode_exited",
        }])

    if kind == "config_line":
        state.device_touched = True
        state.pending_confirm = None
        return GuardDecision(forward=enter_byte, events=[])

    state.pending_confirm = None
    return GuardDecision(forward=enter_byte, events=[])


def submit_line(state: ShellSessionState, line: str) -> dict[str, Any]:
    """Line-oriented gate for the REST Shell (MVP1/2). Runs the SAME tested
    decision as the streaming path. Returns whether the line is cleared to
    reach the device and the guard events to surface."""
    state.line_buffer = line.rstrip("\r\n")
    state.tainted = False
    decision = _enter_decision(state, "\r")
    cleared = decision.forward.endswith("\r") and KILL_LINE not in decision.forward
    verdict = classify_line(line, state.in_config)
    return {
        "cleared": cleared,
        "line": line,
        "kind": verdict["kind"],
        "events": decision.events,
        "state": state.as_dict(),
    }


def submit_paste(state: ShellSessionState, text: str) -> dict[str, Any] | None:
    """Paste-oriented gate. Returns a paste-interception result (config-ish
    paste that must be staged), or None when the paste is plain reads that may
    run line-by-line."""
    decision = _paste_decision(state, text if "\n" in text or "\r" in text else text + "\n")
    if decision is None:
        return None
    return {"cleared": False, "events": decision.events, "state": state.as_dict()}


def feed(state: ShellSessionState, chunk: str) -> GuardDecision:
    """Feed raw engineer input; returns exactly what may reach the device."""
    paste = _paste_decision(state, chunk)
    if paste is not None:
        state.line_buffer = ""
        state.tainted = False
        return paste

    forward: list[str] = []
    events: list[dict[str, Any]] = []
    for ch in chunk:
        if ch == "\x03":  # Ctrl-C: clears line + any pending confirmation
            state.line_buffer = ""
            state.tainted = False
            state.pending_confirm = None
            forward.append(ch)
            continue
        if ch in ("\x7f", "\x08"):
            state.line_buffer = state.line_buffer[:-1]
            forward.append(ch)
            continue
        if ch in ("\r", "\n"):
            decision = _enter_decision(state, ch)
            forward.append(decision.forward)
            events.extend(decision.events)
            continue
        if ch < " " or ch == "\x1b":
            if ch not in _SAFE_CONTROL:
                state.tainted = True  # history/edit sequence we can't model
            forward.append(ch)
            continue
        state.line_buffer += ch
        forward.append(ch)
    return GuardDecision(forward="".join(forward), events=events)
