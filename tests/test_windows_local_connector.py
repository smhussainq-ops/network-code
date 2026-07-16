from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

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

    result = runner_agent.doctor(argparse.Namespace(timeout=1.0))
    output = capsys.readouterr().out
    data = json.loads(output)

    assert result == 0
    assert data["ok"] is True
    assert data["inventory"]["device_count"] == 1
    assert data["security"]["credentials_returned"] is False
    assert "private-runner-token" not in output
    assert "private-signing-secret" not in output
    assert "device-secret" not in output


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
