from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.config_policy import prohibited_custom_config_lines
from netcode.models import RenderResult
from netcode.paths import WorkspacePaths
from netcode.runner_checks import local_policy_gate


@pytest.mark.parametrize(
    "line",
    [
        "reload",
        "relo",
        "do reload now",
        "write",
        "wr",
        "write memory",
        "copy running-config startup-config",
        "format flash:",
        "bash",
        "interface Ethernet1 ; reload",
    ],
)
def test_custom_config_hard_floor_rejects_destructive_or_ambiguous_lines(line: str) -> None:
    assert prohibited_custom_config_lines(line)


@pytest.mark.parametrize(
    "line",
    [
        "interface Ethernet1",
        "description CUSTOMER_UPLINK",
        "no shutdown",
        "route-map EXPORT deny 20",
        "no interface Loopback999",
    ],
)
def test_custom_config_hard_floor_does_not_classify_normal_feature_config(line: str) -> None:
    assert prohibited_custom_config_lines(line) == []


def test_custom_config_plan_blocks_reload_before_any_job(tmp_path: Path, monkeypatch) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post(
        "/api/desired-state/plan",
        json={
            "change_type": "custom_config",
            "site": "store-1842",
            "device_id": "v2-store1",
            "requested_by": "unit",
            "values": {
                "description": "must not run",
                "config_lines": "reload",
                "rollback_lines": "no interface Loopback999",
                "verify_contains": "Loopback999",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    custom = next(
        check for check in body["pipeline"]["validation"]["checks"]
        if check["id"] == "custom_policy"
    )
    assert custom["status"] == "fail"
    assert "prohibited" in custom["message"].lower()
    assert body.get("job") is None


def test_runner_independently_blocks_prohibited_forward_or_rollback() -> None:
    intent = SimpleNamespace(
        change_type="custom_config",
        custom=SimpleNamespace(config_lines="description safe", rollback_lines="reload"),
    )
    render = RenderResult(template_path="x", config="description safe\n", variables={})

    gate = local_policy_gate(intent, render, "", "")

    assert gate["ok"] is False
    assert gate["blocked_lines"] == ["reload"]
