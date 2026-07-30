from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from netcode import api, entitlements as entitlement_module, runner_hub
from netcode.bootstrap import init_workspace
from netcode.entitlements import PlatformEntitlements
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore


def _headers(
    org_id: str = "org-retail",
    *,
    role: str = "admin",
    user_id: str = "usr_founder",
) -> dict[str, str]:
    return {
        "Authorization": "Bearer trusted-founder-service",
        "X-Rezonance-Org-ID": org_id,
        "X-Rezonance-User": "founder-admin",
        "X-Rezonance-User-ID": user_id,
        "X-Rezonance-Role": role,
    }


def _workspace(tmp_path: Path, monkeypatch) -> tuple[PlatformStore, TestClient]:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "trusted-founder-service")
    monkeypatch.setattr(
        entitlement_module,
        "get_entitlements",
        lambda **_kwargs: PlatformEntitlements(
            plan_id="community",
            platform_available=True,
            max_devices=25,
            max_connectors=1,
            max_workflow_packs=5,
            production_writes=False,
            source="test",
            approval_mode="operator_confirmed",
        ),
    )
    store = PlatformStore(workspace)
    store.ensure_org("org-retail", "Retail Pilot", "retail-pilot")
    return store, TestClient(api.app)


def _legacy_runner(store: PlatformStore, *, org_id: str = "org-retail"):
    return store.create_runner(
        name="windows-gns3-01",
        pool=org_id,
        token_hash=hashlib.sha256(b"current-runner-token").hexdigest(),
        hmac_secret="current-hmac-secret",
        org_id=org_id,
    )


def _issue_claim(client: TestClient, runner_id: str):
    return client.post(
        f"/api/internal/orgs/org-retail/runners/{runner_id}/replacement-claims",
        headers=_headers(),
        json={
            "connector_name": "windows-gns3-01",
            "organization_name": "Retail Pilot",
            "operator_email": "operator@retail.example",
            "replace_pending": True,
        },
    )


