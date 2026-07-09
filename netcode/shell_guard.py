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
- The human shell is a real SSH terminal: configuration mode is allowed without
  an automation change record. Netcode records the session passively; unattended
  automation remains governed elsewhere.
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

    mode: str = "direct"  # direct | guarded | change_attached
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


# Philosophy: this is a real SSH terminal for good-faith engineers, so it is
# ALLOW-BY-DEFAULT — any command flows unless it's one of three things:
#   1. a genuinely destructive/disruptive exec command (needs a confirm),
#   2. a credential/AAA line (always blocked),
#   3. command chaining/control characters that make the audited line ambiguous.
# Everything else — show/enable/en/terminal/ping/traceroute/bash-less reads,
# vendor abbreviations, pipes to filters — just works, like SecureCRT.

# Exec commands that change/disrupt the device or can exfiltrate config. Matched
# by whole first-token OR an unambiguous abbreviation prefix. These get a
# re-Enter confirm, not a hard block (a real engineer sometimes needs them).
_DESTRUCTIVE_VERBS = frozenset({
    "reload", "write", "wr", "copy", "clear", "delete", "erase", "format",
    "boot", "install", "request", "rollback", "commit",
})
# Verbs whose 3+ char abbreviations should also be treated as destructive.
_DESTRUCTIVE_ABBREV = ("reload", "clear", "copy", "delete", "erase", "format")
# Shell escapes bypass the guard entirely -> confirm before dropping to a shell.
_SHELL_ESCAPES = frozenset({"bash", "python", "python3", "tclsh", "ash", "zsh"})

# Command-chaining / redirection separators that let a benign-looking first token
# smuggle a second command. Pipe is NOT here: EOS/IOS only pipe to output filters
# (include/section/grep), which is safe and heavily used for reads.
_CHAIN_SEPARATORS = (";", "&", "`", "$(", ">", "<", "\x00", "\n", "\r")


def _collapse_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space, matching how device
    CLI parsers tokenize. Defeats double-space/tab evasion of the fragment lists."""
    return " ".join(text.split())


def _first_token(line: str) -> str:
    normalized = _collapse_ws(line).strip().lower()
    return normalized.split(" ", 1)[0] if normalized else ""


def _is_config_enter(token: str) -> bool:
    """A token that enters configuration mode, incl. vendor abbreviations."""
    if token in ("configuration", "config-transaction", "edit"):
        return True
    # any 4+ char prefix of "configure" (conf, confi, config, configure, ...)
    return len(token) >= 4 and "configure".startswith(token)


def _is_destructive(effective_lower: str, in_config: bool) -> str | None:
    """Return the matched destructive verb, or None. `effective_lower` has any
    leading 'do ' already stripped and whitespace collapsed."""
    token = effective_lower.split(" ", 1)[0] if effective_lower else ""
    if token in _DESTRUCTIVE_VERBS:
        return token
    for verb in _DESTRUCTIVE_ABBREV:
        if len(token) >= 3 and verb.startswith(token):
            return verb
    if in_config and (token == "no" or token in ("shutdown", "shut")):
        return token
    return None


def classify_line(line: str, in_config: bool) -> dict[str, Any]:
    """Classify one completed command line. Pure function; heavily tested.

    ALLOW-BY-DEFAULT: returns "read" (flows to the device) unless the line
    enters config mode, is destructive, is a shell escape, touches credentials,
    or chains commands."""
    normalized = _collapse_ws(line).strip()
    lowered = normalized.lower()
    if not lowered:
        return {"kind": "empty"}
    # Command chaining lets 'show x ; reload' smuggle a second command past
    # first-token classification. Reject outright.
    for sep in _CHAIN_SEPARATORS:
        if sep in lowered:
            return {"kind": "blocked_chain", "match": sep.strip() or "control"}
    # Credential/AAA floor — matched on the whitespace-collapsed line so
    # 'enable  secret' / 'username\tadmin' can't dodge it.
    for fragment in ALWAYS_BLOCKED_FRAGMENTS:
        if fragment in lowered + " ":
            return {"kind": "always_blocked", "fragment": fragment.strip()}
    token = _first_token(lowered)
    # Unwrap 'do <exec>' so a config-mode do-command is judged as the exec it runs.
    effective = lowered[3:].strip() if lowered.startswith("do ") else lowered
    eff_token = effective.split(" ", 1)[0] if effective else ""

    if _is_config_enter(token) or _is_config_enter(eff_token):
        return {"kind": "config_enter"}
    if in_config and lowered in ("end", "exit", "abort"):
        return {"kind": "config_exit"}
    match = _is_destructive(effective, in_config)
    if match:
        return {"kind": "dangerous", "match": match}
    if eff_token in _SHELL_ESCAPES:
        return {"kind": "dangerous", "match": eff_token}
    # 'do <exec>' is an exec command, not a config line, even inside config mode.
    if in_config and not lowered.startswith("do "):
        return {"kind": "config_line"}
    # Default: a normal exec/read command — let it flow, like real SSH.
    return {"kind": "read"}


def looks_like_paste(chunk: str) -> bool:
    return chunk.count("\n") + chunk.count("\r") >= 2


def _paste_decision(state: ShellSessionState, chunk: str) -> GuardDecision | None:
    """Intercept multi-line pastes containing anything that isn't a plain read,
    before ANY byte reaches the device. Fail closed: a paste passes only when
    EVERY line classifies as a strict read (or is blank)."""
    if not looks_like_paste(chunk):
        return None
    lines = [l for l in chunk.replace("\r", "\n").split("\n") if l.strip()]
    configish = False
    for line in lines:
        if line[:1].isspace():  # indented lines are config-block style
            configish = True
            break
        if classify_line(line, state.in_config)["kind"] not in ("read", "empty"):
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
        state.in_config = True
        state.device_touched = True
        return GuardDecision(forward=enter_byte, events=[{
            "type": "guard", "action": "config_mode_entered_live",
            "change_id": state.change_id,
            "message": "Configuration command sent live. The session is being recorded.",
        }, {"type": "command", "line": line.strip(), "kind": "config_enter",
            "change_id": state.change_id}])

    if kind == "dangerous":
        if state.pending_confirm == line:
            state.pending_confirm = None
            if state.in_config:
                state.device_touched = True
            return GuardDecision(forward=enter_byte, events=[{
                "type": "guard", "action": "dangerous_confirmed",
                "match": verdict["match"],
            }, {"type": "command", "line": line.strip(), "kind": "dangerous",
                "change_id": state.change_id}])
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
        }, {"type": "command", "line": line.strip(), "kind": "config_exit",
            "change_id": state.change_id}])

    if kind == "config_line":
        state.device_touched = True
        state.pending_confirm = None
        return GuardDecision(forward=enter_byte, events=[
            {"type": "command", "line": line.strip(), "kind": "config_line",
             "change_id": state.change_id}])

    if kind in ("blocked_unknown", "blocked_chain"):
        state.pending_confirm = None
        reason = ("command chaining/redirection is not allowed" if kind == "blocked_chain"
                  else f"'{verdict.get('token', '')}' is not a recognized read-only command")
        return GuardDecision(forward=KILL_LINE, events=[{
            "type": "guard", "action": kind,
            "message": (
                f"Blocked: {reason}. Read-only sessions allow only known read "
                "commands (show/ping/traceroute/...). To change the device, "
                "attach a change record. Nothing was sent to the device."
            ),
        }])

    # read / empty
    state.pending_confirm = None
    return GuardDecision(forward=enter_byte, events=[])


def has_forbidden_control(text: str) -> bool:
    """True if the text carries control/escape bytes that could make what the
    DEVICE executes differ from the classified text (backspace edits the line,
    Ctrl-U wipes it, ESC starts a terminal sequence). Over REST we cannot
    reconstruct keystroke intent, so any such byte is fail-closed."""
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            continue
        if ch == "\x1b" or ord(ch) < 0x20 or ch == "\x7f":
            return True
    return False


def _tainted_block(state: ShellSessionState, line: str) -> dict[str, Any]:
    return {
        "cleared": False, "line": line, "kind": "tainted",
        "events": [{
            "type": "guard", "action": "blocked_unverifiable_line",
            "message": (
                "Blocked: the input contained control or escape characters the "
                "guard can't verify (e.g. backspace/kill/escape). Send plain "
                "command text. Nothing was sent to the device."
            ),
        }],
        "state": state.as_dict(),
    }


def submit_line(state: ShellSessionState, line: str) -> dict[str, Any]:
    """Line-oriented gate for the REST Shell (MVP1/2). Runs the SAME tested
    decision as the streaming path. Fail-closed on control/escape bytes so the
    device can't execute something other than the classified text."""
    if has_forbidden_control(line):
        return _tainted_block(state, line)
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


