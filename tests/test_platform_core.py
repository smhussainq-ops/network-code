from pathlib import Path

from fastapi.testclient import TestClient

from netcode.adapters.registry import AdapterRegistry
from netcode.adapters.rez import RezAdapterBridge
from netcode import api
from netcode.bootstrap import init_workspace
from netcode.discovery import DiscoveryService
from netcode.inventory import Device, Inventory
from netcode.jobs import JobRunner
from netcode.paths import WorkspacePaths
from netcode.platform import platform_capabilities
from netcode.source_of_truth import source_of_truth
from netcode.store import PlatformStore
from netcode.verification import verify_vlan_state
from netcode.workflow import state_after_lab_action, state_after_static_validation, workflow_snapshot


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
    assert any(provider["id"] == "netbox" for provider in providers.json()["providers"])
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
    assert data["path"].endswith("templates/arista/add_vlan.j2")
    assert "vlan {{ vlan.id }}" in data["body"]


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


def test_app_route_serves_ui():
    response = TestClient(api.app).get("/app")

    assert response.status_code == 200
    assert "Netcode" in response.text
    assert "15 capabilities wired behind one simple workflow" in response.text
    assert "Live outcome" in response.text
    assert "Action journal" in response.text
    assert "Production readiness gates" in response.text
    assert "Policy gate" in response.text
    assert "Device commands" in response.text
    assert "Device Commands" in response.text
    assert "Source of truth" in response.text
    assert "Adapter matrix" in response.text
    assert "Discovery" in response.text
    assert "Discover device" in response.text
    assert "Guided Journey" in response.text
    assert "Build the change path one artifact at a time" in response.text
    assert "0/11 complete" in response.text
    assert "Define Change" in response.text
    assert "Use this request" in response.text
    assert "What this means" in response.text
    assert "Show raw artifact / audit evidence" in response.text
    assert "Git Setup" in response.text
    assert "Jinja Template" in response.text


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
