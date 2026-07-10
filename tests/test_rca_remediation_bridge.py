from pathlib import Path
import json

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore
from netcode.yamlio import read_yaml


def _confirmed_proposal(payload: dict) -> dict:
    return {
        "proposal_schema": "netcode.remediation.v1",
        "proposal_source": "rez_structured_rca",
        "root_confirmed": True,
        "root_atom_id": "CONFIG_EXACT_REMEDIATION_REQUIRED",
        **payload,
    }


def test_rca_remediation_runs_static_validation_without_jobs(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-2048",
            "target_device": "Branch-EDGE-03",
            "suggested_pack": "firewall_policy",
            "rationale": "Rez found missing outbound NAT evidence for the scoped flow.",
            "confidence": 0.82,
            "evidence_refs": ["show firewall policy", "policy/select"],
            "proposed_intent": {
                "site": "Site-204",
                "commands": [
                    "config firewall policy",
                    "edit 2048",
                    "set nat enable",
                    "next",
                    "end",
                ],
                "rollback_lines": "config firewall policy\nedit 2048\nunset nat\nnext\nend",
                "verify_contains": "nat enable",
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    change = store.get_change(body["change_id"])
    assert change.status == "blocked"
    assert change.workflow_state == "blocked"
    assert change.device_id == "Branch-EDGE-03"
    assert change.last_job_id is None
    assert store.list_jobs() == []
    assert change.result["plan"]["commands"]
    assert change.result["pipeline"]["render"]["config"]
    assert change.result["pipeline"]["artifacts"]["report_json_path"]
    assert any(check["status"] == "fail" for check in change.result["pipeline"]["validation"]["checks"])

    intent = read_yaml(Path(change.intent_path))
    assert intent["change_type"] == "custom_config"
    assert intent["targets"] == {"device_ids": ["Branch-EDGE-03"]}
    assert "set nat enable" in intent["custom"]["config_lines"]
    assert intent["custom"]["rollback_lines"]
    assert intent["metadata"]["source"] == "rez_rca"
    assert intent["metadata"]["human_approval_required"] is True

    events = store.list_workflow_events(change.id)
    assert len(events) == 1
    assert events[0].action == "rca_proposal"
    assert events[0].to_state == "blocked"


def test_rca_remediation_preserves_known_typed_intent(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-ACL-01",
            "target_device": "Edge-FW-01",
            "suggested_pack": "acl_rule",
            "rationale": "Scoped flow needs an explicit HTTPS permit.",
            "proposed_intent": {
                "change_type": "acl_rule",
                "site": "Site-101",
                "acl": {
                    "name": "EDGE-IN",
                    "sequence": 40,
                    "action": "permit",
                    "protocol": "tcp",
                    "source": "10.10.0.0/24",
                    "destination": "203.0.113.10/32",
                    "destination_port": "443",
                },
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    intent = body["intent"]
    assert intent["change_type"] == "acl_rule"
    assert intent["targets"] == {"device_ids": ["Edge-FW-01"]}
    assert intent["metadata"]["ticket_id"] == "INC-ACL-01"
    assert body["change"]["workflow_state"] == "blocked"


def test_site_context_interface_remediation_stays_typed_and_human_gated(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-CAMPUS-ET2",
            "target_device": "v2-store1",
            "suggested_pack": "interface_config",
            "rationale": "Restore the exact intended interface dependency.",
            "proposed_intent": {
                "change_type": "interface_config",
                "site": "campus",
                "values": {
                    "interface": "Ethernet2",
                    "mode": "routed",
                    "enabled": True,
                    "ip_address": "10.3.2.1/30",
                },
                "interface": {
                    "name": "Ethernet2",
                    "description": "Restore intended operational dependency",
                    "enabled": True,
                    "mode": "routed",
                    "access_vlan": None,
                    "trunk_allowed_vlans": [],
                    "ip_address": "10.3.2.1/30",
                },
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    intent = body["intent"]
    assert intent["change_type"] == "interface_config"
    assert intent["interface"]["name"] == "Ethernet2"
    assert intent["interface"]["enabled"] is True
    assert intent["metadata"]["draft_only"] is True
    assert intent["metadata"]["human_approval_required"] is True
    assert body["change"]["workflow_state"] in {"validated", "blocked"}


def test_site_context_redistribution_remediation_is_typed_validated_and_human_gated(
    tmp_path: Path, monkeypatch
):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "root_atom_id": "CP_REDISTRIBUTION_GAP",
            "proposal_source": "site_operational_context",
            "source": "rez",
            "incident_id": "INC-CAMPUS-REDIST",
            "target_device": "v2-store1",
            "suggested_pack": "routing_redistribution",
            "rationale": "Approved BGP-to-OSPF boundary is absent and scoped reachability failed.",
            "evidence_refs": ["approved-design:campus-bgp-to-ospf", "live:ssh"],
            "proposed_intent": {
                "change_type": "routing_redistribution",
                "site": "campus",
                "targets": {"device_ids": ["v2-store1"]},
                "redistribution": {
                    "from_protocol": "bgp",
                    "to_protocol": "ospf",
                    "target_process": "1",
                    "route_map": "CAMPUS-BGP-TO-OSPF",
                    "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
                    "prefixes": ["1.1.1.0/24", "2.2.2.0/24", "4.4.4.0/24", "5.5.5.0/24"],
                    "route_tag": 65002,
                },
            },
        }),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True
    assert body["change"]["workflow_state"] == "validated"
    assert body["intent"]["change_type"] == "routing_redistribution"
    assert body["intent"]["redistribution"]["route_map"] == "CAMPUS-BGP-TO-OSPF"
    commands = body["change"]["result"]["plan"]["commands"]
    assert "ip prefix-list ENTERPRISE-REMOTE-LOOPBACKS" in commands
    assert "route-map CAMPUS-BGP-TO-OSPF permit 10" in commands
    assert "router ospf 1" in commands
    assert "redistribute bgp route-map CAMPUS-BGP-TO-OSPF" in commands
    rollback = body["change"]["result"]["plan"]["rollback"]
    assert "no redistribute bgp route-map CAMPUS-BGP-TO-OSPF" in rollback
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_rez_rca_validated_draft_can_enter_dry_run_queue(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-DRYRUN",
            "target_device": "v2-store1",
            "suggested_pack": "custom_config",
            "rationale": "Rez proposed a reviewed config draft.",
            "proposed_intent": {
                "site": "store-1842",
                "config_lines": "vlan 992\n   name RCA_DRYRUN\n",
                "rollback_lines": "no vlan 992\n",
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["change"]["workflow_state"] == "validated"
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True

    from netcode.jobs import JobRunner

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    dry_run = JobRunner(WorkspacePaths(tmp_path.resolve()), store=store).run_lab_action(
        Path(body["intent_path"]),
        "dry-run",
        "v2-store1",
        body["change_id"],
    )

    assert dry_run["ok"] is True
    assert dry_run["queued"] is True
    assert dry_run["job"]["action"] == "lab_dry-run"
    assert dry_run["change"]["workflow_state"] == "validated"


def test_rca_remediation_strips_credential_shaped_fields(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-CREDS",
            "target_device": "Edge-FW-01",
            "suggested_pack": "acl_rule",
            "rationale": "Scoped flow needs an explicit HTTPS permit.",
            "proposed_intent": {
                "change_type": "acl_rule",
                "site": "Site-101",
                "password": "hunter2",
                "metadata": {"api_token": "token-should-not-persist", "operator_note": "safe"},
                "policy": {"pci_reachable": False, "private_key": "key-should-not-persist"},
                "acl": {
                    "name": "EDGE-IN",
                    "sequence": 40,
                    "action": "permit",
                    "protocol": "tcp",
                    "source": "10.10.0.0/24",
                    "destination": "203.0.113.10/32",
                    "destination_port": "443",
                    "enable_secret": "secret-should-not-persist",
                },
            },
        }),
    )

    assert response.status_code == 200
    intent_path = Path(response.json()["intent_path"])
    serialized = json.dumps(read_yaml(intent_path), sort_keys=True)
    assert "hunter2" not in serialized
    assert "token-should-not-persist" not in serialized
    assert "key-should-not-persist" not in serialized
    assert "secret-should-not-persist" not in serialized
    assert "password" not in serialized.lower()
    assert "api_token" not in serialized.lower()
    assert "private_key" not in serialized.lower()
    assert "enable_secret" not in serialized.lower()


def test_rez_rca_draft_requires_approval_even_when_global_gate_off(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_REQUIRE_APPROVAL", "0")
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-APPROVAL",
            "target_device": "v2-store1",
            "suggested_pack": "custom_config",
            "rationale": "Rez proposed a reviewed config draft.",
            "proposed_intent": {
                "site": "store-1842",
                "config_lines": "vlan 991\n   name RCA_REVIEWED\n",
                "rollback_lines": "no vlan 991\n",
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    change = store.get_change(body["change_id"])
    store.record_workflow_event(change.id, "dry-run", change.workflow_state, "dry_run_passed", "dry-run proof", {})

    from netcode.jobs import JobRunner

    blocked = JobRunner(WorkspacePaths(tmp_path.resolve()), store=store).run_lab_action(
        Path(body["intent_path"]),
        "apply",
        "v2-store1",
        change.id,
    )

    assert blocked["ok"] is False
    assert blocked["result"]["approval_required"] is True
    assert blocked["result"]["workflow_state"] == "dry_run_passed"


def test_rca_remediation_rejects_agent_narrative_without_confirmed_root(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json={
            "source": "rez",
            "incident_id": "INC-UNCONFIRMED",
            "target_device": "v2-store1",
            "rationale": "Agent Analysis / Unverified Hypothesis",
            "proposed_intent": {
                "change_type": "custom_config",
                "config_lines": "No configuration change is recommended from this run.",
            },
        },
    )

    assert response.status_code == 400
    assert "structured Netcode remediation proposal" in response.json()["detail"]
    assert PlatformStore(workspace).list_changes() == []


def test_rca_remediation_rejects_non_actionable_framework_root(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "root_atom_id": "CI_ROOT_CAUSE",
            "source": "rez",
            "incident_id": "INC-FRAMEWORK",
            "target_device": "v2-store1",
            "proposed_intent": {
                "change_type": "custom_config",
                "config_lines": "description should-not-land",
            },
        }),
    )

    assert response.status_code == 400
    assert "not an actionable device condition" in response.json()["detail"]
    assert PlatformStore(workspace).list_changes() == []


def test_rca_remediation_requires_target_scope(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-MISSING-SCOPE",
            "rationale": "Missing target should fail closed.",
            "proposed_intent": {"change_type": "custom_config", "config_lines": "description x"},
        }),
    )

    assert response.status_code == 400
    assert "target_device" in response.json()["detail"]
