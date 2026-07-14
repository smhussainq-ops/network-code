from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.production_readiness import collect_netcode_production_issues
from netcode.yamlio import read_yaml


def _valid_env() -> dict[str, str]:
    return {
        "NETCODE_ENV": "production",
        "NETCODE_EXECUTION": "runner",
        "NETCODE_RUNNER_POOL": "pilot",
        "NETCODE_AUTH": "1",
        "NETCODE_REQUIRE_APPROVAL": "true",
        "NETCODE_LICENSE_ENFORCEMENT": "true",
        "DATABASE_URL": "postgresql://netcode:secret@postgres.internal:5432/netcode",
        "NETCODE_WORKSPACE": "/data",
        "NETCODE_ALLOWED_HOSTS": "netcode.rezonance.example",
        "NETCODE_REZ_BRIDGE_TOKEN": "bridge-token-with-at-least-32-characters",
        "NETCODE_ENTITLEMENT_URL": "http://rez.internal:8080/api/license/platform-entitlements",
        "NETCODE_ENTITLEMENT_TOKEN": "entitlement-token-with-at-least-32-characters",
        "NETCODE_REZ_TRIGGER_URL": "http://rez.internal:8080",
        "NETCODE_REZ_TRIGGER_TOKEN": "trigger-token-with-at-least-32-characters",
        "NETCODE_REZ_ENVIRONMENT_ID": "env_customer_1",
        "WEB_CONCURRENCY": "1",
        "NETCODE_BOOTSTRAP_ADMIN_EMAIL": "pilot@example.com",
        "NETCODE_BOOTSTRAP_ADMIN_PASSWORD": "a-long-random-bootstrap-secret",
    }


def test_development_does_not_require_cloud_configuration():
    assert collect_netcode_production_issues({}, persisted_auth_users=False) == []


def test_complete_production_configuration_is_ready():
    assert collect_netcode_production_issues(_valid_env(), persisted_auth_users=False) == []


def test_persisted_admin_allows_bootstrap_secret_to_be_removed():
    env = _valid_env()
    env.pop("NETCODE_BOOTSTRAP_ADMIN_EMAIL")
    env.pop("NETCODE_BOOTSTRAP_ADMIN_PASSWORD")

    assert collect_netcode_production_issues(env, persisted_auth_users=True) == []


def test_production_rejects_direct_execution_and_approval_bypasses():
    env = _valid_env()
    env.update(
        {
            "NETCODE_EXECUTION": "local",
            "NETCODE_AUTH": "false",
            "NETCODE_REQUIRE_APPROVAL": "false",
            "NETCODE_REZ_BRIDGE_TOKEN": "short",
            "WEB_CONCURRENCY": "3",
            "NETCODE_BOOTSTRAP_ADMIN_PASSWORD": "admin123",
        }
    )

    issues = collect_netcode_production_issues(env, persisted_auth_users=False)

    assert "NETCODE_EXECUTION must be runner" in issues
    assert "NETCODE_AUTH must be enabled" in issues
    assert "NETCODE_REQUIRE_APPROVAL must be enabled" in issues
    assert "NETCODE_REZ_BRIDGE_TOKEN must contain at least 32 characters" in issues
    assert "WEB_CONCURRENCY must be 1 until runner and shell state is externalized" in issues
    assert "NETCODE_BOOTSTRAP_ADMIN_PASSWORD must be a strong secret" in issues


def test_production_requires_postgres_licensing_and_rez_handoff():
    env = _valid_env()
    env.update(
        {
            "DATABASE_URL": "sqlite:////data/netcode.db",
            "NETCODE_LICENSE_ENFORCEMENT": "false",
            "NETCODE_ENTITLEMENT_URL": "",
            "NETCODE_REZ_TRIGGER_URL": "",
            "NETCODE_REZ_ENVIRONMENT_ID": "",
        }
    )

    issues = collect_netcode_production_issues(env, persisted_auth_users=False)

    assert "DATABASE_URL must use PostgreSQL" in issues
    assert "NETCODE_LICENSE_ENFORCEMENT must be enabled" in issues
    assert "NETCODE_ENTITLEMENT_URL must be an http(s) URL" in issues
    assert "NETCODE_REZ_TRIGGER_URL must be an http(s) URL" in issues
    assert "NETCODE_REZ_ENVIRONMENT_ID is required" in issues


def test_production_requires_explicit_allowed_hosts():
    env = _valid_env()
    env["NETCODE_ALLOWED_HOSTS"] = "*"

    issues = collect_netcode_production_issues(env, persisted_auth_users=False)

    assert "NETCODE_ALLOWED_HOSTS must contain explicit production hosts" in issues


def test_placeholder_secrets_never_satisfy_production_gate():
    env = _valid_env()
    env.update(
        {
            "DATABASE_URL": "postgresql://netcode:replace-with-secret@postgres.internal/netcode",
            "NETCODE_REZ_BRIDGE_TOKEN": "replace-with-secret-manager-value",
            "NETCODE_ENTITLEMENT_TOKEN": "replace-with-secret-manager-value",
            "NETCODE_REZ_TRIGGER_TOKEN": "replace-with-secret-manager-value",
            "NETCODE_BOOTSTRAP_ADMIN_PASSWORD": "replace-with-secret-manager-value",
        }
    )

    issues = collect_netcode_production_issues(env, persisted_auth_users=False)

    assert "DATABASE_URL must not contain placeholder credentials" in issues
    assert "NETCODE_REZ_BRIDGE_TOKEN must contain at least 32 characters" in issues
    assert "NETCODE_ENTITLEMENT_TOKEN must contain at least 32 characters" in issues
    assert "NETCODE_REZ_TRIGGER_TOKEN must contain at least 32 characters" in issues
    assert "NETCODE_BOOTSTRAP_ADMIN_PASSWORD must be a strong secret" in issues


def test_packaged_static_assets_can_live_outside_workspace(tmp_path, monkeypatch):
    static_dir = tmp_path / "application" / "static"
    static_dir.mkdir(parents=True)
    monkeypatch.setenv("NETCODE_STATIC_DIR", str(static_dir))

    workspace = WorkspacePaths(tmp_path / "runtime")

    assert workspace.static == static_dir.resolve()


def test_production_workspace_does_not_seed_lab_inventory_or_example_intent(tmp_path):
    workspace = WorkspacePaths(tmp_path / "runtime")

    init_workspace(workspace, include_examples=False)

    assert not (workspace.inventories / "lab.yaml").exists()
    assert not (workspace.intents / "examples" / "add_guest_vlan.yaml").exists()
    policies = read_yaml(workspace.policies / "invariants.yaml")
    assert policies["segmentation"]["pci_subnets"] == []


def test_production_image_does_not_embed_lab_inventory():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY inventories" not in dockerfile


def test_production_image_removes_python_build_tree():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "rm -rf /app/build" in dockerfile


def test_production_api_documentation_is_disabled(monkeypatch):
    monkeypatch.setattr(api, "_PRODUCTION_RUNTIME", True)
    client = TestClient(api.app)

    for path in ("/docs", "/docs/", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
