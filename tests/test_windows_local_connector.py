from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import pytest

from netcode import runner_agent
from netcode.yamlio import read_yaml, write_yaml


def test_dpapi_yaml_path_uses_secret_protection(tmp_path: Path, monkeypatch):
    import netcode.windows_security as windows_security

    monkeypatch.setattr(windows_security, "protect_machine", lambda value: b"protected:" + value[::-1])
    monkeypatch.setattr(
        windows_security,
        "unprotect_machine",
        lambda value: value.removeprefix(b"protected:")[::-1],
    )
    path = tmp_path / "inventory.dpapi"
    payload = {"defaults": {"username": "admin", "password": "local-secret"}, "devices": [{"id": "r1"}]}

    write_yaml(path, payload)

    assert b"local-secret" not in path.read_bytes()
    assert read_yaml(path) == payload


def test_connector_doctor_reports_public_readiness_only(tmp_path: Path, monkeypatch, capsys):
    identity = tmp_path / "identity.json"
    inventory = tmp_path / "inventory.yaml"
    identity.write_text(json.dumps({
        "server": "https://control.example.test",
        "runner_id": "runner-1",
        "runner_token": "private-runner-token",
        "hmac_secret": "private-signing-secret",
        "pool": "pilot",
        "name": "windows-connector",
    }), encoding="utf-8")
    write_yaml(inventory, {
        "defaults": {"username": "admin", "password": "device-secret", "platform": "arista_eos"},
        "devices": [{"id": "core-1", "hostname": "core-1", "host": "192.0.2.10", "site": "hq"}],
    })
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", tmp_path)
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory)
    monkeypatch.setattr(runner_agent, "_get", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(runner_agent, "_rez_runtime_check", lambda: {
        "id": "rez_runtime",
        "status": "pass",
        "message": "Rez driver runtime loaded 11 platform adapter(s).",
    })
    monkeypatch.setattr(runner_agent, "_governed_template_check", lambda: {
        "id": "governed_templates",
        "status": "pass",
        "message": "Governed Arista and Cisco NTP templates are available locally.",
    })

    result = runner_agent.doctor(argparse.Namespace(timeout=1.0))
    output = capsys.readouterr().out
    data = json.loads(output)

    assert result == 0
    assert data["ok"] is True
    assert data["inventory"]["device_count"] == 1
    assert next(check for check in data["checks"] if check["id"] == "rez_runtime")["status"] == "pass"
    assert next(check for check in data["checks"] if check["id"] == "governed_templates")["status"] == "pass"
    assert data["security"]["credentials_returned"] is False
    assert "private-runner-token" not in output
    assert "private-signing-secret" not in output
    assert "device-secret" not in output


def test_rez_runtime_check_reports_driver_import_failure(monkeypatch):
    from netcode.adapters.rez import RezAdapterBridge

    monkeypatch.setattr(RezAdapterBridge, "health", lambda self: {
        "ok": False,
        "platform_count": 0,
        "error": "ModuleNotFoundError: No module named 'pydantic.deprecated.class_validators'",
    })

    check = runner_agent._rez_runtime_check()

    assert check["id"] == "rez_runtime"
    assert check["status"] == "fail"
    assert "pydantic.deprecated.class_validators" in check["message"]


def test_governed_template_check_requires_both_supported_ntp_templates(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner_agent, "_runner_workspace_root", lambda: tmp_path)
    arista = tmp_path / "templates" / "arista" / "ntp_standardize.j2"
    arista.parent.mkdir(parents=True)
    arista.write_text("ntp server {{ server }}\n", encoding="utf-8")

    missing = runner_agent._governed_template_check()

    assert missing["status"] == "fail"
    assert "cisco_ios" in missing["message"]

    cisco = tmp_path / "templates" / "cisco_ios" / "ntp_standardize.j2"
    cisco.parent.mkdir(parents=True)
    cisco.write_text("ntp server {{ server }}\n", encoding="utf-8")

    assert runner_agent._governed_template_check()["status"] == "pass"


def test_control_snapshot_never_returns_local_secrets(tmp_path: Path, monkeypatch):
    from netcode.windows_connector_control import connector_snapshot

    identity = tmp_path / "identity.json"
    inventory = tmp_path / "inventory.yaml"
    identity.write_text(json.dumps({
        "server": "https://control.example.test",
        "runner_id": "runner-1",
        "runner_token": "private-runner-token",
        "hmac_secret": "private-signing-secret",
        "pool": "community",
        "name": "windows-connector",
    }), encoding="utf-8")
    write_yaml(inventory, {
        "devices": [{
            "id": "core-1",
            "hostname": "core-1",
            "host": "192.0.2.10",
            "platform": "arista_eos",
            "username": "device-user",
            "password": "device-secret",
        }],
    })
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory)

    snapshot = connector_snapshot()
    serialized = json.dumps(snapshot)

    assert snapshot["enrolled"] is True
    assert snapshot["inventory"]["device_count"] == 1
    assert "private-runner-token" not in serialized
    assert "private-signing-secret" not in serialized
    assert "device-user" not in serialized
    assert "device-secret" not in serialized


