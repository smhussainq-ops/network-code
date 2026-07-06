"""Fleet rollout: wave planning, auto-halt contract, and drift collapse."""

from __future__ import annotations

from pathlib import Path

import pytest

from netcode import fleet
from netcode.bootstrap import init_workspace
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore


def _fleet_workspace(tmp_path: Path, device_count: int = 6) -> WorkspacePaths:
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    devices = "\n".join(
        f"""- id: sw{i}
  hostname: sw{i}
  host: 10.0.0.{i}
  groups:
  - stores
  site: store-{i}
"""
        for i in range(1, device_count + 1)
    )
    (paths.inventories / "lab.yaml").write_text(
        f"""lab_type: test
defaults:
  platform: arista_eos
  username: admin
  password: admin
  port: 22
devices:
{devices}
""",
        encoding="utf-8",
    )
    return paths


VALUES = {"vlan_id": 90, "name": "GUEST_WIFI", "device_group": "stores"}


def _plan(paths: WorkspacePaths, **overrides):
    kwargs = dict(
        change_type="add_vlan", values=VALUES, device_ids=None, device_group="stores",
        canary_size=1, batch_size=2, description="fleet test", requested_by="tester",
    )
    kwargs.update(overrides)
    return fleet.plan_fleet_rollout(paths, **kwargs)


def test_plan_builds_canary_then_batches_with_a_change_per_device(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=6)
    rollout = _plan(paths)
    assert rollout["status"] == "planned"
    assert rollout["device_count"] == 6
    waves = rollout["waves"]
    assert [len(w["targets"]) for w in waves] == [1, 2, 2, 1]  # canary + batches of 2
    assert waves[0]["label"] == "Canary"
    store = PlatformStore(paths)
    for wave in waves:
        for target in wave["targets"]:
            assert target["change_id"], "every device gets its own change record"
            change = store.get_change(target["change_id"])
            assert change.workflow_state == "validated"
            assert change.device_id == target["device_id"]


def test_plan_rejects_unknown_devices_instead_of_silently_dropping(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    with pytest.raises(ValueError, match="Unknown devices"):
        _plan(paths, device_ids=["sw1", "ghost9"], device_group=None)


def test_plan_rejects_zero_canary(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    with pytest.raises(ValueError, match="canary"):
        _plan(paths, canary_size=0)


def test_rollout_auto_halts_on_first_failure_and_skips_the_rest(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=6)
    rollout = _plan(paths)
    rollout_id = rollout["id"]
    store = PlatformStore(paths)
    store.update_rollout(rollout_id, status="running")

    touched: list[str] = []

    def fake_run_device(p, s, rid, target):
        touched.append(target["device_id"])
        failed = target["device_id"] == "sw3"
        s.update_rollout_target(rid, target["device_id"],
                                status="failed" if failed else "passed",
                                stage="apply" if failed else "done",
                                message="boom" if failed else "ok")
        return not failed

    monkeypatch.setattr(fleet, "_run_device", fake_run_device)
    fleet._run_rollout(paths, rollout_id)

    final = fleet.rollout_status(paths, rollout_id)
    assert final["status"] == "halted"
    assert "sw3" in (final["halt_reason"] or "")
    statuses = {t["device_id"]: t["status"] for w in final["waves"] for t in w["targets"]}
    # canary sw1 passed, batch1 sw2 passed + sw3 failed, everything after never touched
    assert statuses["sw1"] == "passed" and statuses["sw2"] == "passed"
    assert statuses["sw3"] == "failed"
    assert statuses["sw4"] == statuses["sw5"] == statuses["sw6"] == "skipped"
    assert "sw4" not in touched and "sw5" not in touched and "sw6" not in touched


def test_rollout_completes_when_every_wave_passes(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=4)
    rollout = _plan(paths)
    store = PlatformStore(paths)
    store.update_rollout(rollout["id"], status="running")
    monkeypatch.setattr(fleet, "_run_device", lambda p, s, rid, t: (
        s.update_rollout_target(rid, t["device_id"], status="passed", stage="done") or True))
    fleet._run_rollout(paths, rollout["id"])
    final = fleet.rollout_status(paths, rollout["id"])
    assert final["status"] == "completed"
    assert final["target_counts"] == {"passed": 4}


def test_operator_halt_request_stops_between_devices(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=4)
    rollout = _plan(paths)
    rollout_id = rollout["id"]
    store = PlatformStore(paths)
    store.update_rollout(rollout_id, status="running")

    def fake_run_device(p, s, rid, target):
        s.update_rollout_target(rid, target["device_id"], status="passed", stage="done")
        s.update_rollout(rid, status="halt_requested", halt_reason="operator said stop")
        return True

    monkeypatch.setattr(fleet, "_run_device", fake_run_device)
    fleet._run_rollout(paths, rollout_id)
    final = fleet.rollout_status(paths, rollout_id)
    assert final["status"] == "halted"
    assert final["target_counts"].get("skipped") == 3


def test_blocked_plan_cannot_start(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=2)
    rollout = _plan(paths)
    store = PlatformStore(paths)
    store.update_rollout(rollout["id"], status="blocked", halt_reason="policy")
    with pytest.raises(ValueError, match="blocked"):
        fleet.start_rollout(paths, rollout["id"])


def test_unknown_change_type_fails_before_any_rollout_row_exists(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    with pytest.raises(ValueError, match="Unknown change type"):
        _plan(paths, change_type="explode_network")
    assert PlatformStore(paths).list_rollouts() == []


def test_timeout_cancels_queued_job_so_runner_cannot_zombie_apply(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    store = PlatformStore(paths)
    rollout = _plan(paths)
    target = rollout["waves"][0]["targets"][0]
    job = store.queue_job(target["change_id"], "lab_apply", "store-lab", {"action": "apply"})
    cancelled = store.cancel_queued_jobs_for_change(target["change_id"], "rollout deadline")
    assert cancelled == 1
    assert store.get_job(job.id).status == "failed"
    # the poison pill is gone: a late runner has nothing to claim
    assert store.claim_next_job(rollout["org_id"], "store-lab", "runner-x") is None


def test_startup_reconciliation_fails_orphaned_rollouts_closed(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=3)
    store = PlatformStore(paths)
    rollout = _plan(paths)
    rollout_id = rollout["id"]
    store.update_rollout(rollout_id, status="running")
    first = rollout["waves"][0]["targets"][0]
    store.update_rollout_target(rollout_id, first["device_id"], status="running", stage="apply")
    orphan_job = store.queue_job(first["change_id"], "lab_apply", "store-lab", {"action": "apply"})

    assert fleet.reconcile_rollouts_on_startup(paths) == 1
    final = fleet.rollout_status(paths, rollout_id)
    assert final["status"] == "halted"
    assert "restarted" in final["halt_reason"]
    statuses = {t["device_id"]: t["status"] for w in final["waves"] for t in w["targets"]}
    assert statuses[first["device_id"]] == "failed"
    assert list(statuses.values()).count("skipped") == 2
    assert store.get_job(orphan_job.id).status == "failed"  # queued job cancelled


def test_drift_status_collapse():
    assert fleet._drift_status({"status": "in_sync"}, [{"vlan_id": 1}]) == "in_sync"
    assert fleet._drift_status({"status": "drifted"}, [{"vlan_id": 1}]) == "drifted"
    assert fleet._drift_status({"status": "unknown"}, [{"vlan_id": 1}]) == "unreachable"
    assert fleet._drift_status({"error": "boom"}, [{"vlan_id": 1}]) == "unreachable"
    assert fleet._drift_status({"status": "in_sync"}, []) == "no_baseline"
