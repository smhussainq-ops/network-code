from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode import entitlements as entitlement_module
from netcode import runner_hub
from netcode.auth import hash_password, token_hash
from netcode.bootstrap import init_workspace
from netcode.entitlements import EntitlementError, PlatformEntitlements
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore
from netcode.workflow_packs import workflow_pack_catalog


def _suspended(**_kwargs):
    raise EntitlementError("The platform license is not active.")


def _community_entitlements(**_kwargs) -> PlatformEntitlements:
    return PlatformEntitlements(
        plan_id="community",
        platform_available=True,
        max_devices=25,
        max_connectors=1,
        max_workflow_packs=1,
        production_writes=True,
        source="test_authority",
    )


def test_suspension_blocks_shell_before_session_creation(tmp_path: Path, monkeypatch) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "get_entitlements", _suspended)
    api._SHELL_SESSIONS.clear()

    response = TestClient(api.app).post("/api/shell/open", json={"device_id": "v2-store1"})

    assert response.status_code == 403
    assert response.json()["error"] == "plan_limit_reached"
    assert api._SHELL_SESSIONS == {}


def test_suspension_closes_an_already_active_shell(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.close_code: int | None = None

        async def send_json(self, message: dict[str, object]) -> None:
            self.messages.append(message)

        async def close(self, *, code: int) -> None:
            self.close_code = code

    calls: list[tuple[str, bool]] = []

    def revoked(*, org_id: str, force: bool = False):
        calls.append((org_id, force))
        raise EntitlementError("The platform license is not active.")

    monkeypatch.setattr(api, "get_entitlements", revoked)
    websocket = FakeWebSocket()

    asyncio.run(
        api._shell_entitlement_watchdog(
            websocket,
            "org-suspended",
            interval_seconds=0,
        )
    )

    assert calls == [("org-suspended", True)]
    assert websocket.close_code == 4403
    assert websocket.messages == [{
        "t": "status",
        "s": "license_suspended",
        "m": "This Shell session ended because the organization license is not active.",
    }]


def test_suspension_leaves_connector_job_queued_and_unclaimed(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(workspace)
    token = "nrt_suspended-connector"
    runner = store.create_runner(
        name="connector-a",
        pool="default",
        token_hash=token_hash(token),
        hmac_secret="test-secret",
        org_id="org_default",
    )
    change = store.create_change(workspace.intents / "examples" / "add_guest_vlan.yaml", "v2-store1")
    job = store.create_job(change.id, "read_rez_ssh")
    monkeypatch.setattr(api, "get_entitlements", _suspended)

    response = TestClient(api.app).post(
        "/api/runner/poll",
        headers={"Authorization": f"Bearer {token}"},
        json={"wait_seconds": 0},
    )

    assert response.status_code == 403
    stored = store.get_job(job.id)
    assert stored.status == "queued"
    assert stored.claimed_by is None
    assert runner.org_id == stored.org_id


def test_shell_websocket_rejects_authenticated_user_from_another_org(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_AUTH", "1")
    store = PlatformStore(workspace)
    store.ensure_org("org_a", "A", "a")
    store.ensure_org("org_b", "B", "b")
    store.create_user("org_b", "operator@b.example", hash_password("operator-password"), role="operator")
    api._SHELL_SESSIONS["session-org-a"] = {
        "org_id": "org_a",
        "device_id": "edge-a",
        "runner_id": "connector-a",
        "runner_pool": "default",
        "state": {},
    }
    client = TestClient(api.app)
    login = client.post(
        "/api/auth/login",
        json={"email": "operator@b.example", "password": "operator-password", "org_id": "org_b"},
    )
    assert login.status_code == 200

    try:
        with client.websocket_connect("/api/shell/session/session-org-a") as websocket:
            message = websocket.receive_json()
            assert message == {"t": "status", "s": "error", "m": "Unknown or expired session."}
    finally:
        api._SHELL_SESSIONS.clear()


def test_community_catalog_exposes_exactly_one_workflow_pack(tmp_path: Path, monkeypatch) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "get_entitlements", _community_entitlements)

    response = TestClient(api.app).get("/api/workflow-packs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["entitled_count"] == 1
    assert payload["available_count"] == 4
    assert [pack["id"] for pack in payload["packs"]] == ["golden-baseline-standardization"]
    assert len(workflow_pack_catalog(1)["packs"]) == 1


def test_community_paid_workflow_cannot_bypass_catalog_with_direct_plan(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "get_entitlements", _community_entitlements)
    client = TestClient(api.app)

    catalog = client.get("/api/desired-state/catalog")
    response = client.post(
        "/api/desired-state/plan",
        json={
            "change_type": "add_vlan",
            "site": "site-101",
            "device_id": "access-sw-01",
            "requested_by": "marcus",
            "values": {"vlan_id": 3980, "name": "MARCUS"},
        },
    )

    assert catalog.status_code == 200
    assert {item["id"] for item in catalog.json()["change_types"]} == {"ntp_standardize", "custom_config"}
    assert response.status_code == 403
    assert response.json()["error"] == "plan_limit_reached"
    assert PlatformStore(workspace).list_changes() == []


def test_community_device_26_is_rejected_without_corrupting_first_25(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entitlement_module, "get_entitlements", _community_entitlements)
    store = PlatformStore(workspace)
    token = "nrt_community-device-boundary"
    runner = store.create_runner(
        name="connector-a",
        pool="default",
        token_hash=token_hash(token),
        hmac_secret="test-secret",
        org_id="org_default",
    )
    client = TestClient(api.app)
    headers = {"Authorization": f"Bearer {token}"}

    accepted = client.post(
        "/api/runner/inventory-sync",
        headers=headers,
        json={
            "revision": "first-25",
            "replace": True,
            "devices": [
                {
                    "id": f"access-sw-{index:02d}",
                    "hostname": f"access-sw-{index:02d}",
                    "host": f"192.0.2.{index}",
                    "platform": "cisco_iosxe",
                    "site": "site-101",
                }
                for index in range(1, 26)
            ],
        },
    )
    rejected = client.post(
        "/api/runner/inventory-sync",
        headers=headers,
        json={
            "revision": "device-26",
            "replace": False,
            "devices": [
                {
                    "id": "access-sw-26",
                    "hostname": "access-sw-26",
                    "host": "192.0.2.26",
                    "platform": "cisco_iosxe",
                    "site": "site-101",
                }
            ],
        },
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 403
    assert rejected.json()["error"] == "plan_limit_reached"
    assert store.catalog_device_count("org_default", runner_id=runner.id) == 25
    assert store.resolve_device("org_default", "access-sw-01") is not None
    assert store.resolve_device("org_default", "access-sw-25") is not None
    assert store.resolve_device("org_default", "access-sw-26") is None


def test_community_second_connector_fails_after_join_token_claim(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.setattr(entitlement_module, "get_entitlements", _community_entitlements)
    store = PlatformStore(workspace)
    store.create_runner(
        name="connector-a",
        pool="default",
        token_hash="first-token-hash",
        hmac_secret="first-secret",
        org_id="org_default",
    )
    join = runner_hub.mint_join_token(store, "default", org_id="org_default")

    result = runner_hub.enroll_runner(store, join["join_token"], "connector-b")

    assert result["ok"] is False
    assert result["error"] == "connector_limit_reached"
    assert len(store.list_runners(org_id="org_default")) == 1


def test_trusted_rez_proxy_binds_request_to_forwarded_organization(tmp_path: Path, monkeypatch) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "private-rez-service-token")
    seen: list[str] = []

    def scoped_entitlements(*, org_id: str, **_kwargs) -> PlatformEntitlements:
        seen.append(org_id)
        return _community_entitlements()

    monkeypatch.setattr(api, "get_entitlements", scoped_entitlements)
    response = TestClient(api.app).get(
        "/api/workflow-packs",
        headers={
            "Authorization": "Bearer private-rez-service-token",
            "X-Rezonance-Org-ID": "org-retail-a",
            "X-Rezonance-User": "marcus@example.com",
            "X-Rezonance-Role": "operator",
        },
    )

    assert response.status_code == 200
    assert seen == ["org-retail-a"]


def test_forged_rez_organization_header_is_rejected(tmp_path: Path, monkeypatch) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "private-rez-service-token")

    response = TestClient(api.app).get(
        "/api/workflow-packs",
        headers={
            "Authorization": "Bearer ordinary-browser-token",
            "X-Rezonance-Org-ID": "org-victim",
            "X-Rezonance-Role": "admin",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Trusted Rez service token is invalid."


def test_trusted_rez_identity_is_the_approval_actor(monkeypatch) -> None:
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "private-rez-service-token")
    principal = api._trusted_rez_service_principal(
        {
            "x-rezonance-org-id": "org-retail-a",
            "x-rezonance-user": "marcus@example.com",
            "x-rezonance-role": "operator",
        },
        "Bearer private-rez-service-token",
    )

    assert api._approver_identity(principal, "spoofed-name", "requester@example.com", None) == "marcus@example.com"
