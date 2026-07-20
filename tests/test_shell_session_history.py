from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.store import DEFAULT_ORG_ID, PlatformStore


def test_shell_session_updates_counters_without_erasing_touch_state(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    store.create_shell_session(
        session_id="session-counter-test",
        org_id=DEFAULT_ORG_ID,
        device_id="v2-store1",
        display_id="v2-store1",
        platform="arista_eos",
        transcript_path=str(workspace.reports / "shell-session-counter-test.jsonl"),
        device_touched=True,
    )

    updated = store.update_shell_session(
        "session-counter-test",
        command_delta=2,
        output_bytes_delta=128,
    )

    assert updated is not None
    assert updated["command_count"] == 2
    assert updated["output_bytes"] == 128
    assert updated["device_touched"] is True


def test_shell_session_can_explicitly_clear_touch_state(tmp_path: Path):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    store = PlatformStore(workspace)
    store.create_shell_session(
        session_id="session-touch-test",
        org_id=DEFAULT_ORG_ID,
        device_id="v2-store1",
        display_id="v2-store1",
        platform="arista_eos",
        transcript_path=str(workspace.reports / "shell-session-touch-test.jsonl"),
        device_touched=True,
    )

    updated = store.update_shell_session("session-touch-test", device_touched=False)

    assert updated is not None
    assert updated["device_touched"] is False


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


def test_service_authenticated_browser_shell_stays_live_and_persists_read_output(
    tmp_path: Path,
    monkeypatch,
):
    """Exercise the complete browser -> broker -> connector Shell transport."""
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "get_entitlements", lambda **_kwargs: object())
    api._SHELL_SESSIONS.clear()
    api._RUNNER_CHANNELS.clear()
    api._RUNNER_CHANNEL_POOLS.clear()
    api._BROWSER_SOCKETS.clear()
    client = TestClient(api.app)
    runner_id = "connector-shell-e2e"
    session_id = "shell-transport-e2e"
    api._SHELL_SESSIONS[session_id] = {
        "org_id": DEFAULT_ORG_ID,
        "device_id": "v2-hq-core",
        "display_id": "v2-hq-core",
        "platform": "arista_eos",
        "runner_id": runner_id,
        "runner_pool": "pilot",
        "state": {"mode": "direct", "device_touched": False},
    }
    PlatformStore(workspace).create_shell_session(
        session_id=session_id,
        org_id=DEFAULT_ORG_ID,
        device_id="v2-hq-core",
        display_id="v2-hq-core",
        platform="arista_eos",
        runner_id=runner_id,
        runner_pool="pilot",
        transcript_path=str(workspace.reports / f"shell-{session_id}.jsonl"),
    )
    api._shell_append(
        workspace,
        session_id,
        {
            "event": "session_opened",
            "device_id": "v2-hq-core",
            "org_id": DEFAULT_ORG_ID,
            "runner_id": runner_id,
        },
    )
    monkeypatch.setenv("NETCODE_AUTH", "1")
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "private-service-token")
    service_headers = {
        "Authorization": "Bearer private-service-token",
        "X-Rezonance-Org-ID": DEFAULT_ORG_ID,
        "X-Rezonance-User": "community-operator",
        "X-Rezonance-User-ID": "usr_community_operator",
        "X-Rezonance-Role": "operator",
    }
    encoded_output = base64.b64encode(b"Hostname: v2-hq-core\r\n").decode("ascii")

    class FakeRunnerChannel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(frame)
            sid = str(frame.get("sid") or "")
            browser = api._BROWSER_SOCKETS.get(sid)
            if browser is None:
                return
            if frame.get("t") == "open":
                await browser.send_json(
                    {"t": "status", "sid": sid, "s": "connected"}
                )
            elif frame.get("t") == "in":
                event = {
                    "t": "event",
                    "sid": sid,
                    "e": {
                        "type": "command",
                        "line": "show hostname",
                        "kind": "operational",
                    },
                }
                api._record_shell_command(sid, event["e"])
                await browser.send_json(event)
                api._record_shell_output(sid, encoded_output)
                await browser.send_json(
                    {"t": "out", "sid": sid, "d": encoded_output}
                )

    runner_channel = FakeRunnerChannel()
    api._RUNNER_CHANNELS[runner_id] = runner_channel
    api._RUNNER_CHANNEL_POOLS[runner_id] = "pilot"

    try:
        with client.websocket_connect(
            f"/api/shell/session/{session_id}", headers=service_headers
        ) as browser_socket:
            assert browser_socket.receive_json() == {
                "t": "status",
                "sid": session_id,
                "s": "connected",
            }
            browser_socket.send_json({"t": "in", "d": "show hostname\r"})
            assert browser_socket.receive_json()["t"] == "event"
            assert browser_socket.receive_json() == {
                "t": "out",
                "sid": session_id,
                "d": encoded_output,
            }
            browser_socket.close(code=1000)

        assert runner_channel.frames[0] == {
            "t": "open",
            "sid": session_id,
            "device_id": "v2-hq-core",
            "state": {"mode": "direct", "device_touched": False},
        }
        assert runner_channel.frames[1] == {
            "t": "in",
            "sid": session_id,
            "d": "show hostname\r",
        }
        for _ in range(50):
            if runner_channel.frames[-1] == {"t": "close", "sid": session_id}:
                break
            time.sleep(0.01)
        assert runner_channel.frames[-1] == {"t": "close", "sid": session_id}

        transcript = client.get(
            f"/api/shell/{session_id}/transcript", headers=service_headers
        )
        history = client.get("/api/shell/sessions", headers=service_headers)
        assert transcript.status_code == 200
        body = transcript.json()
        assert body["session"]["status"] == "closed"
        assert body["session"]["command_count"] == 1
        assert body["session"]["output_bytes"] == len(b"Hostname: v2-hq-core\r\n")
        assert any(entry.get("command") == "show hostname" for entry in body["entries"])
        assert any("Hostname: v2-hq-core" in entry.get("output", "") for entry in body["entries"])
        assert history.status_code == 200
        assert history.json()["sessions"][0]["id"] == session_id
    finally:
        api._SHELL_SESSIONS.clear()
        api._RUNNER_CHANNELS.clear()
        api._RUNNER_CHANNEL_POOLS.clear()
        api._BROWSER_SOCKETS.clear()


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
