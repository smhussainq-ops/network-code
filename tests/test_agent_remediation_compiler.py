import copy
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from netcode import api
from netcode.auth import Principal, hash_password, mint_session, token_hash
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA, NetworkModelError
from netcode.network_model_lifecycle import activate_verified_revision, approve_with_git
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.models import RenderResult, load_intent_data
from netcode.store import PlatformStore
from netcode.validation import StaticValidator
from netcode.workflow import require_action_allowed
from netcode.yamlio import read_yaml


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


def _sign_current_proof(payload: dict) -> dict:
    proof = payload["evidence_contract"]
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


def _review_proposal() -> dict:
    payload = _proposal(root_atom_id="NORMALIZED_ROUTING_ROOT")
    payload.update(
        {
            "suggested_pack": "routing_policy",
            "draft_mode": "needs_input",
            "expected_outcome": "Restore the approved route without shadowing.",
            "verification_checks": [
                "The approved prefix resolves through the intended next hop",
                "Round-trip reachability succeeds",
            ],
            "unresolved_inputs": [
                "Select the supported Netcode change type",
                "Supply exact forward and rollback intent",
            ],
            "proposed_intent": {
                "change_type": "routing_policy",
                "site": "branch",
                "targets": {"device_ids": [DEVICE_ID]},
                "values": {
                    "desired_outcome": "Restore the approved route without shadowing.",
                },
            },
        }
    )
    payload["evidence_contract"].update(
        {
            "change_type": "routing_policy",
            "draft_mode": "needs_input",
            "sufficient_for_review": True,
            "sufficient_for_execution": False,
            "unresolved_inputs": payload["unresolved_inputs"],
            "expected_outcome": payload["expected_outcome"],
            "verification_checks": payload["verification_checks"],
        }
    )
    return _refresh_integrity(payload)


def _auth_header(
    store: PlatformStore,
    *,
    org_id: str = ORG_ID,
    email: str,
    role: str,
) -> dict[str, str]:
    if org_id != ORG_ID:
        store.ensure_org(org_id, org_id, org_id)
    user = store.create_user(
        org_id,
        email,
        hash_password("review-contract-password"),
        role=role,
    )
    return {"Authorization": f"Bearer {mint_session(store, user.id, org_id)}"}


def _activate_replacement_model(tmp_path: Path) -> None:
    workspace = WorkspacePaths(tmp_path.resolve())
    store = PlatformStore(workspace)
    repository = NetworkModelRepository(store)
    active = repository.active_revision(ORG_ID, ENVIRONMENT_ID)
    assert active is not None
    replacement_id = "branch-approved-v2"
    repository.create_revision(
        {
            "schema": NETWORK_MODEL_SCHEMA,
            "org_id": ORG_ID,
            "environment_id": ENVIRONMENT_ID,
            "revision_id": replacement_id,
            "parent_revision_id": REVISION_ID,
            "status": "proposed",
            "source": {
                "type": "manual_review",
                "reference": f"approved:{replacement_id}",
            },
            "coverage": copy.deepcopy(active["coverage"]),
            "authority_bindings": copy.deepcopy(active["authority_bindings"]),
            "model": copy.deepcopy(active["model"]),
        },
        created_by="intent-reviewer",
    )
    approve_with_git(
        repository,
        org_id=ORG_ID,
        environment_id=ENVIRONMENT_ID,
        revision_id=replacement_id,
        approved_by="intent-reviewer",
        git_root=workspace.git_workspace,
    )
    activate_verified_revision(
        repository,
        store,
        org_id=ORG_ID,
        environment_id=ENVIRONMENT_ID,
        revision_id=replacement_id,
        actor="intent-reviewer",
        git_root=workspace.git_workspace,
        reviewed_intent_update=True,
        expected_current_revision_id=REVISION_ID,
    )


def _completion_payload() -> dict:
    return {
        "proposed_intent": {
            "change_type": "custom_config",
            "site": "branch",
            "targets": {"device_ids": [DEVICE_ID]},
            "config_lines": "interface Ethernet3\n   description REVIEWED_UPLINK\n",
            "rollback_lines": "interface Ethernet3\n   no description\n",
            "verify_contains": "description REVIEWED_UPLINK",
        },
        "expected_outcome": "The approved uplink intent is restored.",
        "verification_checks": [
            "Running configuration contains the reviewed description",
            "Approved reachability succeeds",
        ],
    }


