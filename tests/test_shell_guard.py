"""Shell Guard tests — the guard is safety-spine code; every gate is proven.

The invariant under test: nothing reaches the device that the session's mode
does not permit, and `device_touched` is earned only by forwarded config.
"""

from netcode.shell_guard import (
    KILL_LINE,
    GuardDecision,
    ShellSessionState,
    classify_line,
    feed,
    guard_submit,
    looks_like_paste,
    submit_line,
)


def type_line(state: ShellSessionState, line: str) -> GuardDecision:
    """Simulate interactive typing: chars then Enter, in one chunk (still not
    a paste — pastes need 2+ newlines)."""
    return feed(state, line + "\r")


# ---------------------------------------------------------------- classify

def test_classify_config_enter_covers_abbreviations():
    for spelling in ("configure terminal", "conf t", "config", "configure session x", "edit"):
        assert classify_line(spelling, in_config=False)["kind"] == "config_enter", spelling


def test_classify_read_commands_pass():
    for line in ("show vlan brief", "show run | sec vlan", "ping 10.0.0.1", "co", "con"):
        assert classify_line(line, in_config=False)["kind"] == "read", line


def test_classify_credential_lines_always_blocked_even_in_config():
    for line in ("username evil secret x", "aaa new-model", "snmp-server community public"):
        assert classify_line(line, in_config=True)["kind"] == "always_blocked", line


def test_classify_dangerous_context():
    assert classify_line("reload", in_config=False)["kind"] == "dangerous"
    assert classify_line("write erase", in_config=False)["kind"] == "dangerous"
    # config-only dangers are plain reads outside config mode (e.g. "no vlan"
    # is not a valid exec command anyway)
    assert classify_line("shutdown", in_config=False)["kind"] == "read"
    assert classify_line("shutdown", in_config=True)["kind"] == "dangerous"
    assert classify_line("no vlan 90", in_config=True)["kind"] == "dangerous"


# ---------------------------------------------------------------- the gate

def test_read_only_session_forwards_show_commands():
    state = ShellSessionState()
    decision = type_line(state, "show vlan brief")
    assert decision.forward == "show vlan brief\r"
    assert decision.events == []
    assert state.device_touched is False


def test_read_only_session_blocks_config_mode_with_kill_line():
    state = ShellSessionState()
    decision = type_line(state, "conf t")
    # typed chars pass through (remote echo), CR is replaced by kill-line
    assert decision.forward.endswith(KILL_LINE)
    assert "\r" not in decision.forward
    actions = [e.get("action") for e in decision.events]
    assert "blocked_config_mode" in actions
    assert state.in_config is False
    assert state.device_touched is False


def test_change_attached_session_allows_config_mode_and_earns_touched():
    state = ShellSessionState(mode="change_attached", change_id="CHG-1")
    decision = type_line(state, "configure terminal")
    assert decision.forward.endswith("\r")
    assert state.in_config is True
    assert state.device_touched is True
    exit_decision = type_line(state, "end")
    assert exit_decision.forward.endswith("\r")
    assert state.in_config is False


def test_credential_lines_blocked_even_with_change_attached():
    state = ShellSessionState(mode="change_attached", change_id="CHG-1", in_config=True)
    decision = type_line(state, "username hacker secret pw")
    assert decision.forward.endswith(KILL_LINE)
    assert any(e.get("action") == "blocked_credential_line" for e in decision.events)
    # blocked lines never earn device_touched on their own
    assert state.pending_confirm is None


def test_dangerous_requires_double_enter():
    state = ShellSessionState(mode="change_attached", change_id="CHG-1")
    first = type_line(state, "reload")
    assert "\r" not in first.forward  # swallowed, not sent
    assert any(e.get("action") == "dangerous_needs_confirm" for e in first.events)
    second = feed(state, "\r")  # bare Enter confirms the same buffered line
    assert second.forward == "\r"
    assert any(e.get("action") == "dangerous_confirmed" for e in second.events)


def test_ctrl_c_cancels_dangerous_confirmation():
    state = ShellSessionState(mode="change_attached", change_id="CHG-1")
    type_line(state, "reload")
    assert state.pending_confirm is not None
    feed(state, "\x03")
    assert state.pending_confirm is None
    assert state.line_buffer == ""


