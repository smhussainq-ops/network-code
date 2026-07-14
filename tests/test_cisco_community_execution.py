from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from netcode.adapters.registry import AdapterRegistry
from netcode.bootstrap import init_workspace
from netcode.fleet import plan_fleet_rollout
from netcode.jobs import JobRunner
from netcode.orchestrator import create_desired_state_intent, run_static_pipeline
from netcode.paths import WorkspacePaths
from netcode.runner_agent import _runner_workspace_root
from netcode.store import PlatformStore


def _workspace(tmp_path: Path) -> WorkspacePaths:
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    (paths.inventories / "lab.yaml").write_text(
        """lab_type: cisco_gns3
defaults:
  platform: cisco_ios
  username: local-user
  password: local-password
  port: 22
devices:
- id: gns3-r1
  hostname: gns3-r1
  host: 192.0.2.10
  groups: [community]
  site: site-101
""",
        encoding="utf-8",
    )
    return paths


@pytest.mark.parametrize("alias", ["ios", "iosxe", "ios-xe", "cisco_xe", "cisco_iosxe", "cisco_ios_xe"])
def test_cisco_ios_xe_aliases_share_one_governed_adapter(alias: str) -> None:
    assert AdapterRegistry.normalize_execution_platform(alias) == "cisco_ios"


def test_cisco_ntp_pipeline_uses_cisco_template(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    intent_path = create_desired_state_intent(
        paths,
        change_type="ntp_standardize",
        site="site-101",
        device_id="gns3-r1",
        requested_by="marcus",
        values={"servers": "10.0.0.10,10.0.0.11", "prefer_first": True},
    )

    result = run_static_pipeline(paths, intent_path, platform="cisco_ios")

    assert result.status == "pass"
    assert "/cisco_ios/ntp_standardize.j2" in result.render.template_path
    assert result.render.config == "ntp server 10.0.0.10 prefer\nntp server 10.0.0.11\n"


def test_runner_template_root_is_independent_of_launch_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NETCODE_RUNNER_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert _runner_workspace_root() == Path(__file__).resolve().parents[1]


def test_cisco_vlan_rollout_is_blocked_before_creating_a_rollout(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)

    with pytest.raises(ValueError, match="Governed 'add_vlan' execution is not available"):
        plan_fleet_rollout(
            paths,
            change_type="add_vlan",
            values={"vlan_id": 90, "name": "COMMUNITY"},
            device_ids=["gns3-r1"],
            device_group=None,
            canary_size=1,
            batch_size=1,
            description="unsupported Cisco rollout",
            requested_by="marcus",
        )

    assert PlatformStore(paths).list_rollouts() == []


def test_control_plane_carries_signed_dry_run_state_to_apply_and_apply_state_to_rollback(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    intent_path = create_desired_state_intent(
        paths,
        change_type="ntp_standardize",
        site="site-101",
        device_id="gns3-r1",
        requested_by="marcus",
        values={"servers": "10.0.0.10", "prefer_first": True},
    )
    store = PlatformStore(paths)
    change = store.create_change(intent_path, "gns3-r1", requested_by="marcus")
    state = {
        "schema": "netcode.ntp-pre-change.v1",
        "device_id": "gns3-r1",
        "platform": "cisco_ios",
        "managed_servers": ["10.0.0.10"],
        "prior_lines": {},
        "fingerprint": "reviewed",
    }
    dry_job = store.create_job(change.id, "lab_dry-run")
    store.update_job(dry_job.id, "completed", "passed", {"status": "pass", "evidence": {"rollback_state": state}})
    runner = JobRunner(paths, store=store)

    apply_context = runner._operation_context(
        change_id=change.id,
        org_id=change.org_id,
        action="apply",
        change_type="ntp_standardize",
    )
    assert apply_context == {"approved_pre_change_state": state}

    apply_job = store.create_job(change.id, "lab_apply")
    store.update_job(apply_job.id, "completed", "passed", {"status": "pass", "evidence": {"rollback_state": state}})
    rollback_context = runner._operation_context(
        change_id=change.id,
        org_id=change.org_id,
        action="rollback",
        change_type="ntp_standardize",
    )
    assert rollback_context == {"rollback_state": state}


def test_apply_is_blocked_when_reviewed_dry_run_state_is_missing(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    store = PlatformStore(paths)
    intent_path = create_desired_state_intent(
        paths,
        change_type="ntp_standardize",
        site="site-101",
        device_id="gns3-r1",
        requested_by="marcus",
        values={"servers": "10.0.0.10"},
    )
    change = store.create_change(intent_path, "gns3-r1", requested_by="marcus")

    with pytest.raises(ValueError, match="Exact pre-change NTP evidence is unavailable"):
        JobRunner(paths, store=store)._operation_context(
            change_id=change.id,
            org_id=change.org_id,
            action="apply",
            change_type="ntp_standardize",
        )


def test_newer_failed_dry_run_invalidates_older_successful_evidence(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    runner = JobRunner(paths, store=PlatformStore(paths))
    state = {"schema": "netcode.ntp-pre-change.v1"}
    runner.store.list_jobs = lambda **_kwargs: [  # type: ignore[method-assign]
        SimpleNamespace(change_id="chg-1", action="lab_dry-run", status="failed", result={}),
        SimpleNamespace(
            change_id="chg-1",
            action="lab_dry-run",
            status="completed",
            result={"evidence": {"rollback_state": state}},
        ),
    ]

    assert runner._ntp_state_from_job("chg-1", "dry-run", "org-1") is None
