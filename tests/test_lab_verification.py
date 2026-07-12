from netcode.inventory import Device
from netcode.lab import AristaEOSLabAdapter, dry_run_capability, offline_dry_run


class _Render:
    def __init__(self, config: str):
        self.config = config


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
