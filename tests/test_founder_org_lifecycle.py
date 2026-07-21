from __future__ import annotations

import hashlib
from types import SimpleNamespace

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore


def _headers(org_id: str = "org-retail", *, role: str = "admin", user_id: str = "usr_founder") -> dict[str, str]:
    return {
        "Authorization": "Bearer trusted-rez-service",
        "X-Rezonance-Org-ID": org_id,
        "X-Rezonance-User": "founder-admin",
        "X-Rezonance-User-ID": user_id,
        "X-Rezonance-Role": role,
    }


def _workspace(tmp_path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "trusted-rez-service")
    store = PlatformStore(workspace)
    store.ensure_org("org-retail", "Retail", "retail")
    return workspace, store


def _runner(store: PlatformStore):
    return store.create_runner(
        "retail-windows-01",
        "org-retail",
        hashlib.sha256(b"runner-token").hexdigest(),
        "runner-hmac",
        org_id="org-retail",
    )


def test_suspend_is_org_scoped_and_fail_closed_across_jobs_shell_and_pairing(tmp_path, monkeypatch) -> None:
    workspace, store = _workspace(tmp_path, monkeypatch)
    runner = _runner(store)
    store.create_join_token(hashlib.sha256(b"unused-pairing").hexdigest(), "org-retail", org_id="org-retail")
    queued = store.create_read_job("org-retail", runner.pool, "verify", {"device_id": "edge-1"})
    store.create_shell_session(
        session_id="shell-retail",
        org_id="org-retail",
        device_id="edge-1",
        display_id="edge-1",
        platform="arista_eos",
        runner_id=runner.id,
        runner_pool=runner.pool,
        transcript_path=str(tmp_path / "shell-retail.jsonl"),
        status="active",
    )
    api._SHELL_SESSIONS["shell-retail"] = {
        "org_id": "org-retail",
        "runner_id": runner.id,
        "device_id": "edge-1",
        "state": {},
    }

    try:
        response = TestClient(api.app).post(
            "/api/internal/orgs/org-retail/suspend",
            headers=_headers(),
        )
    finally:
        api._SHELL_SESSIONS.clear()

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["invalidated_pairing_codes"] == 1
    assert PlatformStore(workspace).get_job(queued.id).status == "cancelled"
    assert PlatformStore(workspace).get_runner(runner.id).drain_requested is True
    assert PlatformStore(workspace).get_shell_session("shell-retail")["status"] == "terminated"
    assert PlatformStore(workspace).consume_join_token(hashlib.sha256(b"unused-pairing").hexdigest()) is None


def test_lifecycle_rejects_missing_identity_non_admin_and_cross_org(tmp_path, monkeypatch) -> None:
    _workspace(tmp_path, monkeypatch)
    client = TestClient(api.app)

    no_identity = client.post(
        "/api/internal/orgs/org-retail/suspend",
        headers=_headers(user_id=""),
    )
    non_admin = client.post(
        "/api/internal/orgs/org-retail/suspend",
        headers=_headers(role="operator"),
    )
    cross_org = client.post(
        "/api/internal/orgs/org-victim/suspend",
        headers=_headers(org_id="org-retail"),
    )

    assert no_identity.status_code == 401
    assert non_admin.status_code == 403
    assert cross_org.status_code == 404


def test_reactivate_requires_fresh_entitlement_and_resumes_connector(tmp_path, monkeypatch) -> None:
    workspace, store = _workspace(tmp_path, monkeypatch)
    runner = _runner(store)
    store.set_runner_drain(runner.id, "org-retail", requested=True)
    calls: list[tuple[str, bool]] = []

    def active_entitlement(*, org_id: str, force: bool = False):
        calls.append((org_id, force))
        return SimpleNamespace(plan_id="community")

    monkeypatch.setattr(api, "get_entitlements", active_entitlement)
    response = TestClient(api.app).post(
        "/api/internal/orgs/org-retail/reactivate",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["resumed_connectors"] == 1
    assert calls == [("org-retail", True)]
    assert PlatformStore(workspace).get_runner(runner.id).drain_requested is False


def test_revoke_permanently_revokes_connector_and_unused_pairing(tmp_path, monkeypatch) -> None:
    workspace, store = _workspace(tmp_path, monkeypatch)
    runner = _runner(store)
    pairing_hash = hashlib.sha256(b"unused-pairing").hexdigest()
    store.create_join_token(pairing_hash, "org-retail", org_id="org-retail")

    class Channel:
        closed_with: int | None = None

        async def close(self, *, code: int) -> None:
            self.closed_with = code

    channel = Channel()
    api._RUNNER_CHANNELS[runner.id] = channel
    api._RUNNER_CHANNEL_POOLS[runner.id] = runner.pool

    try:
        response = TestClient(api.app).post(
            "/api/internal/orgs/org-retail/revoke",
            headers=_headers(),
        )
    finally:
        api._RUNNER_CHANNELS.clear()
        api._RUNNER_CHANNEL_POOLS.clear()

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["invalidated_pairing_codes"] == 1
    assert PlatformStore(workspace).get_runner(runner.id).revoked_at
    assert PlatformStore(workspace).consume_join_token(pairing_hash) is None
    assert channel.closed_with == 4403


def test_revoke_drains_claimed_work_before_revoking_connector_identity(tmp_path, monkeypatch) -> None:
    workspace, store = _workspace(tmp_path, monkeypatch)
    runner = _runner(store)
    job = store.create_read_job("org-retail", runner.pool, "verify", {"device_id": "edge-1"})
    assert store.claim_next_job("org-retail", runner.pool, runner.id) is not None

    channel = object()
    api._RUNNER_CHANNELS[runner.id] = channel
    api._RUNNER_CHANNEL_POOLS[runner.id] = runner.pool
    try:
        response = TestClient(api.app).post(
            "/api/internal/orgs/org-retail/revoke",
            headers=_headers(),
        )
        assert api._RUNNER_CHANNELS.get(runner.id) is channel
    finally:
        api._RUNNER_CHANNELS.clear()
        api._RUNNER_CHANNEL_POOLS.clear()

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["state"] == "revoking"
    assert response.json()["running_jobs"] == 1
    assert response.json()["revoked_connectors"] == 0
    assert PlatformStore(workspace).get_job(job.id).status == "running"
    persisted_runner = PlatformStore(workspace).get_runner(runner.id)
    assert persisted_runner.drain_requested is True
    assert persisted_runner.revoked_at is None


def test_replacing_pairing_code_invalidates_the_previous_unused_code(tmp_path, monkeypatch) -> None:
    workspace, store = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "enforce_capacity", lambda *_args, **_kwargs: None)
    client = TestClient(api.app)
    first = client.post(
        "/api/runners/join-token",
        headers=_headers(),
        json={"pool": "org-retail", "replace_unused": True},
    ).json()["join_token"]
    second = client.post(
        "/api/runners/join-token",
        headers=_headers(),
        json={"pool": "org-retail", "replace_unused": True},
    ).json()["join_token"]

    assert first != second
    assert PlatformStore(workspace).consume_join_token(hashlib.sha256(first.encode()).hexdigest()) is None
    assert PlatformStore(workspace).consume_join_token(hashlib.sha256(second.encode()).hexdigest()) == {
        "pool": "org-retail",
        "org_id": "org-retail",
    }