def test_repair_is_two_phase_recoverable_and_keeps_one_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = _workspace(tmp_path, monkeypatch)
    runner = _legacy_runner(store)
    issued = _issue_claim(client, runner.id)

    assert issued.status_code == 200
    pairing_code = str(issued.json()["pairing_code"])
    claim_id = str(issued.json()["replacement"]["claim_id"])
    assert pairing_code.startswith("nrc_")
    assert issued.json()["replacement"]["runner_id"] == runner.id
    assert issued.json()["replacement"]["org_id"] == "org-retail"

    preview = client.post(
        "/api/runner/replacement/preview",
        json={"pairing_code": pairing_code},
    )
    assert preview.status_code == 200
    assert preview.json() == {
        "ok": True,
        "state": "pending",
        "expires_at": issued.json()["replacement"]["expires_at"],
        "prepared_at": None,
        "committed_at": None,
        "connector_name": "windows-gns3-01",
        "organization_name": "Retail Pilot",
        "operator_email": "operator@retail.example",
        "identity_verified": True,
    }
    assert "org_id" not in preview.json()
    assert "runner_id" not in preview.json()
    assert "claim_id" not in preview.json()

    wrong_current = client.post(
        "/api/runner/replacement/prepare",
        headers={"Authorization": "Bearer wrong-current-token"},
        json={"pairing_code": pairing_code},
    )
    assert wrong_current.status_code == 401
    assert client.post(
        "/api/runner/replacement/preview",
        json={"pairing_code": pairing_code},
    ).json()["state"] == "pending"

    read_job = store.create_read_job(
        "org-retail",
        runner.pool,
        "verify",
        {"device_id": "edge-1"},
        target_runner_id=runner.id,
    )
    assert store.claim_next_job("org-retail", runner.pool, runner.id) is not None
    running_block = client.post(
        "/api/runner/replacement/prepare",
        json={"pairing_code": pairing_code},
    )
    assert running_block.status_code == 409
    assert "work is running" in running_block.json()["detail"]
    assert client.post(
        "/api/runner/replacement/preview",
        json={"pairing_code": pairing_code},
    ).json()["ok"] is True
    store.update_job(read_job.id, "completed", "read complete", {"ok": True})

    store.create_shell_session(
        session_id="shell-repair-block",
        org_id="org-retail",
        device_id="edge-1",
        display_id="edge-1",
        platform="arista_eos",
        runner_id=runner.id,
        runner_pool=runner.pool,
        transcript_path=str(tmp_path / "shell.jsonl"),
        status="active",
    )
    shell_block = client.post(
        "/api/runner/replacement/prepare",
        json={"pairing_code": pairing_code},
    )
    assert shell_block.status_code == 409
    assert "Shell session is open" in shell_block.json()["detail"]
    store.update_shell_session(
        "shell-repair-block",
        status="closed",
        ended=True,
        end_reason="test complete",
    )

    prepared = client.post(
        "/api/runner/replacement/prepare",
        headers={"Authorization": "Bearer current-runner-token"},
        json={"pairing_code": pairing_code},
    )
    assert prepared.status_code == 200
    prepared_body = prepared.json()
    first_pending_token = str(prepared_body["runner_token"])
    assert first_pending_token.startswith("nrt_")
    assert prepared_body["hmac_secret"]
    assert prepared_body["runner_id"] == runner.id
    assert prepared_body["connector_name"] == "windows-gns3-01"
    assert pairing_code not in json.dumps(prepared_body)

    # A staged credential may confirm the repair, but it cannot poll, sync, or
    # read connector metadata before the atomic commit.
    assert client.get(
        "/api/runner/me",
        headers={"Authorization": f"Bearer {first_pending_token}"},
    ).status_code == 401
    with client.websocket_connect("/api/runner/stream") as websocket:
        websocket.send_json({"token": first_pending_token})
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
    assert closed.value.code == 4401

    # A lost prepare response can be recovered with the still-unconsumed
    # founder-bound claim. The retry replaces only the staged credential.
    recovered_prepare = client.post(
        "/api/runner/replacement/prepare",
        json={"pairing_code": pairing_code},
    )
    assert recovered_prepare.status_code == 200
    pending_token = str(recovered_prepare.json()["runner_token"])
    assert pending_token != first_pending_token

    stale_pending = client.post(
        "/api/runner/replacement/commit",
        headers={"Authorization": f"Bearer {first_pending_token}"},
        json={"pairing_code": pairing_code},
    )
    assert stale_pending.status_code == 401
    assert client.post(
        "/api/runner/replacement/preview",
        json={"pairing_code": pairing_code},
    ).json()["state"] == "prepared"

    committed = client.post(
        "/api/runner/replacement/commit",
        headers={"Authorization": f"Bearer {pending_token}"},
        json={"pairing_code": pairing_code},
    )
    assert committed.status_code == 200
    assert committed.json()["state"] == "committed"
    assert committed.json()["already_committed"] is False
    assert committed.json()["runner_id"] == runner.id
    assert store.active_runner_count("org-retail") == 1
    assert len(store.list_runners(org_id="org-retail")) == 1

    assert client.get(
        "/api/runner/me",
        headers={"Authorization": "Bearer current-runner-token"},
    ).status_code == 401
    repaired_identity = client.get(
        "/api/runner/me",
        headers={"Authorization": f"Bearer {pending_token}"},
    )
    assert repaired_identity.status_code == 200
    assert repaired_identity.json()["organization_name"] == "Retail Pilot"
    assert repaired_identity.json()["operator_email"] == "operator@retail.example"
    assert repaired_identity.json()["identity_verified"] is True

    replay = client.post(
        "/api/runner/replacement/commit",
        headers={"Authorization": f"Bearer {pending_token}"},
        json={"pairing_code": pairing_code},
    )
    assert replay.status_code == 200
    assert replay.json()["already_committed"] is True
    assert client.post(
        "/api/runner/replacement/preview",
        json={"pairing_code": pairing_code},
    ).json()["error"] == "repair_code_consumed"

    status = client.get(
        f"/api/internal/orgs/org-retail/runners/{runner.id}/replacement-claims/latest",
        headers=_headers(),
    )
    assert status.status_code == 200
    assert status.json()["replacement"]["claim_id"] == claim_id
    assert status.json()["replacement"]["consumed"] is True
    assert status.json()["replacement"]["state"] == "committed"
    assert {
        event["event"] for event in status.json()["audit_events"]
    } >= {
        "identity_repair_claim_issued",
        "identity_repair_prepared",
        "identity_repair_committed",
    }
    serialized_status = json.dumps(status.json())
    for forbidden in (
        pairing_code,
        pending_token,
        first_pending_token,
        "current-runner-token",
        "current-hmac-secret",
        prepared_body["hmac_secret"],
        "token_hash",
        "hmac_secret",
    ):
        assert str(forbidden) not in serialized_status


def test_repair_claim_fails_closed_on_scope_name_role_and_ambiguity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = _workspace(tmp_path, monkeypatch)
    runner = _legacy_runner(store)
    payload = {
        "connector_name": "windows-gns3-01",
        "organization_name": "Retail Pilot",
        "operator_email": "operator@retail.example",
    }

    assert client.post(
        f"/api/internal/orgs/org-retail/runners/{runner.id}/replacement-claims",
        headers=_headers(role="operator"),
        json=payload,
    ).status_code == 403
    assert client.post(
        f"/api/internal/orgs/org-retail/runners/{runner.id}/replacement-claims",
        headers=_headers(org_id="org-other"),
        json=payload,
    ).status_code == 404

    wrong_name = client.post(
        f"/api/internal/orgs/org-retail/runners/{runner.id}/replacement-claims",
        headers=_headers(),
        json={**payload, "connector_name": "different-connector"},
    )
    assert wrong_name.status_code == 403

    store.create_runner(
        name="duplicate-connector",
        pool="org-retail",
        token_hash=hashlib.sha256(b"duplicate-token").hexdigest(),
        hmac_secret="duplicate-hmac",
        org_id="org-retail",
    )
    monkeypatch.setattr(
        runner_hub,
        "enforce_capacity",
        lambda *_args, **_kwargs: PlatformEntitlements(
            plan_id="test",
            platform_available=True,
            max_devices=25,
            max_connectors=10,
            max_workflow_packs=5,
            production_writes=False,
            source="test",
        ),
    )
    ambiguous = client.post(
        f"/api/internal/orgs/org-retail/runners/{runner.id}/replacement-claims",
        headers=_headers(),
        json=payload,
    )
    assert ambiguous.status_code == 409
    assert "exactly one unambiguous" in ambiguous.json()["detail"]
    assert store.latest_runner_replacement_claim("org-retail", runner.id) is None
