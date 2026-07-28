from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import enroll_runner, mint_join_token, preview_pairing
from netcode.store import PlatformStore


def _store(tmp_path: Path) -> PlatformStore:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    return PlatformStore(workspace)


def test_pairing_preview_preserves_exact_identity_without_consuming_code(tmp_path: Path) -> None:
    store = _store(tmp_path)
    minted = mint_join_token(
        store,
        "org-community-1",
        org_id="org-community-1",
        connector_name="acme-windows-01",
        organization_name="Acme Networks, LLC",
        operator_email="owner@acme.example",
    )
    pairing_code = str(minted["join_token"])

    expected = {
        "ok": True,
        "connector_name": "acme-windows-01",
        "organization_name": "Acme Networks, LLC",
        "operator_email": "owner@acme.example",
        "identity_verified": True,
    }
    assert preview_pairing(store, pairing_code) == expected
    assert preview_pairing(store, pairing_code) == expected

    enrolled = enroll_runner(store, pairing_code, "locally-invented-name")
    assert enrolled["ok"] is True
    assert enrolled["connector_name"] == "acme-windows-01"
    assert enrolled["organization_name"] == "Acme Networks, LLC"
    assert enrolled["operator_email"] == "owner@acme.example"
    assert enrolled["identity_verified"] is True
    assert "org_id" not in enrolled

    runner = store.get_runner(str(enrolled["runner_id"]))
    assert runner.name == "acme-windows-01"
    assert runner.organization_name == "Acme Networks, LLC"
    assert runner.operator_email == "owner@acme.example"
    assert runner.identity_verified is True
    assert preview_pairing(store, pairing_code)["ok"] is False
    assert enroll_runner(store, pairing_code, "another-name")["ok"] is False


def test_pairing_identity_metadata_is_all_or_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="must be supplied together"):
        mint_join_token(
            store,
            "pilot",
            connector_name="acme-windows-01",
        )


def test_legacy_pairing_remains_compatible_but_is_not_identity_verified(tmp_path: Path) -> None:
    store = _store(tmp_path)
    minted = mint_join_token(store, "pilot")
    pairing_code = str(minted["join_token"])

    preview = preview_pairing(store, pairing_code)
    assert preview["ok"] is False
    assert preview["error"] == "identity_not_bound"

    enrolled = enroll_runner(store, pairing_code, "legacy-connector")
    assert enrolled["ok"] is True
    assert enrolled["connector_name"] == "legacy-connector"
    assert enrolled["identity_verified"] is False


def test_runner_identity_endpoint_is_exact_redacted_and_revocation_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    minted_response = client.post(
        "/api/runners/join-token",
        json={
            "pool": "org-community-2",
            "connector_name": "contoso-windows-01",
            "organization_name": "Contoso Community",
            "operator_email": "netops@contoso.example",
        },
    )
    assert minted_response.status_code == 200
    pairing_code = str(minted_response.json()["join_token"])

    preview = client.post("/api/runner/pairing/preview", json={"join_token": pairing_code})
    assert preview.status_code == 200
    assert preview.json() == {
        "ok": True,
        "connector_name": "contoso-windows-01",
        "organization_name": "Contoso Community",
        "operator_email": "netops@contoso.example",
        "identity_verified": True,
    }
    serialized_preview = json.dumps(preview.json())
    assert pairing_code not in serialized_preview
    assert "org-community-2" not in serialized_preview

    enrolled = client.post(
        "/api/runner/enroll",
        json={"join_token": pairing_code, "name": "wrong-local-name"},
    ).json()
    assert enrolled["ok"] is True
    token = str(enrolled["runner_token"])

    identity_response = client.get(
        "/api/runner/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert identity_response.status_code == 200
    assert identity_response.json()["connector_name"] == "contoso-windows-01"
    assert identity_response.json()["organization_name"] == "Contoso Community"
    assert identity_response.json()["operator_email"] == "netops@contoso.example"
    assert identity_response.json()["identity_verified"] is True
    serialized_identity = json.dumps(identity_response.json())
    for forbidden in ("runner_token", "hmac_secret", "org_id", "pool"):
        assert forbidden not in serialized_identity

    store = PlatformStore(WorkspacePaths(tmp_path))
    store.revoke_runner(str(enrolled["runner_id"]), "org_default")
    revoked = client.get(
        "/api/runner/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert revoked.status_code == 401