def test_incomplete_agent_recommendation_creates_one_review_only_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    payload = _review_proposal()

    first = client.post("/api/changes/from-rca", json=payload)
    replay = client.post("/api/changes/from-rca", json=payload)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    body = first.json()
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["change_id"] == body["change_id"]
    assert "source_request" not in replay.json()["review_draft"]
    assert body["draft_mode"] == "needs_input"
    assert body["change"]["workflow_state"] == "needs_input"
    assert body["review_draft"]["expected_outcome"]
    assert body["review_draft"]["unresolved_inputs"] == payload["unresolved_inputs"]
    assert body["review_draft"]["commands"] == []
    assert body["review_draft"]["rollback"] == []
    assert body["workflow"]["allowed_actions"] == []
    assert "source_request" not in body["change"]["result"]["review_draft"]
    record = client.get(f"/api/change/{body['change_id']}/record")
    assert record.status_code == 200, record.text
    assert record.json()["review_draft"]["expected_outcome"]
    assert "source_request" not in record.json()["review_draft"]
    workflow = client.get(f"/api/workflow/change/{body['change_id']}")
    assert workflow.status_code == 200, workflow.text
    assert (
        "source_request"
        not in workflow.json()["change"]["result"]["review_draft"]
    )

    with pytest.raises(ValueError, match="blocked in workflow state needs_input"):
        require_action_allowed("needs_input", "dry_run")
    with pytest.raises(ValueError, match="blocked in workflow state needs_input"):
        require_action_allowed("needs_input", "apply")

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert len(store.list_changes(org_id=ORG_ID)) == 1
    assert "source_request" in (
        store.get_change(body["change_id"]).result or {}
    )["review_draft"]
    assert store.list_jobs(org_id=ORG_ID) == []


def test_review_only_mode_requires_signed_agent_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_review_proposal())
    payload["proposal_source"] = "rez_structured_rca"

    response = TestClient(api.app).post("/api/changes/from-rca", json=payload)

    assert response.status_code == 400
    assert "signed Rez agent recommendation" in response.json()["detail"]
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_changes(org_id=ORG_ID) == []
    assert store.list_jobs(org_id=ORG_ID) == []


def test_authenticated_engineer_completes_same_review_draft_before_dry_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]

    completion_payload = _completion_payload()
    completion_payload["proposed_intent"]["acknowledge_no_rollback"] = True
    completed = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=completion_payload,
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["change_id"] == change_id
    assert body["change"]["workflow_state"] == "validated"
    assert body["draft_mode"] == "executable"
    assert "description REVIEWED_UPLINK" in body["change"]["result"]["plan"]["commands"]
    assert "no description" in body["change"]["result"]["plan"]["rollback"]
    assert (
        body["change"]["result"]["risk_assessment"]["rollback_status"]
        == "partial"
    )
    assert body["change"]["result"]["rollback_risk_accepted"] is False
    assert any(
        "recommended desired state matches approved intent" in item
        for item in body["change"]["result"]["risk_assessment"]["unknowns"]
    )
    assert body["change"]["result"]["completed_by"]
    assert body["change"]["result"]["device_write_performed"] is False
    assert body["intent_path"].endswith("-completed.yaml")
    completed_intent = read_yaml(Path(body["intent_path"]))
    assert completed_intent["custom"]["acknowledge_no_rollback"] is False
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs(org_id=ORG_ID) == []