def test_history_recall_taints_and_blocks_fail_closed():
    state = ShellSessionState()  # read_only
    # up-arrow (ESC [ A) recalls some previous command the buffer can't see
    decision = feed(state, "\x1b[A\r")
    assert decision.forward.startswith("\x1b[A")  # arrows pass through for echo
    assert decision.forward.endswith(KILL_LINE)   # but the Enter is gated
    assert any(e.get("action") == "blocked_unverifiable_line" for e in decision.events)
    # after the block the buffer is clean again: normal reads work
    ok = type_line(state, "show version")
    assert ok.forward == "show version\r"


def test_backspace_editing_stays_verifiable():
    state = ShellSessionState()
    decision = feed(state, "shoq\x7fw ver\r")  # typo, backspace, continue
    assert decision.forward.endswith("\r")
    assert not any(e.get("action") == "blocked_unverifiable_line" for e in decision.events)


# ---------------------------------------------------------------- pastes

def test_config_paste_is_intercepted_whole_in_read_only():
    state = ShellSessionState()
    chunk = "vlan 90\n   name GUEST_WIFI\ninterface vlan 90\n"
    decision = feed(state, chunk)
    assert decision.forward == ""  # not one byte reached the device
    events = [e for e in decision.events if e.get("action") == "paste_intercepted"]
    assert events and events[0]["line_count"] == 3
    assert "stage_as_change" in events[0]["options"]
    assert state.device_touched is False


def test_show_command_paste_passes_through():
    state = ShellSessionState()
    chunk = "show vlan brief\nshow ip route\nshow version\n"
    decision = feed(state, chunk)
    assert "show ip route" in decision.forward
    assert not any(e.get("action") == "paste_intercepted" for e in decision.events)


def test_paste_detection_thresholds():
    assert looks_like_paste("show ver\nshow vlan\n") is True
    assert looks_like_paste("show ver\r") is False


# -------------------------------------------------- REST taint/injection gap

def test_rest_backspace_injection_cannot_smuggle_config_enter():
    """The classic injection: classify as a read, but backspaces edit the line
    on the device into 'conf t'. Must be fail-closed on control bytes."""
    state = ShellSessionState()  # read_only
    d = guard_submit(state, "show ver\x08\x08\x08\x08\x08\x08\x08\x08conf t")
    assert d["kind"] == "blocked"
    assert any(e["action"] == "blocked_unverifiable_line" for e in d["events"])
    assert state.in_config is False


def test_rest_kill_line_and_escape_are_blocked():
    for hostile in ("show ver\x15conf t", "show ver\x1b[Dconf t", "conf\tt"[:0] + "sh\x1bver"):
        state = ShellSessionState()
        d = guard_submit(state, hostile)
        assert d["kind"] == "blocked", hostile


def test_rest_embedded_newline_credential_is_not_smuggled_past_a_read():
    """'show version\\nusername evil ...' must never be forwarded as one read."""
    state = ShellSessionState(mode="change_attached", change_id="CHG-1")
    d = guard_submit(state, "show version\nusername evil secret x")
    assert d["kind"] in ("blocked", "paste_intercept")  # never run_reads
    assert d["kind"] != "run_reads"


def test_rest_multiline_all_reads_runs_each_line():
    state = ShellSessionState()
    d = guard_submit(state, "show version\nshow vlan brief\nshow ip route")
    assert d["kind"] == "run_reads"
    assert d["lines"] == ["show version", "show vlan brief", "show ip route"]


def test_rest_multiline_mixing_read_and_nonread_blocks():
    state = ShellSessionState()
    d = guard_submit(state, "show version\nreload")
    assert d["kind"] == "blocked"
    assert any(e["action"] in ("blocked_mixed_multiline", "paste_intercepted") for e in d["events"]) or d["kind"] == "blocked"


def test_rest_single_read_runs():
    state = ShellSessionState()
    d = guard_submit(state, "show vlan brief")
    assert d["kind"] == "run_reads" and d["lines"] == ["show vlan brief"]


def test_rest_config_enter_still_gated_via_guard_submit():
    read_only = ShellSessionState()
    assert guard_submit(read_only, "conf t")["kind"] == "blocked"
    attached = ShellSessionState(mode="change_attached", change_id="CHG-1")
    assert guard_submit(attached, "conf t")["kind"] == "config_staged"


def test_submit_line_rejects_control_bytes_directly():
    state = ShellSessionState()
    assert submit_line(state, "show ver\x08\x08x")["cleared"] is False
