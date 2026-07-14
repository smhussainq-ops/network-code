from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netcode import api, runner_agent
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.runner_hub import (
    authenticate_runner,
    confirm_runner_token_rotation,
    enroll_runner,
    mint_join_token,
    prepare_runner_token_rotation,
)
from netcode.store import DEFAULT_ORG_ID, PlatformStore


def _store(tmp_path: Path) -> PlatformStore:
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    return PlatformStore(workspace)


def _enrolled(store: PlatformStore) -> dict[str, object]:
    join = mint_join_token(store, "pilot")
    result = enroll_runner(store, str(join["join_token"]), "connector-1")
    assert result["ok"] is True
    return result


def test_two_phase_rotation_preserves_access_and_expires_overlap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    enrolled = _enrolled(store)
    old_token = str(enrolled["runner_token"])
    runner = authenticate_runner(store, old_token)
    assert runner is not None

    prepared = prepare_runner_token_rotation(store, runner, old_token)
    pending_token = str(prepared["runner_token"])
    assert pending_token != old_token
    assert authenticate_runner(store, old_token) is not None
    pending_runner = authenticate_runner(store, pending_token)
    assert pending_runner is not None

    confirmed = confirm_runner_token_rotation(store, pending_runner, pending_token)
    assert confirmed["already_confirmed"] is False
    assert authenticate_runner(store, pending_token) is not None
    assert authenticate_runner(store, old_token) is not None
    repeated = confirm_runner_token_rotation(
        store,
        authenticate_runner(store, pending_token),
        pending_token,
    )
    assert repeated["already_confirmed"] is True

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with store._connect() as conn:  # noqa: SLF001 - expire the overlap at the durable boundary.
        conn.execute(
            "UPDATE runners SET previous_token_expires_at = ? WHERE id = ?",
            (expired, enrolled["runner_id"]),
        )
    assert authenticate_runner(store, old_token) is None
    assert authenticate_runner(store, pending_token) is not None
    events = store.list_runner_security_events(str(enrolled["runner_id"]), DEFAULT_ORG_ID)
    assert {item["event"] for item in events} >= {
        "token_enrolled",
        "token_rotation_prepared",
        "token_rotation_confirmed",
    }


def test_pending_rotation_does_not_invalidate_saved_current_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    enrolled = _enrolled(store)
    current = str(enrolled["runner_token"])
    runner = authenticate_runner(store, current)
    prepare_runner_token_rotation(store, runner, current)

    assert authenticate_runner(store, current) is not None
    with pytest.raises(RuntimeError, match="already pending"):
        prepare_runner_token_rotation(store, runner, current)


def test_revoke_invalidates_current_previous_and_pending_tokens(tmp_path: Path) -> None:
    store = _store(tmp_path)
    enrolled = _enrolled(store)
    old_token = str(enrolled["runner_token"])
    runner = authenticate_runner(store, old_token)
    first_pending = prepare_runner_token_rotation(store, runner, old_token)
    first_token = str(first_pending["runner_token"])
    confirm_runner_token_rotation(store, authenticate_runner(store, first_token), first_token)
    current_runner = authenticate_runner(store, first_token)
    second_pending = prepare_runner_token_rotation(store, current_runner, first_token)
    second_token = str(second_pending["runner_token"])

    revoked = store.revoke_runner(str(enrolled["runner_id"]), DEFAULT_ORG_ID)
    assert revoked.status == "revoked"
    store.touch_runner(revoked.id, status="online")
    store.heartbeat_runner(revoked.id, version="stale-process", state="online")
    assert store.get_runner(revoked.id).status == "revoked"
    assert authenticate_runner(store, old_token) is None
    assert authenticate_runner(store, first_token) is None
    assert authenticate_runner(store, second_token) is None
    with pytest.raises(ValueError, match="Unknown runner"):
        store.set_runner_drain(revoked.id, DEFAULT_ORG_ID, requested=False)