def test_community_control_plane_is_locked_unless_internal_override_is_enabled(monkeypatch):
    from netcode.windows_connector_control import PRODUCTION_CONTROL_PLANE, control_plane_url

    monkeypatch.setenv("NETCODE_CONTROL_PLANE_URL", "https://untrusted.example.test")
    monkeypatch.delenv("NETCODE_ALLOW_CONTROL_PLANE_OVERRIDE", raising=False)
    assert control_plane_url() == PRODUCTION_CONTROL_PLANE

    monkeypatch.setenv("NETCODE_ALLOW_CONTROL_PLANE_OVERRIDE", "1")
    assert control_plane_url() == "https://untrusted.example.test"


def test_diagnostics_verify_exact_customer_identity_without_returning_secrets(
    tmp_path: Path,
    monkeypatch,
):
    from netcode import windows_connector_control

    identity = tmp_path / "identity.json"
    inventory = tmp_path / "inventory.yaml"
    identity.write_text(json.dumps({
        "server": "https://control.rezonancenetworks.com",
        "runner_id": "runner-1",
        "runner_token": "private-runner-token",
        "hmac_secret": "private-signing-secret",
        "pool": "private-org-id",
        "name": "acme-windows-01",
        "organization_name": "Acme Networks",
        "operator_email": "owner@acme.example",
        "identity_verified": True,
    }), encoding="utf-8")
    write_yaml(inventory, {
        "devices": [{
            "id": "core-1",
            "hostname": "core-1",
            "host": "192.0.2.10",
            "platform": "arista_eos",
            "username": "device-user",
            "password": "device-secret",
        }],
    })
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", inventory)
    monkeypatch.setattr(
        runner_agent,
        "connector_identity",
        lambda **kwargs: {
            "ok": True,
            "connector_name": "acme-windows-01",
            "organization_name": "Acme Networks",
            "operator_email": "owner@acme.example",
            "identity_verified": True,
        },
    )

    class Completed:
        returncode = 0
        stdout = "TaskName: RezonanceLocalConnector"
        stderr = ""

    monkeypatch.setattr(windows_connector_control.subprocess, "run", lambda *args, **kwargs: Completed())

    report = windows_connector_control.collect_diagnostics(timeout=1.0)
    serialized = json.dumps(report)

    assert report["ok"] is True
    assert report["connector"]["organization_name"] == "Acme Networks"
    assert report["connector"]["operator_email"] == "owner@acme.example"
    assert next(check for check in report["checks"] if check["id"] == "cloud_identity")["status"] == "pass"
    for secret in ("private-runner-token", "private-signing-secret", "private-org-id", "device-user", "device-secret"):
        assert secret not in serialized


