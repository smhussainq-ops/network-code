from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.store import DEFAULT_ORG_ID, PlatformStore


def test_shell_transcript_survives_process_memory_loss(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    api._SHELL_SESSIONS.clear()
    client = TestClient(api.app)

    opened = client.post("/api/shell/open", json={"device_id": "v2-store1"})
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]
    api._record_shell_command(
        session_id,
        {"type": "command", "line": "show version", "kind": "operational"},
    )
    api._record_shell_output(
        session_id,
        base64.b64encode(b"v2-store1 uptime is 1 day\r\n").decode("ascii"),
    )
    PlatformStore(workspace).update_shell_session(session_id, status="closed", ended=True)

    api._SHELL_SESSIONS.clear()  # Simulate a control-plane restart.
    transcript = client.get(f"/api/shell/{session_id}/transcript")
    history = client.get("/api/shell/sessions")

    assert transcript.status_code == 200
    body = transcript.json()
    assert body["session"]["status"] == "closed"
    assert body["session"]["command_count"] == 1
    assert body["session"]["output_bytes"] == len(b"v2-store1 uptime is 1 day\r\n")
    assert any(entry.get("command") == "show version" for entry in body["entries"])
    assert any("uptime is 1 day" in entry.get("output", "") for entry in body["entries"])
    assert history.status_code == 200
    assert history.json()["sessions"][0]["id"] == session_id


def test_shell_history_backfills_legacy_transcripts(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    legacy_id = "legacy-session-01"
    legacy_path = workspace.reports / f"shell-{legacy_id}.jsonl"
    legacy_path.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {
                    "event": "session_opened",
                    "device_id": "edge-legacy",
                    "org_id": DEFAULT_ORG_ID,
                    "guard_enabled": True,
                },
                {
                    "event": "command",
                    "device_id": "edge-legacy",
                    "command": "show clock",
                    "kind": "operational",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    response = TestClient(api.app).get("/api/shell/sessions")

    assert response.status_code == 200
    indexed = next(item for item in response.json()["sessions"] if item["id"] == legacy_id)
    assert indexed["device_id"] == "edge-legacy"
    assert indexed["status"] == "archived"
    assert indexed["command_count"] == 1
    assert PlatformStore(workspace).get_shell_session(legacy_id) is not None


def test_shell_history_is_tenant_scoped(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    foreign_id = "foreign-session-01"
    foreign_path = workspace.reports / f"shell-{foreign_id}.jsonl"
    foreign_path.write_text(
        json.dumps({"event": "session_opened", "device_id": "foreign-edge", "org_id": "org_other"}) + "\n",
        encoding="utf-8",
    )
    PlatformStore(workspace).create_shell_session(
        session_id=foreign_id,
        org_id="org_other",
        device_id="foreign-edge",
        display_id="foreign-edge",
        platform="arista_eos",
        transcript_path=str(foreign_path),
    )
    client = TestClient(api.app)

    history = client.get("/api/shell/sessions")
    transcript = client.get(f"/api/shell/{foreign_id}/transcript")

    assert all(item["id"] != foreign_id for item in history.json()["sessions"])
    assert transcript.status_code == 404
