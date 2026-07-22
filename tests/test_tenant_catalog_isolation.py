from __future__ import annotations

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import enroll_runner, mint_join_token
from netcode.store import PlatformStore


def _headers(org_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer service-admin",
        "X-Rezonance-Org-ID": org_id,
        "X-Rezonance-User-ID": f"usr-{org_id}",
        "X-Rezonance-User": f"operator@{org_id}.invalid",
        "X-Rezonance-Role": "operator",
    }


def test_new_org_catalog_is_empty_while_original_org_retains_25_devices(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "service-admin")
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    join = mint_join_token(store, "production", org_id="org_default")
    enrolled = enroll_runner(store, join["join_token"], "windows-gns3-01")
    runner = store.get_runner(enrolled["runner_id"])
    assert runner is not None
    store.sync_runner_devices(
        runner,
        [
            {
                "id": f"edge-{index}",
                "hostname": f"EDGE-{index}",
                "host": f"192.0.2.{index + 1}",
                "platform": "arista_eos",
                "site": "hq",
                "role": "edge",
            }
            for index in range(25)
        ],
        revision="production-25",
    )

    client = TestClient(api.app)
    isolated = client.get("/api/devices", headers=_headers("org-isolated"))
    production = client.get("/api/devices", headers=_headers("org_default"))

    assert isolated.status_code == 200
    assert isolated.json()["total"] == 0
    assert isolated.json()["devices"] == []
    assert production.status_code == 200
    assert production.json()["total"] == 25
    assert len(production.json()["devices"]) == 25