def test_runner_identity_rotation_survives_confirmation_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_file = tmp_path / "identity.json"
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", tmp_path)
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity_file)
    identity = {
        "server": "https://control.example.com",
        "runner_id": "runner-1",
        "runner_token": "old-token",
        "hmac_secret": "hmac",
        "pool": "pilot",
        "name": "connector-1",
        "token_rotate_after": "2000-01-01T00:00:00+00:00",
        "token_pending": False,
    }
    calls: list[str] = []

    def fake_post(server, path, body, token=None, timeout=40.0):  # noqa: ANN001, ARG001
        calls.append(path)
        if path.endswith("/rotate"):
            return {
                "ok": True,
                "runner_token": "pending-token",
                "token_expires_at": "2099-01-01T00:00:00+00:00",
                "token_rotate_after": "2098-01-01T00:00:00+00:00",
                "pending_token_valid_until": "2097-01-01T00:00:00+00:00",
            }
        raise TimeoutError("confirmation response lost")

    monkeypatch.setattr(runner_agent, "_post", fake_post)
    pending = runner_agent._maintain_runner_token(identity)  # noqa: SLF001
    saved = json.loads(identity_file.read_text(encoding="utf-8"))

    assert calls == ["/api/runner/token/rotate", "/api/runner/token/confirm"]
    assert pending["runner_token"] == "pending-token"
    assert pending["rotation_fallback_token"] == "old-token"
    assert saved["token_pending"] is True


def test_rejected_pending_identity_restores_saved_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_file = tmp_path / "identity.json"
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", tmp_path)
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity_file)
    pending = {
        "server": "https://control.example.com",
        "runner_token": "expired-pending",
        "rotation_fallback_token": "still-current",
        "token_pending": True,
    }

    def reject(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("HTTP 401 from /api/runner/token/confirm: expired")

    monkeypatch.setattr(runner_agent, "_post", reject)
    restored = runner_agent._maintain_runner_token(pending)  # noqa: SLF001

    assert restored["runner_token"] == "still-current"
    assert restored["token_pending"] is False
    assert "rotation_fallback_token" not in restored
    assert json.loads(identity_file.read_text(encoding="utf-8"))["runner_token"] == "still-current"


def test_atomic_identity_write_failure_keeps_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_file = tmp_path / "identity.json"
    identity_file.write_text('{"runner_token":"old"}', encoding="utf-8")
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", tmp_path)
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity_file)

    def fail_replace(source, destination):  # noqa: ANN001, ARG001
        raise OSError("disk failure")

    monkeypatch.setattr(runner_agent.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        runner_agent._write_identity({"runner_token": "new"})  # noqa: SLF001

    assert json.loads(identity_file.read_text(encoding="utf-8"))["runner_token"] == "old"
    assert not list(tmp_path.glob(".*.tmp"))


def test_token_endpoints_rotate_confirm_and_revoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    join = client.post("/api/runners/join-token", json={"pool": "pilot"}).json()
    enrolled = client.post(
        "/api/runner/enroll",
        json={"join_token": join["join_token"], "name": "connector-api"},
    ).json()
    old_headers = {"Authorization": f"Bearer {enrolled['runner_token']}"}

    prepared = client.post("/api/runner/token/rotate", headers=old_headers)
    assert prepared.status_code == 200
    pending_token = prepared.json()["runner_token"]
    pending_headers = {"Authorization": f"Bearer {pending_token}"}
    confirmed = client.post("/api/runner/token/confirm", headers=pending_headers)
    assert confirmed.status_code == 200
    assert client.post(
        "/api/runner/heartbeat",
        headers=pending_headers,
        json={"version": "test", "state": "online"},
    ).status_code == 200

    revoked = client.post(f"/api/runners/{enrolled['runner_id']}/revoke")
    assert revoked.status_code == 200
    assert client.post(
        "/api/runner/heartbeat",
        headers=pending_headers,
        json={"version": "test", "state": "online"},
    ).status_code == 401
    events = client.get(f"/api/runners/{enrolled['runner_id']}/security-events")
    assert events.status_code == 200
    assert events.json()["events"][0]["event"] == "token_revoked"


def test_enroll_persists_rotation_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity_file = tmp_path / "identity.json"
    monkeypatch.setattr(runner_agent, "IDENTITY_DIR", tmp_path)
    monkeypatch.setattr(runner_agent, "IDENTITY_FILE", identity_file)

    def fake_post(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        return {
            "ok": True,
            "runner_id": "runner-1",
            "runner_token": "token-1",
            "hmac_secret": "hmac-1",
            "pool": "pilot",
            "token_expires_at": "2099-01-01T00:00:00+00:00",
            "token_rotate_after": "2098-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(runner_agent, "_post", fake_post)
    result = runner_agent.enroll(
        argparse.Namespace(server="https://control.example.com", join_token="join", name="connector")
    )
    saved = json.loads(identity_file.read_text(encoding="utf-8"))

    assert result == 0
    assert saved["token_rotate_after"] == "2098-01-01T00:00:00+00:00"
    assert saved["token_pending"] is False