def test_engineer_may_accept_unavailable_rollback_without_weakening_other_gates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    payload = _completion_payload()
    payload["proposed_intent"]["rollback_lines"] = ""

    missing_acknowledgment = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=payload,
    )
    assert missing_acknowledgment.status_code == 400
    assert "explicit acknowledgment" in missing_acknowledgment.json()["detail"]

    for invalid_acknowledgment in ("true", "True", 1, []):
        payload["proposed_intent"][
            "acknowledge_no_rollback"
        ] = invalid_acknowledgment
        invalid_response = client.post(
            f"/api/change/{change_id}/complete-rca-draft",
            json=payload,
        )
        assert invalid_response.status_code == 400
        assert (
            "explicit acknowledgment"
            in invalid_response.json()["detail"]
        )

    payload["proposed_intent"]["acknowledge_no_rollback"] = True
    completed = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=payload,
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    result = body["change"]["result"]
    assert body["change"]["workflow_state"] == "validated"
    assert result["plan"]["rollback"] == ""
    assert result["risk_assessment"]["rollback_status"] == "unavailable"
    assert result["risk_assessment"]["rollback_available"] is False
    assert result["rollback_risk_accepted"] is True
    assert result["device_write_performed"] is False
    assert any(
        "recommended desired state matches approved intent" in item
        for item in result["risk_assessment"]["unknowns"]
    )
    assert any(
        "reviewer explicitly accepted" in item
        for item in result["risk_assessment"]["unknowns"]
    )
    assert "dry_run" in body["workflow"]["allowed_actions"]
    assert "apply" not in body["workflow"]["allowed_actions"]
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_jobs(org_id=ORG_ID) == []
    events = store.list_workflow_events(change_id)
    completion = [
        event for event in events if event.action == "complete_rca_draft"
    ]
    assert len(completion) == 1
    assert completion[0].evidence["rollback_status"] == "unavailable"
    assert completion[0].evidence["rollback_risk_accepted"] is True
    record = client.get(f"/api/change/{change_id}/record")
    assert record.status_code == 200, record.text
    assert record.json()["plan"]["risk_assessment"]["rollback_status"] == "unavailable"
    assert record.json()["plan"]["rollback_risk_accepted"] is True
    completed_intent = read_yaml(Path(body["intent_path"]))
    assert completed_intent["custom"]["acknowledge_no_rollback"] is True
    store.update_change(
        change_id,
        "completed",
        {"status": "pass"},
        workflow_state="rollback_available",
    )
    durable_record = client.get(f"/api/change/{change_id}/record")
    assert durable_record.status_code == 200, durable_record.text
    assert durable_record.json()["plan"]["rollback_risk_accepted"] is True


@pytest.mark.parametrize(
    ("root_atom_id", "change_type"),
    [
        ("CP_ROUTE_BLACKHOLE", "routing_policy"),
        ("CP_REDISTRIBUTION_GAP", "routing_redistribution"),
    ],
)
def test_specialized_evidence_gates_defer_signed_review_only_drafts(
    tmp_path: Path,
    monkeypatch,
    root_atom_id: str,
    change_type: str,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_review_proposal())
    payload["root_atom_id"] = root_atom_id
    payload["suggested_pack"] = change_type
    payload["proposed_intent"]["change_type"] = change_type
    payload["evidence_contract"]["root_atom_id"] = root_atom_id
    payload["evidence_contract"]["change_type"] = change_type
    _refresh_integrity(payload)

    response = TestClient(api.app).post("/api/changes/from-rca", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_mode"] == "needs_input"
    assert body["change"]["workflow_state"] == "needs_input"
    assert body["workflow"]["allowed_actions"] == []
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs(org_id=ORG_ID) == []


def test_review_completion_authentication_and_tenant_contracts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NETCODE_AUTH", raising=False)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    viewer = _auth_header(
        store,
        email="viewer@example.invalid",
        role="viewer",
    )
    operator = _auth_header(
        store,
        email="operator@example.invalid",
        role="operator",
    )
    other_operator = _auth_header(
        store,
        org_id="org-other",
        email="other-operator@example.invalid",
        role="operator",
    )
    revoked = _auth_header(
        store,
        email="revoked@example.invalid",
        role="operator",
    )
    revoked_token = revoked["Authorization"].removeprefix("Bearer ")
    store.revoke_session(token_hash(revoked_token))
    monkeypatch.setenv("NETCODE_AUTH", "1")

    assert client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=_completion_payload(),
    ).status_code == 401
    assert client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        headers=viewer,
        json=_completion_payload(),
    ).status_code == 403
    assert client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        headers=revoked,
        json=_completion_payload(),
    ).status_code == 401
    assert client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        headers=other_operator,
        json=_completion_payload(),
    ).status_code == 404

    completed = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        headers=operator,
        json=_completion_payload(),
    )
    replay = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        headers=operator,
        json=_completion_payload(),
    )
    assert completed.status_code == 200, completed.text
    assert replay.status_code == 409
    assert store.list_jobs(org_id=ORG_ID) == []