def test_community_cli_hides_manual_inventory_import():
    completed = subprocess.run(
        [sys.executable, "-m", "netcode.runner_agent", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "discover-inventory" in completed.stdout
    assert "inventory-import" not in completed.stdout


def test_pairing_repair_prepares_saves_commits_and_preserves_existing_identity(monkeypatch):
    current = {
        "server": "https://control.rezonancenetworks.com",
        "runner_id": "runner-legacy",
        "runner_token": "current-runner-token",
        "hmac_secret": "current-hmac-secret",
        "pool": "pilot",
        "name": "windows-gns3-01",
        "organization_name": "",
        "operator_email": "",
        "identity_verified": False,
    }
    posts: list[dict[str, object]] = []
    writes: list[dict[str, object]] = []

    def fake_post(server, path, body, token=None, timeout=40.0):
        posts.append({"server": server, "path": path, "body": body, "token": token, "timeout": timeout})
        if path.endswith("/prepare"):
            return {
                "ok": True,
                "runner_id": "runner-legacy",
                "runner_token": "pending-runner-token",
                "hmac_secret": "pending-hmac-secret",
                "pool": "org-community",
                "connector_name": "acme-windows-01",
                "organization_name": "Acme Networks",
                "operator_email": "owner@acme.example",
                "identity_verified": True,
                "token_expires_at": "2026-08-01T00:00:00+00:00",
                "token_rotate_after": "2026-07-31T00:00:00+00:00",
            }
        return {
            "ok": True,
            "runner_id": "runner-legacy",
            "connector_name": "acme-windows-01",
            "organization_name": "Acme Networks",
            "operator_email": "owner@acme.example",
            "identity_verified": True,
        }

    monkeypatch.setattr(runner_agent, "_post", fake_post)
    monkeypatch.setattr(runner_agent, "_write_identity", lambda identity: writes.append(dict(identity)))

    result = runner_agent.repair_pairing(
        "https://control.rezonancenetworks.com",
        "one-time-repair-code",
        current,
    )

    assert result["ok"] is True
    assert [post["path"] for post in posts] == [
        "/api/runner/replacement/prepare",
        "/api/runner/replacement/commit",
    ]
    assert posts[0]["token"] == "current-runner-token"
    assert posts[1]["token"] == "pending-runner-token"
    assert posts[0]["body"] == posts[1]["body"] == {"pairing_code": "one-time-repair-code"}
    assert writes[0]["replacement_pending"] is True
    assert writes[0]["replacement_fallback_identity"]["runner_token"] == "current-runner-token"
    assert writes[-1]["runner_id"] == "runner-legacy"
    assert writes[-1]["runner_token"] == "pending-runner-token"
    assert writes[-1]["hmac_secret"] == "pending-hmac-secret"
    assert writes[-1]["pool"] == "org-community"
    assert writes[-1]["name"] == "acme-windows-01"
    assert "replacement_pairing_code" not in writes[-1]
    assert "replacement_fallback_identity" not in writes[-1]
    serialized = json.dumps(result)
    for secret in (
        "one-time-repair-code",
        "current-runner-token",
        "current-hmac-secret",
        "pending-runner-token",
        "pending-hmac-secret",
    ):
        assert secret not in serialized


def test_pending_pairing_repair_is_recoverable_after_interruption(monkeypatch):
    pending = {
        "server": "https://control.rezonancenetworks.com",
        "runner_id": "runner-legacy",
        "runner_token": "pending-runner-token",
        "hmac_secret": "pending-hmac-secret",
        "pool": "org-community",
        "name": "acme-windows-01",
        "organization_name": "Acme Networks",
        "operator_email": "owner@acme.example",
        "identity_verified": True,
        "replacement_pending": True,
        "replacement_pairing_code": "one-time-repair-code",
        "replacement_fallback_identity": {
            "runner_id": "runner-legacy",
            "runner_token": "current-runner-token",
            "hmac_secret": "current-hmac-secret",
        },
    }
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(
        runner_agent,
        "_post",
        lambda *args, **kwargs: {
            "ok": True,
            "runner_id": "runner-legacy",
            "connector_name": "acme-windows-01",
            "organization_name": "Acme Networks",
            "operator_email": "owner@acme.example",
            "identity_verified": True,
            "already_committed": True,
        },
    )
    monkeypatch.setattr(runner_agent, "_write_identity", lambda identity: writes.append(dict(identity)))

    recovered = runner_agent._commit_pending_replacement(pending)

    assert "replacement_pending" not in recovered
    assert recovered["runner_token"] == "pending-runner-token"
    assert "replacement_pairing_code" not in writes[-1]


def test_pairing_repair_uses_founder_bound_claim_when_old_token_is_rejected(monkeypatch):
    calls: list[str | None] = []

    def fake_post(server, path, body, token=None, timeout=40.0):
        calls.append(token)
        if path.endswith("/prepare") and token:
            raise RuntimeError("HTTP 401 from replacement prepare")
        if path.endswith("/prepare"):
            return {
                "ok": True,
                "runner_id": "runner-legacy",
                "runner_token": "pending-runner-token",
                "hmac_secret": "pending-hmac-secret",
                "pool": "org-community",
                "connector_name": "acme-windows-01",
                "organization_name": "Acme Networks",
                "operator_email": "owner@acme.example",
                "identity_verified": True,
            }
        return {
            "ok": True,
            "runner_id": "runner-legacy",
            "connector_name": "acme-windows-01",
            "organization_name": "Acme Networks",
            "operator_email": "owner@acme.example",
            "identity_verified": True,
        }

    monkeypatch.setattr(runner_agent, "_post", fake_post)
    monkeypatch.setattr(runner_agent, "_write_identity", lambda identity: None)

    result = runner_agent.repair_pairing(
        "https://control.rezonancenetworks.com",
        "founder-bound-repair-code",
        {
            "server": "https://control.rezonancenetworks.com",
            "runner_id": "runner-legacy",
            "runner_token": "rejected-runner-token",
            "hmac_secret": "current-hmac-secret",
            "pool": "pilot",
            "name": "windows-gns3-01",
        },
    )

    assert result["ok"] is True
    assert calls == ["rejected-runner-token", None, "pending-runner-token"]


def test_rejected_pending_repair_restores_the_previous_connector_identity(monkeypatch):
    fallback = {
        "runner_id": "runner-legacy",
        "runner_token": "current-runner-token",
        "hmac_secret": "current-hmac-secret",
    }
    pending = {
        **fallback,
        "server": "https://control.rezonancenetworks.com",
        "runner_token": "stale-pending-token",
        "replacement_pending": True,
        "replacement_pairing_code": "one-time-repair-code",
        "replacement_fallback_identity": fallback,
    }
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(
        runner_agent,
        "_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 401 from replacement commit")),
    )
    monkeypatch.setattr(runner_agent, "_write_identity", lambda identity: writes.append(dict(identity)))

    with pytest.raises(RuntimeError, match="HTTP 401"):
        runner_agent._commit_pending_replacement(pending)

    assert writes == [fallback]