def guard_submit(state: ShellSessionState, text: str) -> dict[str, Any]:
    """The single fail-closed entry for REST input (one line OR a multi-line
    paste). Centralizes every decision so the runner just executes the result.

    Returns a dict with `kind`:
      - "run_reads": `lines` are cleared read-only commands to execute
      - "run_live": `lines` are cleared live CLI/config commands to execute
      - "paste_intercept": a config-ish paste to stage as a change
      - "blocked": guard refused; see `events`
    """
    if has_forbidden_control(text):
        blocked = _tainted_block(state, text)
        return {"kind": "blocked", "lines": [], "events": blocked["events"], "state": blocked["state"]}

    lines = [l for l in text.replace("\r", "\n").split("\n") if l.strip()]

    if len(lines) > 1:
        # Multi-line input in the human shell runs live as long as every line
        # passes the same per-line safety floor. This supports real config
        # blocks while still rejecting credentials, chaining, and destructive
        # commands that need an explicit one-line confirmation.
        live_events: list[dict[str, Any]] = []
        for line in lines:
            result = submit_line(state, line)
            live_events.extend(result["events"])
            if not result["cleared"] or result["kind"] in ("dangerous", "always_blocked", "blocked_chain"):
                return {"kind": "blocked", "lines": [], "state": state.as_dict(), "events": live_events or result["events"]}
        live_kind = "run_live" if state.device_touched or any(e.get("kind") in ("config_enter", "config_line") for e in live_events) else "run_reads"
        return {"kind": live_kind, "lines": lines, "events": live_events, "state": state.as_dict()}

    single = lines[0] if lines else ""
    result = submit_line(state, single)
    if result["cleared"] and not state.in_config and result["kind"] in ("read", "config_exit", "empty"):
        return {"kind": "run_reads", "lines": [single], "events": result["events"], "state": result["state"]}
    if result["cleared"] and result["kind"] in ("config_enter", "config_line"):
        return {"kind": "run_live", "lines": [single], "events": result["events"], "state": result["state"]}
    if result["cleared"] and state.in_config:
        return {"kind": "run_live", "lines": [single], "events": result["events"], "state": result["state"]}
    return {"kind": "blocked", "lines": [], "events": result["events"], "state": result["state"]}


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
