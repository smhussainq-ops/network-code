from __future__ import annotations

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths


def test_rez_discovery_bridge_routes_to_selected_connector_and_preserves_job_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_REZ_BRIDGE_TOKEN", "bridge-token")
    init_workspace(WorkspacePaths(tmp_path))
    observed = {}

    def fake_runner_read(p, action, payload, org_id, timeout=60.0, *, change_id=None, target_runner_id=None):  # noqa: ANN001
        observed.update({
            "action": action,
            "payload": dict(payload),
            "org_id": org_id,
            "timeout": timeout,
            "target_runner_id": target_runner_id,
        })
        return {
            "ok": True,
            "_job_id": "job-1",
            "_runner_id": "connector-1",
            "device_states": {"core-1": {"device": {"hostname": "core-1"}}},
            "source_of_truth_candidates": [{"id": "core-1", "host": "10.20.0.10"}],
        }

    monkeypatch.setattr(api, "_runner_read", fake_runner_read)
    response = TestClient(api.app).post(
        "/api/rez/runner-read",
        headers={"Authorization": "Bearer bridge-token"},
        json={
            "action": "rez_discover_network",
            "timeout": 600,
            "payload": {
                "seed_node": "core-1",
                "depth": 2,
                "connector_id": "connector-1",
                "allowed_cidrs": ["10.20.0.0/24"],
            },
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["_job_id"] == "job-1"
    assert body["candidate_disposition"]["status"] == "review_required"
    assert body["candidate_disposition"]["source_of_truth_written"] is False
    assert observed["action"] == "rez_discover_network"
    assert observed["target_runner_id"] == "connector-1"
    assert observed["timeout"] == 600
    assert "connector_id" not in observed["payload"]


def test_rez_discovery_bridge_strips_credential_shaped_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_REZ_BRIDGE_TOKEN", "bridge-token")
    init_workspace(WorkspacePaths(tmp_path))
    observed = {}

    def fake_runner_read(p, action, payload, org_id, timeout=60.0, **kwargs):  # noqa: ANN001
        observed.update(payload)
        return {"ok": False, "error": "expected test failure"}

    monkeypatch.setattr(api, "_runner_read", fake_runner_read)
    response = TestClient(api.app).post(
        "/api/rez/runner-read",
        headers={"Authorization": "Bearer bridge-token"},
        json={
            "action": "rez_discover_network",
            "payload": {
                "seed_node": "10.20.0.10",
                "username": "must-not-queue",
                "password": "must-not-queue",
                "api_token": "must-not-queue",
            },
        },
    )

    assert response.status_code == 200
    assert "username" not in observed
    assert "password" not in observed
    assert "api_token" not in observed


def test_production_rez_bridge_requires_and_uses_authenticated_org_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_REZ_BRIDGE_TOKEN", "bridge-token")
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setattr(api, "_PRODUCTION_RUNTIME", True)
    init_workspace(WorkspacePaths(tmp_path))
    observed = {}

    def fake_runner_read(p, action, payload, org_id, timeout=60.0, **kwargs):  # noqa: ANN001
        observed.update({"org_id": org_id, "action": action})
        return {"ok": False, "error": "no connector in isolated org"}

    monkeypatch.setattr(api, "_runner_read", fake_runner_read)
    client = TestClient(api.app)
    unscoped = client.post(
        "/api/rez/runner-read",
        headers={"Authorization": "Bearer bridge-token"},
        json={"action": "rez_ssh_command", "payload": {"device": "core-1", "command": "show version"}},
    )
    assert unscoped.status_code == 401

    scoped = client.post(
        "/api/rez/runner-read",
        headers={
            "Authorization": "Bearer bridge-token",
            "X-Rezonance-Org-ID": "org-isolated",
            "X-Rezonance-User-ID": "usr-isolated",
            "X-Rezonance-User": "operator@example.invalid",
            "X-Rezonance-Role": "operator",
        },
        json={"action": "rez_ssh_command", "payload": {"device": "core-1", "command": "show version"}},
    )
    assert scoped.status_code == 200
    assert observed == {"org_id": "org-isolated", "action": "rez_ssh_command"}

    unscoped_design = client.get(
        "/api/network-model/active/rez-design",
        headers={"Authorization": "Bearer bridge-token"},
        params={"environment_id": "env-production"},
    )
    assert unscoped_design.status_code == 401
