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
    assert rollout["rez_change_id"].startswith("REZ-CHG-")
    assert rollout["audit"]["canonical_rollout_id"] == rollout["id"]
    assert rollout["audit"]["device_change_records"] == 6
    waves = rollout["waves"]
    assert [len(w["targets"]) for w in waves] == [1, 2, 2, 1]  # canary + batches of 2
    assert waves[0]["label"] == "Canary"
    store = PlatformStore(paths)
    for wave in waves:
        for target in wave["targets"]:
            assert target["change_id"], "every device gets its own change record"
            assert target["audit_ref"].startswith("REZ-DEV-")
            assert target["rez_change_id"] == rollout["rez_change_id"]
            change = store.get_change(target["change_id"])
            assert change.workflow_state == "validated"
            assert change.device_id == target["device_id"]
            plan_event = store.list_workflow_events(target["change_id"])[0]
            assert plan_event.evidence["rez_change_id"] == rollout["rez_change_id"]


def test_plan_rejects_unknown_devices_instead_of_silently_dropping(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    with pytest.raises(ValueError, match="Unknown devices"):
        _plan(paths, device_ids=["sw1", "ghost9"], device_group=None)


def test_plan_rejects_zero_canary(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    with pytest.raises(ValueError, match="canary"):
        _plan(paths, canary_size=0)


def test_delete_draft_soft_deletes_targets_but_retains_audit(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    rollout = _plan(paths)
    final = fleet.cancel_rollout(paths, rollout["id"], "reviewer@example.com")

    assert final["status"] == "cancelled"
    assert "audit tombstone retained" in final["halt_reason"]
    assert final["rez_change_id"] == rollout["rez_change_id"]
    store = PlatformStore(paths)
    for wave in final["waves"]:
        for target in wave["targets"]:
            assert target["status"] == "skipped"
            assert target["stage"] == "cancelled"
            change = store.get_change(target["change_id"])
            assert change.status == "cancelled"
            assert any(event.action == "delete_draft" for event in store.list_workflow_events(change.id))
    with pytest.raises(ValueError, match="only a planned rollout can start"):
        fleet.start_rollout(paths, rollout["id"])


def test_delete_rejects_executed_rollout(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=2)
    rollout = _plan(paths)
    PlatformStore(paths).update_rollout(rollout["id"], status="completed")
    with pytest.raises(ValueError, match="remain immutable for audit"):
        fleet.cancel_rollout(paths, rollout["id"], "reviewer@example.com")


def test_retry_creates_new_unapproved_rollout_for_failed_and_untouched_only(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=3)
    original = _plan(paths, batch_size=1)
    store = PlatformStore(paths)
    targets = store.list_rollout_targets(original["id"])
    store.update_rollout(original["id"], status="halted", halt_reason="canary failed")
    store.update_rollout_target(original["id"], targets[0]["device_id"], status="failed", stage="dry-run")
    store.update_rollout_target(original["id"], targets[1]["device_id"], status="skipped", stage="planned")
    store.update_rollout_target(original["id"], targets[2]["device_id"], status="passed", stage="done")

    retry = fleet.retry_rollout(
        paths,
        original["id"],
        scope="failed_and_untouched",
        requested_by="marcus",
    )

    assert retry["id"] != original["id"]
    assert retry["status"] == "planned"
    assert retry["parent_rollout_id"] == original["id"]
    assert retry["retry_scope"] == "failed_and_untouched"
    assert retry.get("approved_by") is None
    retried_devices = {
        target["device_id"] for wave in retry["waves"] for target in wave["targets"]
    }
    assert retried_devices == {targets[0]["device_id"], targets[1]["device_id"]}
    assert targets[2]["device_id"] not in retried_devices
    assert store.get_rollout(original["id"])["status"] == "halted"
    assert store.list_jobs() == []


def test_dry_run_failure_attaches_reusable_read_only_rez_handoff(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=1)
    rollout = _plan(paths, canary_size=1, batch_size=1)
    store = PlatformStore(paths)
    store.update_rollout(rollout["id"], status="running")
    target = store.list_rollout_targets(rollout["id"])[0]
    monkeypatch.setattr(fleet, "_lab_action_and_wait", lambda *args, **kwargs: (False, "candidate rejected"))

    assert fleet._run_device(paths, store, rollout["id"], target) is False
    change = store.get_change(target["change_id"])
    handoffs = list((change.result or {}).get("diagnostics_handoffs") or [])
    assert len(handoffs) == 1
    assert handoffs[0]["context"]["check"] == "fleet_dry_run"
    assert handoffs[0]["context"]["read_only"] is True
    assert handoffs[0]["safety"]["device_writes"] == "none"

    opened = fleet.rollout_failure_handoff(paths, rollout["id"])
    assert opened["failed_device"] == target["device_id"]
    assert opened["read_only"] is True
    assert "Do not apply configuration" in opened["question"]
    assert len((store.get_change(target["change_id"]).result or {}).get("diagnostics_handoffs") or []) == 1


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


# ── Approval gate ────────────────────────────────────────────────────────────

def test_apply_blocked_until_approved_when_approval_required(tmp_path: Path, monkeypatch):
    from netcode.jobs import JobRunner
    paths = _fleet_workspace(tmp_path, device_count=1)
    monkeypatch.setenv("NETCODE_REQUIRE_APPROVAL", "1")
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")  # queue path: no device I/O
    rollout = _plan(paths)
    target = rollout["waves"][0]["targets"][0]
    store = PlatformStore(paths)
    change = store.get_change(target["change_id"])
    # simulate the proven state
    store.record_workflow_event(change.id, "dry-run", change.workflow_state, "dry_run_passed", "proof", {})
    blocked = JobRunner(paths).run_lab_action(Path(target["intent_path"]), "apply", target["device_id"], change.id)
    assert blocked["ok"] is False
    assert blocked["result"]["approval_required"] is True
    # approve (second engineer), then apply queues
    store.record_workflow_event(change.id, "approve", "dry_run_passed", "approved", "Approved by reviewer.", {})
    queued = JobRunner(paths).run_lab_action(Path(target["intent_path"]), "apply", target["device_id"], change.id)
    assert queued["ok"] is True and queued["queued"] is True


def test_rollout_approval_requester_cannot_self_approve(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=2)
    rollout = _plan(paths)  # requested_by="tester"
    with pytest.raises(ValueError, match="cannot approve their own"):
        fleet.approve_rollout(paths, rollout["id"], "tester")
    approved = fleet.approve_rollout(paths, rollout["id"], "reviewer")
    assert approved["approved_by"] == "reviewer"


def test_rollout_start_requires_approval_when_gate_is_on(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=2)
    monkeypatch.setenv("NETCODE_REQUIRE_APPROVAL", "1")
    rollout = _plan(paths)
    with pytest.raises(ValueError, match="Approval gate"):
        fleet.start_rollout(paths, rollout["id"])
    fleet.approve_rollout(paths, rollout["id"], "reviewer")
    monkeypatch.setattr(fleet, "_run_rollout_safe", lambda p, rid: None)
    started = fleet.start_rollout(paths, rollout["id"])
    assert started["status"] == "running"


# ── Closed loop: drift -> remediation ────────────────────────────────────────

def test_remediation_targets_only_drifted_devices(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=4)
    org = "org_default"
    with fleet._DRIFT_LOCK:
        fleet._DRIFT_STATES[org] = {
            "status": "done", "started_at": "t", "finished_at": "t",
            "progress": {"done": 4, "total": 4}, "report_path": None,
            "devices": [
                {"device_id": "sw1", "status": "in_sync", "detail": {}},
                {"device_id": "sw2", "status": "drifted", "detail": {"vlans": [
                    {"vlan_id": 777, "name": "GUEST_WIFI", "status": "drifted"}]}},
                {"device_id": "sw3", "status": "drifted", "detail": {"vlans": [
                    {"vlan_id": 777, "name": "GUEST_WIFI", "status": "drifted"},
                    {"vlan_id": 90, "name": "GUEST_WIFI", "status": "in_sync"}]}},
                {"device_id": "sw4", "status": "unreachable", "detail": {}},
            ],
        }
    rollouts = fleet.create_remediation_rollouts(paths, org, requested_by="tester")
    assert len(rollouts) == 1
    remediation = rollouts[0]
    devices = sorted(t["device_id"] for w in remediation["waves"] for t in w["targets"])
    assert devices == ["sw2", "sw3"]
    assert "777" in remediation["description"]
    assert remediation["status"] == "planned"


def test_remediation_requires_a_finished_sweep(tmp_path: Path):
    paths = _fleet_workspace(tmp_path, device_count=1)
    with fleet._DRIFT_LOCK:
        fleet._DRIFT_STATES.pop("org_x", None)
    with pytest.raises(ValueError, match="drift sweep"):
        fleet.create_remediation_rollouts(paths, "org_x", requested_by="tester")


# ── Drift watch ──────────────────────────────────────────────────────────────

def test_drift_watch_toggle(tmp_path: Path, monkeypatch):
    paths = _fleet_workspace(tmp_path, device_count=1)
    monkeypatch.setattr(fleet, "start_fleet_drift", lambda *a, **k: None)
    from netcode.models import load_intent
    status = fleet.set_drift_watch(paths, "org_default", 30, load_intent)
    assert status == {"enabled": True, "minutes": 30}
    status = fleet.set_drift_watch(paths, "org_default", 0, load_intent)
    assert status == {"enabled": False, "minutes": 0}


# ── NTP pack ─────────────────────────────────────────────────────────────────

def test_ntp_pack_full_static_spine(tmp_path: Path):
    from netcode.orchestrator import create_desired_state_intent, run_static_pipeline
    paths = _fleet_workspace(tmp_path, device_count=1)
    intent_path = create_desired_state_intent(
        paths, change_type="ntp_standardize", site="store-1", device_id="sw1",
        requested_by="tester", values={"servers": "10.42.0.10, 10.42.0.11"})
    result = run_static_pipeline(paths, intent_path)
    assert result.status == "pass"
    rendered = result.render.config
    assert "ntp server 10.42.0.10 prefer" in rendered
    assert "ntp server 10.42.0.11" in rendered
    from netcode.change_types import spec_for
    from netcode.models import load_intent
    rollback = spec_for("ntp_standardize").rollback(load_intent(intent_path))
    assert "no ntp server 10.42.0.10" in rollback and "no ntp server 10.42.0.11" in rollback


def test_ntp_pack_blocks_rogue_server_against_approved_list(tmp_path: Path):
    from netcode.orchestrator import create_desired_state_intent, run_static_pipeline
    from netcode.ui_config import configured_policy_path
    paths = _fleet_workspace(tmp_path, device_count=1)
    policy_path = configured_policy_path(paths)
    policy_path.write_text(policy_path.read_text() + "\nntp:\n  approved_servers:\n  - 10.42.0.10\n", encoding="utf-8")
    intent_path = create_desired_state_intent(
        paths, change_type="ntp_standardize", site="store-1", device_id="sw1",
        requested_by="tester", values={"servers": "10.42.0.10, 6.6.6.6"})
    result = run_static_pipeline(paths, intent_path)
    assert result.status == "fail"
    failing = [c for c in result.validation.checks if c.status != "pass"]
    assert any("approved NTP server list" in c.message for c in failing)


def test_fleet_verify_failure_attaches_rez_handoff(tmp_path: Path, monkeypatch):
    from netcode.orchestrator import create_desired_state_intent, run_static_pipeline

    paths = _fleet_workspace(tmp_path, device_count=1)
    intent_path = create_desired_state_intent(
        paths,
        change_type="add_vlan",
        site="store-1",
        device_id="sw1",
        requested_by="tester",
        values={"vlan_id": 210, "name": "APP_210", "subnet": "10.210.0.0/24"},
    )
    pipeline = run_static_pipeline(paths, intent_path)
    store = PlatformStore(paths)
    change = store.create_change(intent_path, "sw1")
    store.update_change(change.id, "validated", pipeline.model_dump(), workflow_state="validated")

    monkeypatch.setattr(fleet, "execution_mode", lambda: "runner")
    monkeypatch.setattr(
        fleet,
        "_read_and_wait",
        lambda store, org_id, action, payload: {"ok": False, "message": "VLAN 210 missing after canary"},
    )

    ok, message = fleet._verify_device(paths, store, intent_path, "sw1", change.id)

    assert ok is False
    assert "missing" in message
    stored = store.get_change(change.id)
    handoff = stored.result["diagnostics_handoffs"][0]
    assert handoff["context"]["source"] == "netcode_verification"
    assert handoff["context"]["read_only"] is True
    assert handoff["safety"]["device_writes"] == "none"
