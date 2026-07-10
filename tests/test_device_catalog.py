from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api, runner_agent
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import enroll_runner, mint_join_token
from netcode.store import DEFAULT_ORG_ID, PlatformStore
from netcode.yamlio import write_yaml


def _runner(store: PlatformStore, name: str = "connector-1", pool: str = "pilot"):
    join = mint_join_token(store, pool)
    enrolled = enroll_runner(store, join["join_token"], name)
    return store.get_runner(enrolled["runner_id"]), enrolled["runner_token"]


def _device(device_id: str, host: str, *, site: str = "Site-101", role: str = "edge") -> dict:
    return {
        "id": device_id,
        "hostname": device_id.upper(),
        "host": host,
        "port": 22,
        "platform": "arista_eos",
        "site": site,
        "role": role,
        "groups": ["production"],
        "aliases": [f"alias-{device_id}"],
    }


def test_catalog_resolves_canonical_id_hostname_ip_fqdn_and_alias(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    runner, _ = _runner(store)

    store.sync_runner_devices(
        runner,
        [_device("v2-hq-core", "core01.example.net")],
        revision="rev-1",
    )

    for identifier in ("v2-hq-core", "V2-HQ-CORE", "core01.example.net", "core01.example.net:22", "alias-v2-hq-core"):
        resolved = store.resolve_device(DEFAULT_ORG_ID, identifier)
        assert resolved is not None
        assert resolved["canonical_id"] == "v2-hq-core"
        assert resolved["runner_id"] == runner.id
    saved = store.devices_by_identifiers(DEFAULT_ORG_ID, ["alias-v2-hq-core", "v2-hq-core"])
    assert [item["canonical_id"] for item in saved] == ["v2-hq-core"]
    assert "password" not in json.dumps(store.query_devices(DEFAULT_ORG_ID)).lower()

    store.sync_runner_devices(
        runner,
        [_device("edge-new", "192.0.2.99")],
        revision="discovery-one",
        replace=False,
    )
    refreshed_runner = store.get_runner(runner.id)
    assert refreshed_runner.inventory_revision == "rev-1"
    assert refreshed_runner.device_count == 1
    assert store.query_devices(DEFAULT_ORG_ID)["total"] == 2


def test_runner_inventory_sync_rejects_credentials(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    store = PlatformStore(WorkspacePaths(tmp_path))
    _, token = _runner(store)

    response = client.post(
        "/api/runner/inventory-sync",
        headers={"Authorization": f"Bearer {token}"},
        json={"revision": "bad", "devices": [{**_device("edge-1", "192.0.2.1"), "password": "must-not-leak"}]},
    )

    assert response.status_code == 400
    assert "forbidden credential" in response.json()["detail"]
    assert store.query_devices(DEFAULT_ORG_ID)["total"] == 0

    disguised = client.post(
        "/api/runner/inventory-sync",
        headers={"Authorization": f"Bearer {token}"},
        json={"revision": "bad-2", "devices": [{**_device("edge-1", "192.0.2.1"), "aliases": [{"password": "hidden"}]}]},
    )
    assert disguised.status_code == 400
    assert "strings only" in disguised.json()["detail"]


def test_duplicate_canonical_id_cannot_be_stolen_by_another_connector(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    first, _ = _runner(store, "connector-a", "shared")
    second, _ = _runner(store, "connector-b", "shared")
    store.sync_runner_devices(first, [_device("edge-1", "192.0.2.1")], revision="a")

    result = store.sync_runner_devices(second, [_device("edge-1", "198.51.100.1")], revision="b")

    assert len(result["conflicts"]) == 1
    resolved = store.resolve_device(DEFAULT_ORG_ID, "edge-1")
    assert resolved is not None
    assert resolved["runner_id"] == first.id
    assert resolved["host"] == "192.0.2.1"


def test_shell_open_routes_catalog_device_to_exact_connector(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    store = PlatformStore(workspace)
    first, _ = _runner(store, "connector-a", "shared")
    second, _ = _runner(store, "connector-b", "shared")
    store.sync_runner_devices(first, [_device("edge-a", "192.0.2.10")], revision="a")
    store.sync_runner_devices(second, [_device("v2-hq-core", "192.0.2.20")], revision="b")
    api._RUNNER_CHANNELS.clear()
    api._RUNNER_CHANNEL_POOLS.clear()
    api._RUNNER_CHANNELS[first.id] = object()  # type: ignore[assignment]
    api._RUNNER_CHANNELS[second.id] = object()  # type: ignore[assignment]
    api._RUNNER_CHANNEL_POOLS.update({first.id: first.pool, second.id: second.pool})
    try:
        response = TestClient(api.app).post("/api/shell/open", json={"device_id": "V2-HQ-CORE"})
        assert response.status_code == 200
        body = response.json()
        assert body["device_id"] == "v2-hq-core"
        assert body["runner_id"] == second.id
        assert api._SHELL_SESSIONS[body["session_id"]]["runner_id"] == second.id
    finally:
        api._RUNNER_CHANNELS.clear()
        api._RUNNER_CHANNEL_POOLS.clear()
        api._SHELL_SESSIONS.clear()


def test_device_search_is_metadata_only_and_marks_only_live_connector_connectable(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(workspace)
    runner, _ = _runner(store)
    store.sync_runner_devices(runner, [_device("v2-hq-core", "192.0.2.20")], revision="live")
    api._RUNNER_CHANNELS.clear()
    before_jobs = len(store.list_jobs())
    client = TestClient(api.app)

    offline = client.get("/api/devices", params={"q": "hq", "limit": 500})
    assert offline.status_code == 422  # API enforces the public 50-row cap.
    result = client.get("/api/devices", params={"q": "hq", "limit": 50}).json()
    assert result["returned"] == 1
    assert result["devices"][0]["connectable"] is False
    assert result["device_connections_opened"] == 0
    assert len(PlatformStore(workspace).list_jobs()) == before_jobs

    api._RUNNER_CHANNELS[runner.id] = object()  # type: ignore[assignment]
    try:
        online = client.get("/api/devices", params={"q": "hq"}).json()
        assert online["devices"][0]["connectable"] is True
    finally:
        api._RUNNER_CHANNELS.clear()


def test_catalog_handles_ten_thousand_devices_with_bounded_pages(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    runner, _ = _runner(store)
    devices = [
        _device(
            f"edge-{index:05d}",
            f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}",
            site=f"Site-{index % 100:03d}",
            role="core" if index % 10 == 0 else "edge",
        )
        for index in range(10_000)
    ]
    store.sync_runner_devices(runner, devices, revision="scale-10k")

    started = time.perf_counter()
    result = store.query_devices(DEFAULT_ORG_ID, query="edge-09999", limit=50)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert result["total"] == 1
    assert result["returned"] == 1

    first = store.query_devices(DEFAULT_ORG_ID, limit=50)
    second = store.query_devices(DEFAULT_ORG_ID, cursor=first["next_cursor"], limit=50)
    assert first["returned"] == 50
    assert second["returned"] == 50
    assert {item["canonical_id"] for item in first["devices"]}.isdisjoint(
        {item["canonical_id"] for item in second["devices"]}
    )


def test_runner_public_inventory_snapshot_never_contains_secrets(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "inventory.yaml"
    write_yaml(inventory_path, {
        "defaults": {"username": "admin", "password": "default-secret"},
        "devices": [{
            **_device("v2-hq-core", "192.0.2.20"),
            "username": "device-admin",
            "password": "device-secret",
            "api_token": "token-secret",
        }],
    })
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory_path)

    snapshot = runner_agent._public_inventory_snapshot()
    serialized = json.dumps(snapshot).lower()
    assert snapshot["devices"][0]["id"] == "v2-hq-core"
    assert "username" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
    assert "api_token" not in serialized