def test_connector_ui_uses_customer_pairing_language():
    from netcode import windows_connector_control

    source = Path(windows_connector_control.__file__).read_text(encoding="utf-8")

    assert "One-time pairing code" in source
    assert "Verify code" in source
    assert "Verified customer" in source
    assert "Connect this device" in source
    assert "Pair again" in source
    assert "Repair identity" in source
    assert "Repair this connector" in source
    assert "Open customer portal" in source
    assert "Community login" in source
    assert "https://app.rezonancenetworks.com" in source
    assert "preview_replacement" in source
    assert "self.enroll_name" not in source
    assert "self.enroll_server" not in source
    assert "one-time join token" not in source
    assert "Show password" not in source
    assert "runner_token" not in source


def test_connector_activation_restarts_only_after_identity_repair(monkeypatch):
    from netcode import windows_connector_control

    calls: list[str] = []
    monkeypatch.setattr(
        windows_connector_control,
        "_run_task",
        lambda command: (calls.append(command) or True, ""),
    )

    result = windows_connector_control._activate_connector(restart=True)

    assert result["ok"] is True
    assert result["restarted"] is True
    assert calls == ["End", "Run"]


def test_connector_ui_explains_startup_task_access_denial(monkeypatch):
    from netcode import windows_connector_control

    class Completed:
        returncode = 5
        stdout = ""
        stderr = "ERROR: Access is denied."

    monkeypatch.setattr(windows_connector_control.subprocess, "run", lambda *args, **kwargs: Completed())

    ok, message = windows_connector_control._run_task("Run")

    assert ok is False
    assert "Open Diagnostics" in message
    assert "Repair permissions" in message