def test_review_completion_rejects_stale_active_model_with_auth_on(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NETCODE_AUTH", raising=False)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    original_path = created.json()["intent_path"]
    _activate_replacement_model(tmp_path)
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    operator = _auth_header(
        store,
        email="operator@example.invalid",
        role="operator",
    )
    monkeypatch.setenv("NETCODE_AUTH", "1")

    response = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        headers=operator,
        json=_completion_payload(),
    )

    assert response.status_code == 409
    change = store.get_change(change_id)
    assert change.workflow_state == "needs_input"
    assert change.intent_path == original_path
    assert store.list_jobs(org_id=ORG_ID) == []


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("signature", 400),
        ("organization", 403),
        ("intent_digest", 400),
    ],
)
def test_signed_review_intake_rejects_tampering_with_auth_on(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
    expected_status: int,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    operator = _auth_header(
        store,
        email="operator@example.invalid",
        role="operator",
    )
    payload = copy.deepcopy(_review_proposal())
    if mutation == "signature":
        payload["evidence_contract"]["signature"] = "f" * 64
    elif mutation == "organization":
        payload["evidence_contract"]["org_id"] = "org-other"
        _sign_current_proof(payload)
    else:
        proof = payload["evidence_contract"]
        proof["intent_digest"] = "b" * 64
        proof["recommendation_fingerprint"] = hashlib.sha256(
            "|".join(
                (
                    payload["incident_id"],
                    payload["root_atom_id"],
                    payload["target_device"],
                    proof["root_digest"],
                    proof["intent_digest"],
                )
            ).encode("utf-8")
        ).hexdigest()
        _sign_current_proof(payload)
    monkeypatch.setenv("NETCODE_AUTH", "1")

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        headers=operator,
        json=payload,
    )

    assert response.status_code == expected_status
    assert store.list_changes(org_id=ORG_ID) == []
    assert store.list_jobs(org_id=ORG_ID) == []


def test_failed_completion_preserves_review_artifact_and_can_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app, raise_server_exceptions=False)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    review_path = Path(created.json()["intent_path"])
    review_bytes = review_path.read_bytes()
    real_pipeline = api.run_static_pipeline

    def fail_pipeline(*_args, **_kwargs):
        raise RuntimeError("simulated compiler interruption")

    monkeypatch.setattr(api, "run_static_pipeline", fail_pipeline)
    failed = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=_completion_payload(),
    )

    assert failed.status_code == 500
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    unchanged = store.get_change(change_id)
    assert unchanged.workflow_state == "needs_input"
    assert unchanged.intent_path == str(review_path)
    assert review_path.read_bytes() == review_bytes
    assert store.list_jobs(org_id=ORG_ID) == []

    monkeypatch.setattr(api, "run_static_pipeline", real_pipeline)
    retried = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=_completion_payload(),
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["change"]["workflow_state"] == "validated"


def test_model_candidate_failure_keeps_review_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    original_path = created.json()["intent_path"]

    def reject_candidate(*_args, **_kwargs):
        raise NetworkModelError("simulated model rejection")

    monkeypatch.setattr(api, "create_candidate_for_change_intent", reject_candidate)
    response = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=_completion_payload(),
    )

    assert response.status_code == 409
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    change = store.get_change(change_id)
    assert change.workflow_state == "needs_input"
    assert change.intent_path == original_path
    assert store.list_jobs(org_id=ORG_ID) == []


def test_engineer_completed_redistribution_compiles_reversible_plan_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_review_proposal())
    payload["root_atom_id"] = "CP_REDISTRIBUTION_GAP"
    payload["suggested_pack"] = "routing_redistribution"
    payload["proposed_intent"]["change_type"] = "routing_redistribution"
    payload["evidence_contract"]["root_atom_id"] = "CP_REDISTRIBUTION_GAP"
    payload["evidence_contract"]["change_type"] = "routing_redistribution"
    _refresh_integrity(payload)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=payload)
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]

    completed = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json={
            "proposed_intent": {
                "change_type": "routing_redistribution",
                "site": "branch",
                "targets": {"device_ids": [DEVICE_ID]},
                "redistribution": {
                    "from_protocol": "bgp",
                    "to_protocol": "ospf",
                    "target_process": "1",
                    "route_map": "BRANCH-BGP-TO-OSPF",
                    "prefix_list": "BRANCH-APPROVED-PREFIXES",
                    "prefixes": ["198.51.100.0/24"],
                    "route_tag": 65000,
                },
            },
            "expected_outcome": "The reviewed prefix is eligible for redistribution.",
            "verification_checks": [
                "The prefix list contains the reviewed entry",
                "The approved route is present downstream",
            ],
        },
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    checks = body["change"]["result"]["pipeline"]["validation"]["checks"]
    assert body["change"]["workflow_state"] == "validated", [
        (check["id"], check.get("message"), check.get("evidence"))
        for check in checks
        if check["status"] != "pass"
    ]
    assert "ip prefix-list BRANCH-APPROVED-PREFIXES" in body["change"]["result"]["plan"]["commands"]
    assert "no ip prefix-list BRANCH-APPROVED-PREFIXES" in body["change"]["result"]["plan"]["rollback"]
    assert "dry_run" in body["workflow"]["allowed_actions"]
    assert "apply" not in body["workflow"]["allowed_actions"]
    assert body["device_write_performed"] is False
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs(org_id=ORG_ID) == []


