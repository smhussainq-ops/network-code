from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore
from netcode.yamlio import read_yaml


def test_rca_remediation_creates_draft_custom_change_without_jobs(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json={
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
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    change = store.get_change(body["change_id"])
    assert change.status == "draft"
    assert change.workflow_state == "draft"
    assert change.device_id == "Branch-EDGE-03"
    assert change.last_job_id is None
    assert store.list_jobs() == []

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


def test_rca_remediation_preserves_known_typed_intent(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json={
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
        },
    )

    assert response.status_code == 200
    body = response.json()
    intent = body["intent"]
    assert intent["change_type"] == "acl_rule"
    assert intent["targets"] == {"device_ids": ["Edge-FW-01"]}
    assert intent["metadata"]["ticket_id"] == "INC-ACL-01"
    assert body["change"]["workflow_state"] == "draft"


def test_rca_remediation_requires_target_scope(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json={
            "source": "rez",
            "incident_id": "INC-MISSING-SCOPE",
            "rationale": "Missing target should fail closed.",
            "proposed_intent": {"change_type": "custom_config", "config_lines": "description x"},
        },
    )

    assert response.status_code == 400
    assert "target_device" in response.json()["detail"]
