from pathlib import Path

from netcode.adapters.rez import READ_TRANSPORTS, RezAdapterBridge
from netcode.inventory import Device


def test_rez_bridge_collects_configured_facts_before_disconnect(tmp_path: Path):
    rez_root = tmp_path / "rez"
    drivers = rez_root / "drivers"
    drivers.mkdir(parents=True)
    (drivers / "__init__.py").write_text("")
    (drivers / "collector.py").write_text(
        """
class FakeDriver:
    def __init__(self, host, username, password, port):
        self.host = host
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def get_full_state(self):
        return {"node_id": "d1", "platform": "fake_os"}

DRIVER_MAP = {"fake_os": FakeDriver}
""".strip()
    )
    (drivers / "configured_state.py").write_text(
        """
async def collect_configured_state(platform, driver, state):
    assert driver.connected is True
    return {
        "schema": "rez.configured-state.v1",
        "platform": platform,
        "sections": {"bgp": {"status": "ok", "records": []}},
        "dependencies": [],
        "raw_configuration_returned": False,
    }
""".strip()
    )

    result = RezAdapterBridge(root=rez_root).collect_device_state(
        Device(
            id="d1",
            host="127.0.0.1",
            platform="fake_os",
            username="u",
            password="p",
            port=22,
            hostname="d1",
            site="test",
            groups=(),
        )
    )

    assert result["ok"] is True
    assert result["state"]["configured_state"]["schema"] == "rez.configured-state.v1"
    assert result["state"]["configured_state"]["raw_configuration_returned"] is False


def test_manager_drivers_are_api_only_read_transports():
    assert READ_TRANSPORTS["fortimanager"] == ("api",)
    assert READ_TRANSPORTS["panorama"] == ("api",)


def test_rez_bridge_attaches_runner_local_enable_secret_without_serializing_it(monkeypatch):
    class FakeAristaDriver:
        def __init__(self, hostname, username, password, port):
            self.hostname = hostname

    bridge = RezAdapterBridge()
    monkeypatch.setattr(bridge, "_load_driver_map", lambda: {"arista_eos": FakeAristaDriver})
    device = Device(
        id="edge-1",
        host="192.0.2.10",
        platform="arista_eos",
        username="operator",
        password="login-password",
        port=22,
        hostname="edge-1",
        site="site-1",
        groups=(),
        connection_options={"secret": "enable-secret"},
    )

    platform, driver = bridge.build_driver(device)

    assert platform == "arista_eos"
    assert driver.enable_secret == "enable-secret"
    assert "enable-secret" not in repr(device)