def test_netcode_ui_exposes_review_completion_without_auto_apply() -> None:
    app_js = (
        Path(__file__).resolve().parents[1] / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "Engineer input required" in app_js
    assert "complete-rca-draft" in app_js
    assert "Validate change package" in app_js
    assert "rca-acknowledge-no-rollback" in app_js
    assert 'class="wide check-row"' in app_js
    assert 'id="rca-acknowledge-no-rollback" type="checkbox" />' in app_js
    assert "automatic rollback is unavailable" in app_js
    assert "Automatic rollback is unavailable for this approved change package." in app_js
    assert "appState.plan?.plan?.rollback?.commands" in app_js
    assert "appState.plan?.pipeline?.metadata?.rollback?.commands" not in app_js
    assert "No device write was queued" in app_js


def test_machine_review_draft_rejects_rollback_risk_acknowledgment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = _review_proposal()
    payload["proposed_intent"]["acknowledge_no_rollback"] = True
    _refresh_integrity(payload)

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=payload,
    )

    assert response.status_code == 400
    assert "desired state only" in response.json()["detail"]
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_changes(org_id=ORG_ID) == []
    assert store.list_jobs(org_id=ORG_ID) == []


def test_non_custom_plan_without_rollback_does_not_claim_risk_acceptance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    payload = _proposal()
    request = api.RcaRemediationProposalRequest.model_validate(payload)
    intent = api._intent_from_rca_proposal(request)
    monkeypatch.setattr(
        api,
        "plan_metadata",
        lambda _intent: {
            "risk": "medium",
            "rollback": {"commands": "", "confidence": {}},
            "checks": {"post": []},
            "blast_radius": {
                "devices": [DEVICE_ID],
                "site": "branch",
                "objects": [],
            },
        },
    )

    risk = api._rca_plan_risk_assessment(
        request=request,
        intent=intent,
        pipeline=SimpleNamespace(
            status="pass",
            validation=SimpleNamespace(checks=[]),
        ),
        store=PlatformStore(WorkspacePaths(tmp_path.resolve())),
        org_id=ORG_ID,
    )

    assert risk["rollback_status"] == "unavailable"
    assert risk["rollback_risk_accepted"] is False
    assert any("was not explicitly accepted" in item for item in risk["unknowns"])
    assert not any("reviewer explicitly accepted" in item for item in risk["unknowns"])


def test_review_completion_is_tenant_scoped_and_replay_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    created = client.post("/api/changes/from-rca", json=_review_proposal())
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    completion = {
        "proposed_intent": {
            "change_type": "custom_config",
            "site": "branch",
            "targets": {"device_ids": [DEVICE_ID]},
            "config_lines": "interface Ethernet3\n   description REVIEWED_UPLINK\n",
            "rollback_lines": "interface Ethernet3\n   no description\n",
            "verify_contains": "description REVIEWED_UPLINK",
        },
        "verification_checks": ["Running configuration matches reviewed intent"],
    }

    monkeypatch.setattr(
        api,
        "_request_principal",
        lambda _request: Principal(
            kind="user",
            org_id="org-other",
            role="operator",
            user_id="other-user",
            email="other@example.invalid",
        ),
    )
    cross_tenant = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=completion,
    )
    assert cross_tenant.status_code == 404

    monkeypatch.setattr(
        api,
        "_request_principal",
        lambda _request: Principal(
            kind="user",
            org_id=ORG_ID,
            role="operator",
            user_id="reviewer-user",
            email="reviewer@example.invalid",
        ),
    )
    first = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=completion,
    )
    replay = client.post(
        f"/api/change/{change_id}/complete-rca-draft",
        json=completion,
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 409
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs(org_id=ORG_ID) == []


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
            "ospf_interface",
            {
                "process_id": 1,
                "interface": "Ethernet3",
                "passive": False,
                "current_passive": True,
            },
            "no passive-interface Ethernet3",
            "passive-interface Ethernet3",
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
