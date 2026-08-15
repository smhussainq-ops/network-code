from __future__ import annotations

from pathlib import Path

import pytest

from netcode.adapters.registry import AdapterRegistry
from netcode.bootstrap import init_workspace
from netcode.inventory import Device
from netcode.lab import (
    CiscoIOSNtpAdapter,
    CiscoNXOSCustomConfigAdapter,
    offline_dry_run,
)
from netcode.models import CustomConfigIntent, TargetSpec
from netcode.paths import WorkspacePaths
from netcode.rendering import render_intent


class _Render:
    def __init__(self, config: str):
        self.config = config


class _GenericConnection:
    def __init__(self, lines: list[str], *, ignore_first_config: bool = False):
        self.lines = list(lines)
        self.ignore_first_config = ignore_first_config
        self.config_calls: list[list[str]] = []
        self.save_calls = 0

    def send_command_timing(self, command: str, **_kwargs):
        if command == "show running-config":
            return "\n".join(self.lines)
        return "ok"

    def send_config_set(self, *, config_commands, **_kwargs):
        commands = list(config_commands)
        self.config_calls.append(commands)
        if self.ignore_first_config:
            self.ignore_first_config = False
        else:
            for command in commands:
                if command.startswith("no "):
                    positive = command.removeprefix("no ")
                    self.lines = [line for line in self.lines if line != positive]
                elif command not in self.lines:
                    self.lines.append(command)
        return "\n".join(f"accepted: {command}" for command in commands)

    def save_config(self):
        self.save_calls += 1
        return "Building configuration... [OK]"

    def disconnect(self):
        return None


def _device(platform: str) -> Device:
    return Device(
        id=f"edge-{platform}",
        host="192.0.2.10",
        platform=platform,
        username="local-user",
        password="local-password",
        port=22,
        hostname=f"edge-{platform}",
        site="site-101",
        groups=("edge",),
    )


def _intent(device_id: str) -> CustomConfigIntent:
    return CustomConfigIntent(
        site="site-101",
        targets=TargetSpec(device_ids=[device_id]),
        custom={
            "description": "Enable millisecond log timestamps",
            "config_lines": "service timestamps log datetime msec",
            "rollback_lines": "no service timestamps log datetime msec",
            "verify_contains": "service timestamps log datetime msec",
        },
    )


def _adapter(adapter_class, device: Device, connection: _GenericConnection, context=None):
    adapter = adapter_class(device, operation_context=context or {})
    adapter.connect = lambda: setattr(adapter, "_conn", connection)  # type: ignore[method-assign]
    adapter.disconnect = lambda: None  # type: ignore[method-assign]
    return adapter


@pytest.mark.parametrize("platform", ["arista_eos", "cisco_ios", "cisco_nxos"])
def test_custom_config_render_is_platform_neutral_reviewed_cli(tmp_path: Path, platform: str) -> None:
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    intent = _intent("edge-1")

    rendered = render_intent(intent, paths, platform=platform)

    assert rendered.template_path == "builtin/custom_config"
    assert rendered.config == "service timestamps log datetime msec\n"


def test_generic_write_capability_is_explicit_and_unsupported_platforms_fail_closed() -> None:
    for platform in ("arista_eos", "cisco_ios", "cisco_nxos"):
        support = AdapterRegistry.execution_support(platform, "custom_config")
        assert support["supported"] is True
        assert "custom_config" in support["supported_change_types"]

    assert AdapterRegistry.execution_support("juniper_junos", "custom_config")["supported"] is False
    assert AdapterRegistry.execution_support("cisco_ios", "add_vlan")["supported"] is False


def test_custom_offline_dry_run_persists_only_a_reviewed_config_fingerprint() -> None:
    device = _device("cisco_ios")
    intent = _intent(device.id)
    running_config = "hostname edge-ios\nlogging buffered 10000"

    result = offline_dry_run(
        device,
        intent,
        _Render(intent.custom.config_lines),
        running_config=running_config,
    )

    assert result.status == "pass"
    state = result.evidence["rollback_state"]
    assert state == {
        "schema": "netcode.custom-config-pre-change.v1",
        "device_id": device.id,
        "platform": "cisco_ios",
        "running_config_fingerprint": state["running_config_fingerprint"],
    }
    assert len(state["running_config_fingerprint"]) == 64
    assert running_config not in str(state)


@pytest.mark.parametrize(
    ("platform", "adapter_class"),
    [
        ("cisco_ios", CiscoIOSNtpAdapter),
        ("cisco_nxos", CiscoNXOSCustomConfigAdapter),
    ],
)
def test_generic_custom_apply_verify_save_and_engineer_rollback(
    platform: str,
    adapter_class,
) -> None:
    device = _device(platform)
    intent = _intent(device.id)
    before = "hostname edge"
    dry = offline_dry_run(
        device,
        intent,
        _Render(intent.custom.config_lines),
        running_config=before,
    )
    connection = _GenericConnection([before])

    applied = _adapter(
        adapter_class,
        device,
        connection,
        {"approved_pre_change_state": dry.evidence["rollback_state"]},
    ).apply(intent, _Render(intent.custom.config_lines))

    assert applied.status == "pass"
    assert intent.custom.config_lines in connection.lines
    assert connection.save_calls == 1

    rolled_back = _adapter(adapter_class, device, connection).rollback(
        intent,
        _Render(intent.custom.config_lines),
    )

    assert rolled_back.status == "pass"
    assert intent.custom.config_lines not in connection.lines
    assert connection.save_calls == 2
    assert connection.config_calls[-1] == [intent.custom.rollback_lines]


def test_custom_apply_rejects_config_drift_before_any_write() -> None:
    device = _device("cisco_ios")
    intent = _intent(device.id)
    dry = offline_dry_run(
        device,
        intent,
        _Render(intent.custom.config_lines),
        running_config="hostname edge",
    )
    connection = _GenericConnection(["hostname edge", "logging host 192.0.2.50"])

    result = _adapter(
        CiscoIOSNtpAdapter,
        device,
        connection,
        {"approved_pre_change_state": dry.evidence["rollback_state"]},
    ).apply(intent, _Render(intent.custom.config_lines))

    assert result.status == "fail"
    assert result.evidence["write_started"] is False
    assert connection.config_calls == []
    assert connection.save_calls == 0


def test_custom_failed_verification_runs_reviewed_rollback_without_saving() -> None:
    device = _device("cisco_ios")
    intent = _intent(device.id)
    dry = offline_dry_run(
        device,
        intent,
        _Render(intent.custom.config_lines),
        running_config="hostname edge",
    )
    connection = _GenericConnection(["hostname edge"], ignore_first_config=True)

    result = _adapter(
        CiscoIOSNtpAdapter,
        device,
        connection,
        {"approved_pre_change_state": dry.evidence["rollback_state"]},
    ).apply(intent, _Render(intent.custom.config_lines))

    assert result.status == "fail"
    assert result.evidence["automatic_rollback"]["status"] == "pass"
    assert result.evidence["running_config_may_be_modified"] is False
    assert connection.save_calls == 0
    assert connection.config_calls[-1] == [intent.custom.rollback_lines]
