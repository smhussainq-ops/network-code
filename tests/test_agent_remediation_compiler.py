import copy
import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA
from netcode.network_model_lifecycle import activate_verified_revision, approve_with_git
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.models import RenderResult, load_intent_data
from netcode.store import PlatformStore
from netcode.validation import StaticValidator


ORG_ID = "org_default"
ENVIRONMENT_ID = "env-branch"
REVISION_ID = "branch-approved-v1"
DEVICE_ID = "edge-a"
SIGNING_SECRET = b"test-agent-recommendation-secret-with-32-characters"


@pytest.fixture(autouse=True)
def _recommendation_signing_secret(monkeypatch):
    monkeypatch.setenv(
        "REZ_REMEDIATION_REVIEW_SECRET",
        SIGNING_SECRET.decode("utf-8"),
    )


def _activate_model(tmp_path: Path) -> None:
    workspace = WorkspacePaths(tmp_path.resolve())
    store = PlatformStore(workspace)
    repository = NetworkModelRepository(store)
    repository.create_revision(
        {
            "schema": NETWORK_MODEL_SCHEMA,
            "org_id": ORG_ID,
            "environment_id": ENVIRONMENT_ID,
            "revision_id": REVISION_ID,
            "status": "proposed",
            "source": {
                "type": "manual_review",
                "reference": f"approved:{REVISION_ID}",
            },
            "coverage": {"domains": ["identity", "sites", "topology"]},
            "authority_bindings": {
                domain: {"source": "manual_review", "mode": "propose"}
                for domain in ("identity", "sites", "topology")
            },
            "model": {
                "sites": {
                    "branch": {
                        "devices": {
                            DEVICE_ID: {
                                "role": "edge",
                                "platform": "arista_eos",
                            }
                        },
                        "operational_dependencies": [
                            {
                                "id": "branch-uplink",
                                "kind": "interface",
                                "device_id": DEVICE_ID,
                                "interface": "Ethernet3",
                            }
                        ],
                    }
                },
                "devices": {
                    DEVICE_ID: {
                        "site": "branch",
                        "role": "edge",
                        "platform": "arista_eos",
                    }
                },
            },
        },
        created_by="intent-reviewer",
    )
    approve_with_git(
        repository,
        org_id=ORG_ID,
        environment_id=ENVIRONMENT_ID,
        revision_id=REVISION_ID,
        approved_by="intent-reviewer",
        git_root=workspace.git_workspace,
    )
    activate_verified_revision(
        repository,
        store,
        org_id=ORG_ID,
        environment_id=ENVIRONMENT_ID,
        revision_id=REVISION_ID,
        actor="intent-reviewer",
        git_root=workspace.git_workspace,
        initial_baseline=True,
    )
    runner = store.create_runner(
        "connector-a",
        "pilot",
        "test-token-hash",
        "test-hmac-secret",
        org_id=ORG_ID,
    )
    store.sync_runner_devices(
        runner,
        [
            {
                "id": DEVICE_ID,
                "hostname": DEVICE_ID,
                "host": "192.0.2.10",
                "port": 22,
                "platform": "arista_eos",
                "site": "branch",
                "role": "edge",
                "groups": ["branch"],
                "aliases": [],
            }
        ],
        revision="catalog-v1",
    )


