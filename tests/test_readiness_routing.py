from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import enroll_runner, mint_join_token
from netcode.store import PlatformStore


def _runner(store: PlatformStore, *, org_id: str, name: str):
    join = mint_join_token(store, name, org_id=org_id)
    enrolled = enroll_runner(store, join["join_token"], name)
    store.touch_runner(enrolled["runner_id"], status="online")
    return store.get_runner(enrolled["runner_id"])


def _device(device_id: str) -> dict[str, object]:
    return {
        "id": device_id,
        "hostname": device_id,
        "host": "192.0.2.11" if device_id.endswith("2") else "192.0.2.10",
        "port": 22,
        "platform": "arista_eos",
        "site": "pilot",
        "role": "edge",
        "groups": [],
        "aliases": [],
    }


def test_selected_readiness_resolves_only_the_tenant_device_owner(tmp_path: Path) -> None:
    store = PlatformStore(WorkspacePaths(tmp_path))
    init_workspace(WorkspacePaths(tmp_path))
    first = _runner(store, org_id="org_default", name="first")
    other = _runner(store, org_id="org_other", name="other")
    store.sync_runner_devices(first, [_device("edge-1")], revision="one")
    store.sync_runner_devices(other, [_device("edge-other")], revision="two")

    runner_id, error = api._resolve_selected_device_runner(store, "org_default", ["edge-1"])

    assert error is None
    assert runner_id == first.id
    assert runner_id != other.id
    assert api._resolve_selected_device_runner(store, "org_default", ["edge-other"])[0] is None


def test_selected_readiness_rejects_unknown_mixed_and_offline_targets(tmp_path: Path) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    first = _runner(store, org_id="org_default", name="first")
    second = _runner(store, org_id="org_default", name="second")
    store.sync_runner_devices(first, [_device("edge-1")], revision="one")
    store.sync_runner_devices(second, [_device("edge-2")], revision="two")

    assert "not assigned" in str(api._resolve_selected_device_runner(store, "org_default", ["missing"])[1])
    assert "span multiple" in str(api._resolve_selected_device_runner(store, "org_default", ["edge-1", "edge-2"])[1])
    store.touch_runner(first.id, status="offline")
    assert "not online" in str(api._resolve_selected_device_runner(store, "org_default", ["edge-1"])[1])


def test_readiness_endpoint_assigns_the_selected_runner(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    store = PlatformStore(workspace)
    runner = _runner(store, org_id="org_default", name="selected")
    store.sync_runner_devices(runner, [_device("edge-1")], revision="one")
    observed: dict[str, object] = {}

    def fake_read(paths, action, payload, org_id, timeout=60.0, *, change_id=None, target_runner_id=None):  # noqa: ANN001
        observed.update({"action": action, "payload": payload, "org_id": org_id, "target_runner_id": target_runner_id})
        return {"ok": True, "requested": 1, "tested": 1, "readable": 1, "devices": [{"id": "edge-1", "ok": True}]}

    monkeypatch.setattr(api, "_runner_read", fake_read)
    response = TestClient(api.app).post("/api/readiness/devices", json={"device_ids": ["edge-1"]})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert observed["target_runner_id"] == runner.id
    assert observed["payload"] == {"device_ids": ["edge-1"]}
