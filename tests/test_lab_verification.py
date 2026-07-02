from netcode.inventory import Device
from netcode.lab import AristaEOSLabAdapter


def _adapter(outputs):
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
    adapter = AristaEOSLabAdapter(device)
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