def _refresh_integrity(payload: dict) -> dict:
    proposed = payload["proposed_intent"]
    proof = payload["evidence_contract"]
    intent_digest = hashlib.sha256(
        api._canonical_json(proposed).encode("utf-8")
    ).hexdigest()
    proof["intent_digest"] = intent_digest
    proof["recommendation_fingerprint"] = hashlib.sha256(
        "|".join(
            (
                payload["incident_id"],
                payload["root_atom_id"],
                payload["target_device"],
                proof["root_digest"],
                intent_digest,
            )
        ).encode("utf-8")
    ).hexdigest()
    unsigned = {key: value for key, value in proof.items() if key != "signature"}
    proof["signature"] = hmac.new(
        SIGNING_SECRET,
        api._canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return payload


def _proposal(*, root_atom_id: str = "NORMALIZED_CONFIRMED_ROOT") -> dict:
    payload = {
        "source": "rez",
        "proposal_schema": "netcode.remediation.v1",
        "proposal_source": "rez_agent_recommendation",
        "root_confirmed": True,
        "root_atom_id": root_atom_id,
        "incident_id": "INC-AGENT-RECOMMENDATION",
        "target_device": DEVICE_ID,
        "suggested_pack": "interface_config",
        "rationale": "Restore the required interface administrative state.",
        "confidence": 0.96,
        "evidence_refs": ["interface: Ethernet3", f"scope device: {DEVICE_ID}"],
        "environment_id": ENVIRONMENT_ID,
        "model_revision_id": REVISION_ID,
        "proposed_intent": {
            "change_type": "interface_config",
            "site": "branch",
            "targets": {"device_ids": [DEVICE_ID]},
            "values": {
                "interface": "Ethernet3",
                "enabled": True,
                "apply_scope": "admin_state",
            },
        },
        "evidence_contract": {
            "schema": "rez.agent-remediation-evidence.v1",
            "sufficient_for_draft": True,
            "fresh": True,
            "live_root_confirmed": True,
            "root_atom_id": root_atom_id,
            "root_digest": "a" * 64,
            "target_device": DEVICE_ID,
            "change_type": "interface_config",
            "org_id": ORG_ID,
            "environment_id": ENVIRONMENT_ID,
            "model_revision_id": REVISION_ID,
            "evidence_refs": ["interface: Ethernet3", f"scope device: {DEVICE_ID}"],
            "agent_citations": ["rez.validate:confirmed"],
            "verification_checks": [
                "Interface administrative state is enabled",
                "Approved reachability passes",
            ],
            "signature_algorithm": "hmac-sha256",
        },
    }
    return _refresh_integrity(payload)


def test_agent_desired_state_compiles_to_one_draft_with_rollback_and_risk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    payload = _proposal()

    first = client.post("/api/changes/from-rca", json=payload)
    second = client.post("/api/changes/from-rca", json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    body = first.json()
    replay = second.json()
    assert replay["idempotent_replay"] is True
    assert replay["change_id"] == body["change_id"]
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True
    assert body["change"]["workflow_state"] == "validated"
    result = body["change"]["result"]
    commands = result["plan"]["commands"]
    rollback = result["plan"]["rollback"]
    assert "interface Ethernet3" in commands
    assert "no shutdown" in commands
    assert "interface Ethernet3" in rollback
    assert "shutdown" in rollback
    risk = body["risk_assessment"]
    assert risk["schema"] == "netcode.change-risk.v1"
    assert risk["human_approval_required"] is True
    assert risk["device_write_performed"] is False
    assert risk["rollback_available"] is True
    assert risk["modeled_dependency_count"] == 1
    assert risk["targets"] == [DEVICE_ID]
    record = client.get(f"/api/change/{body['change_id']}/record")
    assert record.status_code == 200
    assert record.json()["plan"]["risk_assessment"] == risk
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert len(store.list_changes(org_id=ORG_ID)) == 1
    assert store.list_jobs(org_id=ORG_ID) == []


def test_real_interface_admin_down_payload_reaches_compiler_without_resigning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    payload = _proposal(root_atom_id="L1_INTERFACE_ADMIN_DOWN")

    response = client.post("/api/changes/from-rca", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["change"]["workflow_state"] == "validated"
    assert "interface Ethernet3" in body["change"]["result"]["plan"]["commands"]
    assert "no shutdown" in body["change"]["result"]["plan"]["commands"]
    assert "shutdown" in body["change"]["result"]["plan"]["rollback"]
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_jobs(org_id=ORG_ID) == []


@pytest.mark.parametrize(
    ("change_type", "values", "command_fragment", "rollback_fragment"),
    [
        (
            "add_vlan",
            {
                "vlan_id": 777,
                "name": "REZ_REVIEWED",
                "subnet": "198.51.100.0/24",
                "svi_enabled": False,
            },
            "vlan 777",
            "no vlan 777",
        ),
        (
            "interface_config",
            {
                "interface": "Ethernet3",
                "enabled": True,
                "apply_scope": "admin_state",
            },
            "no shutdown",
            "shutdown",
        ),
        (
            "bgp_neighbor",
            {
                "asn": 65000,
                "neighbor": "192.0.2.2",
                "remote_as": 65001,
                "shutdown": True,
            },
            "neighbor 192.0.2.2 remote-as 65001",
            "no neighbor 192.0.2.2",
        ),
        (
            "acl_rule",
            {
                "acl_name": "REZ_REVIEWED",
                "sequence": 10,
                "action": "permit",
                "protocol": "tcp",
                "source": "198.51.100.0/24",
                "destination": "203.0.113.0/24",
            },
            "permit tcp 198.51.100.0/24 203.0.113.0/24",
            "no 10",
        ),
        (
            "ntp_standardize",
            {
                "servers": ["192.0.2.123", "192.0.2.124"],
                "prefer_first": True,
            },
            "ntp server 192.0.2.123",
            "no ntp server 192.0.2.123",
        ),
    ],
)
def test_every_advertised_agent_change_type_compiles_with_rollback(
    tmp_path: Path,
    monkeypatch,
    change_type: str,
    values: dict,
    command_fragment: str,
    rollback_fragment: str,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_proposal())
    payload["suggested_pack"] = change_type
    payload["proposed_intent"]["change_type"] = change_type
    payload["proposed_intent"]["values"] = values
    payload["evidence_contract"]["change_type"] = change_type
    _refresh_integrity(payload)

    response = TestClient(api.app).post("/api/changes/from-rca", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True
    assert body["change"]["workflow_state"] == "validated"
    plan = body["change"]["result"]["plan"]
    assert command_fragment in plan["commands"]
    assert rollback_fragment in plan["rollback"]
    assert body["risk_assessment"]["rollback_available"] is True
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert len(store.list_changes(org_id=ORG_ID)) == 1
    assert store.list_jobs(org_id=ORG_ID) == []


def test_acl_numbered_rule_scope_remains_fail_closed(tmp_path: Path) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    intent = load_intent_data(
        {
            "change_type": "acl_rule",
            "site": "branch",
            "targets": {"device_ids": [DEVICE_ID]},
            "acl": {
                "name": "REZ_REVIEWED",
                "sequence": 10,
                "action": "permit",
                "protocol": "ip",
                "source": "any",
                "destination": "any",
            },
        }
    )
    rendered = RenderResult(
        template_path=str(workspace.templates / "arista" / "acl_rule.j2"),
        config=(
            "ip access-list REZ_REVIEWED\n"
            "   10 permit ip any any\n"
            "   20 statistics per-entry\n"
        ),
        variables={},
    )

    result = StaticValidator(workspace)._render_scope(intent, rendered)

    assert result.status == "fail"
    assert result.evidence["unexpected_lines"] == ["   20 statistics per-entry"]


@pytest.mark.parametrize(
    "mutation,expected_status",
    [
        ("tampered_intent", 400),
        ("embedded_commands", 400),
        ("custom_config", 400),
        ("missing_required_value", 400),
        ("multiline_value", 400),
        ("unexpected_value", 400),
        ("string_boolean", 400),
        ("stale_model", 409),
        ("wrong_target", 400),
        ("forged_signature", 400),
    ],
)
def test_agent_recommendation_fails_closed_before_draft(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
    expected_status: int,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_proposal())
    if mutation == "tampered_intent":
        payload["proposed_intent"]["values"]["interface"] = "Ethernet9"
    elif mutation == "embedded_commands":
        payload["proposed_intent"]["commands"] = ["interface Ethernet3", "no shutdown"]
        _refresh_integrity(payload)
    elif mutation == "custom_config":
        payload["proposed_intent"] = {
            "change_type": "custom_config",
            "site": "branch",
            "targets": {"device_ids": [DEVICE_ID]},
            "values": {"description": "free form"},
        }
        payload["evidence_contract"]["change_type"] = "custom_config"
        _refresh_integrity(payload)
    elif mutation == "multiline_value":
        payload["proposed_intent"]["values"]["description"] = (
            "reviewed description\n   shutdown"
        )
        _refresh_integrity(payload)
    elif mutation == "missing_required_value":
        del payload["proposed_intent"]["values"]["interface"]
        _refresh_integrity(payload)
    elif mutation == "unexpected_value":
        payload["proposed_intent"]["values"]["ignored_by_compiler"] = "unsafe ambiguity"
        _refresh_integrity(payload)
    elif mutation == "string_boolean":
        payload["proposed_intent"]["values"]["enabled"] = "false"
        _refresh_integrity(payload)
    elif mutation == "stale_model":
        payload["model_revision_id"] = "superseded-v0"
        payload["evidence_contract"]["model_revision_id"] = "superseded-v0"
        _refresh_integrity(payload)
    elif mutation == "wrong_target":
        payload["target_device"] = "other-edge"
        payload["proposed_intent"]["targets"] = {"device_ids": ["other-edge"]}
        payload["evidence_contract"]["target_device"] = "other-edge"
        _refresh_integrity(payload)
    elif mutation == "forged_signature":
        payload["evidence_contract"]["signature"] = "f" * 64

    response = TestClient(api.app).post("/api/changes/from-rca", json=payload)

    assert response.status_code == expected_status
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_changes(org_id=ORG_ID) == []
    assert store.list_jobs(org_id=ORG_ID) == []


def test_agent_recommendation_cannot_cross_tenant_model_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    request = api.RcaRemediationProposalRequest.model_validate(_proposal())

    with pytest.raises(HTTPException) as exc_info:
        api._require_agent_recommendation_evidence(
            request,
            store=PlatformStore(WorkspacePaths(tmp_path.resolve())),
            org_id="org-other",
        )

    assert exc_info.value.status_code == 403


def test_agent_recommendation_fails_closed_without_verification_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REZ_REMEDIATION_REVIEW_SECRET", raising=False)
    monkeypatch.delenv("NETCODE_ADMIN_TOKEN", raising=False)

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=_proposal(),
    )

    assert response.status_code == 503
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_changes(org_id=ORG_ID) == []
    assert store.list_jobs(org_id=ORG_ID) == []
