"""Fleet rollouts: one intent orchestrated across many devices as
canary -> batch waves with auto-halt on first failure.

Design: a rollout is NOT a new execution path. Every target device gets its
own change record and runs the existing single-change safety spine
(plan -> policy gate -> dry-run proof -> apply -> verify) through the normal
job queue, so per-device evidence and the state machine come for free. This
module only adds the wave structure, the sequencing, and the halt logic.

Also here: the fleet drift sweep — per-device drift (live state vs the
aggregate committed baseline) across the whole inventory in one pass.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from netcode.drift import aggregate_device_vlans
from netcode.inventory import Inventory
from netcode.jobs import JobRunner, execution_mode, runner_pool
from netcode.models import load_intent
from netcode.orchestrator import create_desired_state_intent, run_static_pipeline
from netcode.paths import WorkspacePaths
from netcode.scale import rollout_plan
from netcode.store import DEFAULT_ORG_ID, PlatformStore, record_to_dict
from netcode.ui_config import configured_inventory_path
from netcode.workflow import state_after_static_validation

# Per-action deadline for a queued runner job (connect + execute + report).
JOB_WAIT_SECONDS = 300
JOB_POLL_SECONDS = 2.0
# Verify/drift are synchronous read jobs; runner reads fail closed at 30s.
READ_WAIT_SECONDS = 90

_ROLLOUT_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()

_DRIFT_LOCK = threading.Lock()
# Keyed by org_id: one org's sweep must be invisible to another and must never
# block another org from refreshing its own.
_DRIFT_STATES: dict[str, dict[str, Any]] = {}


def _empty_drift_state() -> dict[str, Any]:
    return {"status": "never_run", "started_at": None, "finished_at": None,
            "progress": {"done": 0, "total": 0}, "devices": [], "report_path": None}


# ── Rollout planning ─────────────────────────────────────────────────────────

def plan_fleet_rollout(
    p: WorkspacePaths,
    *,
    change_type: str,
    values: dict[str, Any],
    device_ids: list[str] | None,
    device_group: str | None,
    canary_size: int,
    batch_size: int,
    description: str,
    requested_by: str,
    org_id: str = DEFAULT_ORG_ID,
    created_by_user_id: str | None = None,
) -> dict[str, Any]:
    """Create a rollout: resolve targets, compute waves, and build a per-device
    intent + change + static plan. Fail-closed: if ANY device fails the static
    pipeline (validation/policy), the rollout is blocked and cannot start."""
    if canary_size < 1:
        raise ValueError("canary_size must be at least 1 — the canary is the point.")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    inventory = Inventory(configured_inventory_path(p))

    from netcode.change_types import spec_for
    try:
        spec_for(change_type)  # unknown change type fails HERE, before any row exists
    except Exception as exc:
        raise ValueError(f"Unknown change type '{change_type}'.") from exc

    if device_ids:
        unknown = [d for d in device_ids if d not in inventory.by_id]
        if unknown:
            raise ValueError(f"Unknown devices (not in source of truth): {', '.join(unknown)}")
        selected = list(dict.fromkeys(device_ids))  # dedupe, keep order
    elif device_group:
        selected = [d.id for d in inventory.by_id.values() if device_group in d.groups]
        if not selected:
            raise ValueError(f"No devices in group '{device_group}'.")
    else:
        raise ValueError("Pick targets: device_ids or device_group.")

    waves_plan = rollout_plan(p, selected, canary_size=canary_size, batch_size=batch_size)
    waves: list[list[str]] = [waves_plan["canaries"]] + waves_plan["batches"]

    store = PlatformStore(p)
    rollout = store.create_rollout(
        description=description or f"{change_type} to {len(selected)} devices",
        change_type=change_type, values=values,
        canary_size=canary_size, batch_size=batch_size,
        requested_by=requested_by, org_id=org_id, created_by_user_id=created_by_user_id,
    )
    rollout_id = rollout["id"]

    blocked: list[str] = []
    try:
        for wave_index, wave in enumerate(waves):
            for device_id in wave:
                device = inventory.by_id[device_id]
                built_path = create_desired_state_intent(
                    p, change_type=change_type, site=device.site or "fleet",
                    device_id=device_id, requested_by=requested_by, values=values,
                )
                # Re-home the intent under a rollout-unique path: the default
                # intents/<site>/<slug>.yaml has no device/rollout in the name, so
                # two targets (or two rollouts) would silently share one file.
                intent_path = p.intents / "fleet" / rollout_id[:8] / f"{device_id}.yaml"
                intent_path.parent.mkdir(parents=True, exist_ok=True)
                intent_path.write_text(built_path.read_text(encoding="utf-8"), encoding="utf-8")
                change = store.create_change(
                    intent_path, device_id, requested_by=requested_by,
                    org_id=org_id, created_by_user_id=created_by_user_id,
                )
                store.add_rollout_target(rollout_id, device_id, wave_index,
                                         change_id=change.id, intent_path=str(intent_path))
                result = run_static_pipeline(p, intent_path)
                passed = result.status == "pass"
                workflow = state_after_static_validation(passed)
                store.update_change(change.id, "validated" if passed else "blocked",
                                    result.model_dump(), workflow_state=workflow.state)
                store.record_workflow_event(
                    change.id, "plan", "draft", workflow.state,
                    f"[fleet {rollout_id[:8]}] {workflow.message}",
                    {"rollout_id": rollout_id, "wave_index": wave_index},
                )
                if passed:
                    store.update_rollout_target(rollout_id, device_id, stage="planned",
                                                message="Static plan + policy gate passed.")
                else:
                    blocked.append(device_id)
                    failing = [c.message for c in result.validation.checks if c.status != "pass"][:2]
                    store.update_rollout_target(rollout_id, device_id, status="blocked", stage="plan",
                                                message="; ".join(failing) or "Static validation failed.")
    except Exception as exc:  # noqa: BLE001 — a half-planned rollout must be terminal, never startable
        store.update_rollout(rollout_id, status="blocked",
                             halt_reason=f"Planning failed: {type(exc).__name__}: {exc}")
        raise ValueError(f"Rollout planning failed: {exc}") from exc

    if blocked:
        store.update_rollout(rollout_id, status="blocked",
                             halt_reason=f"Policy/validation blocked on: {', '.join(blocked)}")
    return rollout_status(p, rollout_id)


def rollout_status(p: WorkspacePaths, rollout_id: str) -> dict[str, Any]:
    store = PlatformStore(p)
    rollout = store.get_rollout(rollout_id)
    targets = store.list_rollout_targets(rollout_id)
    wave_count = (max((t["wave_index"] for t in targets), default=-1)) + 1
    waves = []
    for index in range(wave_count):
        wave_targets = [t for t in targets if t["wave_index"] == index]
        waves.append({
            "index": index,
            "label": "Canary" if index == 0 else f"Batch {index}",
            "targets": wave_targets,
        })
    counts: dict[str, int] = {}
    for t in targets:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    rollout["waves"] = waves
    rollout["target_counts"] = counts
    rollout["device_count"] = len(targets)
    from netcode.jobs import approval_required
    rollout["approval_required"] = approval_required()
    return rollout


# ── Rollout execution ────────────────────────────────────────────────────────

def approve_rollout(p: WorkspacePaths, rollout_id: str, approved_by: str) -> dict[str, Any]:
    store = PlatformStore(p)
    rollout = store.get_rollout(rollout_id)
    if rollout["status"] != "planned":
        raise ValueError(f"Rollout is '{rollout['status']}' — only a planned rollout can be approved.")
    approver = (approved_by or "").strip()
    if not approver:
        raise ValueError("Approver identity is required.")
    if approver == rollout["requested_by"]:
        raise ValueError("The requester cannot approve their own rollout — a second engineer must approve.")
    rollout = store.approve_rollout(rollout_id, approver)
    for target in store.list_rollout_targets(rollout_id):
        if target.get("change_id"):
            change = store.get_change(str(target["change_id"]))
            store.record_workflow_event(
                str(target["change_id"]), "approve", change.workflow_state, change.workflow_state,
                f"Approved via rollout {rollout_id[:8]} by {approver}.",
                {"rollout_id": rollout_id, "approved_by": approver},
            )
    return rollout_status(p, rollout_id)


def start_rollout(p: WorkspacePaths, rollout_id: str) -> dict[str, Any]:
    from netcode.jobs import approval_required
    store = PlatformStore(p)
    rollout = store.get_rollout(rollout_id)
    if rollout["status"] not in ("planned",):
        raise ValueError(f"Rollout is '{rollout['status']}' — only a planned rollout can start.")
    if approval_required() and not rollout.get("approved_by"):
        raise ValueError("Approval gate: a second engineer must approve this rollout before it can start.")
    with _THREADS_LOCK:
        existing = _ROLLOUT_THREADS.get(rollout_id)
        if existing and existing.is_alive():
            raise ValueError("Rollout is already running.")
        store.update_rollout(rollout_id, status="running")
        thread = threading.Thread(target=_run_rollout_safe, args=(p, rollout_id),
                                  name=f"rollout-{rollout_id[:8]}", daemon=True)
        _ROLLOUT_THREADS[rollout_id] = thread
        thread.start()
    return rollout_status(p, rollout_id)


def request_halt(p: WorkspacePaths, rollout_id: str, reason: str) -> dict[str, Any]:
    # Compare-and-set: only a running rollout can move to halt_requested, so a
    # concurrent terminal write (completed/halted) is never clobbered.
    PlatformStore(p).update_rollout(rollout_id, status="halt_requested",
                                    halt_reason=reason or "Halted by operator.",
                                    expected_status="running")
    return rollout_status(p, rollout_id)


def reconcile_rollouts_on_startup(p: WorkspacePaths) -> int:
    """The orchestrator thread dies with the process. Any rollout still marked
    running/halt_requested after a restart is unowned: fail it closed — mark it
    halted, skip untouched targets, and cancel still-queued jobs so a runner can
    never execute them later."""
    store = PlatformStore(p)
    orphans = store.list_rollouts_in_status(("running", "halt_requested"))
    for rollout in orphans:
        rollout_id = str(rollout["id"])
        for target in store.list_rollout_targets(rollout_id):
            if target.get("change_id"):
                store.cancel_queued_jobs_for_change(
                    str(target["change_id"]), "control plane restarted mid-rollout (fail-closed)")
            if target["status"] in ("pending", "running"):
                store.update_rollout_target(
                    rollout_id, target["device_id"], status="failed" if target["status"] == "running" else "skipped",
                    message="Control plane restarted mid-rollout; fail-closed. Re-plan to continue.")
        store.update_rollout(rollout_id, status="halted",
                             halt_reason="Control plane restarted mid-rollout; halted fail-closed.")
    return len(orphans)


def _run_rollout_safe(p: WorkspacePaths, rollout_id: str) -> None:
    try:
        _run_rollout(p, rollout_id)
    except Exception:  # noqa: BLE001 — a crashed orchestrator must read as halted, not running forever
        try:
            PlatformStore(p).update_rollout(rollout_id, status="halted",
                                            halt_reason=f"Orchestrator error: {traceback.format_exc(limit=2)}")
        except Exception:  # noqa: BLE001 — nothing left to do; reconcile-on-startup will catch it
            pass


def _halt_requested(store: PlatformStore, rollout_id: str) -> bool:
    return store.get_rollout(rollout_id)["status"] == "halt_requested"


def _skip_remaining(store: PlatformStore, rollout_id: str) -> None:
    for t in store.list_rollout_targets(rollout_id):
        if t["status"] in ("pending",):
            store.update_rollout_target(rollout_id, t["device_id"], status="skipped",
                                        message="Skipped: rollout halted before this device was touched.")


def _run_rollout(p: WorkspacePaths, rollout_id: str) -> None:
    """Execute waves in order; inside a wave, devices run sequentially (the
    runner executes one job at a time anyway). First failure halts everything
    that has not started — that is the auto-halt contract."""
    store = PlatformStore(p)
    targets = store.list_rollout_targets(rollout_id)
    wave_count = (max((t["wave_index"] for t in targets), default=-1)) + 1

    for wave_index in range(wave_count):
        store.update_rollout(rollout_id, current_wave=wave_index)
        wave = [t for t in targets if t["wave_index"] == wave_index]
        for target in wave:
            if _halt_requested(store, rollout_id):
                store.update_rollout(rollout_id, status="halted")
                _skip_remaining(store, rollout_id)
                return
            ok = _run_device(p, store, rollout_id, target)
            if not ok:
                failed_device = target["device_id"]
                wave_label = "canary" if wave_index == 0 else f"batch {wave_index}"
                store.update_rollout(
                    rollout_id, status="halted",
                    halt_reason=f"Auto-halt: {failed_device} failed in the {wave_label} wave. "
                                f"No further devices were touched.",
                )
                _skip_remaining(store, rollout_id)
                return
    # Compare-and-set: a halt requested after the last device finished must win
    # over 'completed' (the operator asked to stop; say so honestly).
    final = store.update_rollout(rollout_id, status="completed", expected_status="running")
    if final["status"] == "halt_requested":
        store.update_rollout(rollout_id, status="halted")


def _run_device(p: WorkspacePaths, store: PlatformStore, rollout_id: str, target: dict[str, Any]) -> bool:
    """dry-run -> apply -> verify for one device. Any failure returns False."""
    device_id = target["device_id"]
    change_id = target["change_id"]
    intent_path = Path(target["intent_path"])
    store.update_rollout_target(rollout_id, device_id, status="running", stage="dry-run",
                                message="Dry-run proof on the device.")

    for action in ("dry-run", "apply"):
        if action == "apply":
            _inherit_rollout_approval(store, rollout_id, change_id)
        store.update_rollout_target(rollout_id, device_id, stage=action)
        ok, message = _lab_action_and_wait(p, store, intent_path, action, device_id, change_id)
        if not ok:
            store.update_rollout_target(rollout_id, device_id, status="failed", stage=action, message=message)
            return False

    store.update_rollout_target(rollout_id, device_id, stage="verify")
    ok, message = _verify_device(p, store, intent_path, device_id, change_id)
    if not ok:
        store.update_rollout_target(rollout_id, device_id, status="failed", stage="verify", message=message)
        return False
    store.update_rollout_target(rollout_id, device_id, status="passed", stage="done",
                                message="Applied and verified on the device.")
    return True


def _inherit_rollout_approval(store: PlatformStore, rollout_id: str, change_id: str) -> None:
    """A rollout is approved once, up front; each per-device change inherits that
    approval right before its apply (the change reaches dry_run_passed first, so
    the approved state must be stamped after the dry-run, not at approval time)."""
    from netcode.jobs import approval_required
    if not approval_required():
        return
    rollout = store.get_rollout(rollout_id)
    approver = rollout.get("approved_by")
    if not approver:
        return  # start_rollout blocks unapproved rollouts; fail-closed at jobs gate anyway
    change = store.get_change(change_id)
    if change.workflow_state == "dry_run_passed":
        store.record_workflow_event(
            change_id, "approve", change.workflow_state, "approved",
            f"Apply authorized by rollout {rollout_id[:8]} approval ({approver}).",
            {"rollout_id": rollout_id, "approved_by": approver},
        )


def _lab_action_and_wait(
    p: WorkspacePaths, store: PlatformStore, intent_path: Path,
    action: str, device_id: str, change_id: str,
) -> tuple[bool, str]:
    outcome = JobRunner(p).run_lab_action(intent_path, action, device_id, change_id)
    if not outcome.get("queued"):
        result = outcome.get("result") or {}
        return bool(outcome.get("ok")), str(result.get("message") or result.get("error") or "")
    job = outcome.get("job") or {}
    job_id = str(job.get("id"))
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while time.monotonic() < deadline:
        current = store.get_job(job_id)
        if current.status in ("completed", "failed"):
            result = current.result or {}
            return current.status == "completed", str(result.get("message") or current.message or "")
        time.sleep(JOB_POLL_SECONDS)
    # Fail-closed on timeout: cancel the job while it is still queued so an
    # offline runner can never claim it later and apply config AFTER the halt
    # (zombie apply). If the runner already claimed it, say so honestly.
    cancelled = store.cancel_queued_jobs_for_change(
        change_id, f"rollout deadline: {action} exceeded {JOB_WAIT_SECONDS}s")
    if cancelled:
        return False, f"{action} timed out after {JOB_WAIT_SECONDS}s; queued job cancelled (fail-closed)."
    return False, (f"{action} timed out after {JOB_WAIT_SECONDS}s while RUNNING on the runner — "
                   f"the device may still receive it; check job {job_id[:8]} before retrying.")


def _verify_device(
    p: WorkspacePaths, store: PlatformStore, intent_path: Path, device_id: str, change_id: str,
) -> tuple[bool, str]:
    org_id = store.get_change(change_id).org_id
    if execution_mode() == "runner":
        payload = {"intent_yaml": intent_path.read_text(encoding="utf-8"),
                   "device_id": device_id, "present": True}
        result = _read_and_wait(store, org_id, "verify", payload)
    else:
        from netcode.lab import AristaEOSLabAdapter  # local mode only (tests/lab-first)
        inventory = Inventory(configured_inventory_path(p))
        device = inventory.by_id.get(device_id)
        if not device:
            return False, f"Unknown device {device_id}"
        adapter = AristaEOSLabAdapter(device)
        try:
            adapter.connect()
            verification = adapter.verify_intent(load_intent(intent_path), present=True)
            result = {"ok": verification.status == "pass",
                      "message": getattr(verification, "message", "")}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                adapter.disconnect()
            except Exception:  # noqa: BLE001
                pass
    ok = bool(result.get("ok"))
    message = str(result.get("message") or result.get("error") or ("verified" if ok else "verification failed"))
    store.record_workflow_event(
        change_id, "fleet_verify",
        store.get_change(change_id).workflow_state, store.get_change(change_id).workflow_state,
        f"[fleet] verify on {device_id}: {'pass' if ok else 'fail'} — {message}",
        {"verify": result},
    )
    return ok, message


def _read_and_wait(store: PlatformStore, org_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = store.create_read_job(org_id, runner_pool(), action, payload)
    deadline = time.monotonic() + READ_WAIT_SECONDS
    while time.monotonic() < deadline:
        current = store.get_job(job.id)
        if current.status in ("completed", "failed"):
            return current.result or {"ok": current.status == "completed", "message": current.message}
        time.sleep(0.5)
    store.cancel_job_if_queued(job.id, f"read deadline: {action} exceeded {READ_WAIT_SECONDS}s")
    return {"ok": False, "error": f"No runner completed the {action} read within {READ_WAIT_SECONDS}s."}


# ── Fleet drift sweep ────────────────────────────────────────────────────────

def start_fleet_drift(p: WorkspacePaths, org_id: str, load_intent_fn: Callable[[Path], Any]) -> dict[str, Any]:
    with _DRIFT_LOCK:
        state = _DRIFT_STATES.setdefault(org_id, _empty_drift_state())
        if state["status"] == "running":
            return json.loads(json.dumps(state))
        inventory = Inventory(configured_inventory_path(p))
        device_ids = list(inventory.by_id)
        state.update({"status": "running", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "finished_at": None, "progress": {"done": 0, "total": len(device_ids)},
                      "devices": [], "report_path": None})
    thread = threading.Thread(target=_fleet_drift_sweep_safe, args=(p, org_id, device_ids, load_intent_fn),
                              name=f"fleet-drift-{org_id[:8]}", daemon=True)
    thread.start()
    return fleet_drift_snapshot(org_id)


def fleet_drift_snapshot(org_id: str) -> dict[str, Any]:
    with _DRIFT_LOCK:
        return json.loads(json.dumps(_DRIFT_STATES.get(org_id) or _empty_drift_state()))


def _fleet_drift_sweep_safe(p: WorkspacePaths, org_id: str, device_ids: list[str],
                            load_intent_fn: Callable[[Path], Any]) -> None:
    try:
        _fleet_drift_sweep(p, org_id, device_ids, load_intent_fn)
    except Exception as exc:  # noqa: BLE001 — a crashed sweep must never read as 'running' forever
        with _DRIFT_LOCK:
            state = _DRIFT_STATES.setdefault(org_id, _empty_drift_state())
            state.update({"status": "failed", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "error": f"{type(exc).__name__}: {exc}"})


def _fleet_drift_sweep(p: WorkspacePaths, org_id: str, device_ids: list[str],
                       load_intent_fn: Callable[[Path], Any]) -> None:
    store = PlatformStore(p)
    all_changes = [record_to_dict(c) for c in store.list_changes(limit=500, org_id=org_id)]
    results: list[dict[str, Any]] = []
    for device_id in device_ids:
        device_changes = [c for c in all_changes if c.get("device_id") == device_id]
        expected = aggregate_device_vlans(device_changes, load_intent_fn)
        try:
            if execution_mode() == "runner":
                report = _read_and_wait(store, org_id, "device_drift",
                                        {"device_id": device_id, "expected": expected})
            else:
                from netcode.adapters.registry import AdapterRegistry
                from netcode.drift import device_drift_from_state
                inventory = Inventory(configured_inventory_path(p))
                device = inventory.by_id.get(device_id)
                state = AdapterRegistry().rez.collect_device_state(device) if device else {"ok": False}
                report = device_drift_from_state(expected, state, device_id)
        except Exception as exc:  # noqa: BLE001
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        entry = {
            "device_id": device_id,
            "status": _drift_status(report, expected),
            "expected_count": len(expected),
            "message": str(report.get("message") or report.get("error") or ""),
            "detail": report,
        }
        results.append(entry)
        with _DRIFT_LOCK:
            org_state = _DRIFT_STATES.setdefault(org_id, _empty_drift_state())
            org_state["progress"]["done"] += 1
            org_state["devices"] = list(results)
    report_path = p.reports / f"fleet-drift-{time.strftime('%Y%m%d-%H%M%S')}.json"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"org_id": org_id, "devices": results}, indent=2, default=str),
                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        report_path = None  # type: ignore[assignment]
    with _DRIFT_LOCK:
        org_state = _DRIFT_STATES.setdefault(org_id, _empty_drift_state())
        org_state.update({"status": "done", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "report_path": str(report_path) if report_path else None})


def _drift_status(report: dict[str, Any], expected: list[dict[str, Any]]) -> str:
    """Collapse a device_drift_from_state report to one fleet-dashboard word."""
    status = str(report.get("status") or "")
    if report.get("error") or status == "unknown":
        return "unreachable"
    if not expected:
        return "no_baseline"
    return status or "unreachable"


# ── Closed loop: drift finding -> remediation rollout ───────────────────────

def create_remediation_rollouts(
    p: WorkspacePaths, org_id: str, requested_by: str,
    created_by_user_id: str | None = None,
    canary_size: int = 1, batch_size: int = 3,
) -> list[dict[str, Any]]:
    """Turn the latest drift sweep's findings into governed remediation plans:
    one rollout per violated intent, targeting ONLY the devices that drifted
    from it. The rollout then runs the full spine (plan/policy/dry-run/apply/
    verify) — remediation is never a blind push."""
    snapshot = fleet_drift_snapshot(org_id)
    if snapshot["status"] not in ("done",):
        raise ValueError("Run a fleet drift sweep first — remediation plans are built from its findings.")
    missing: dict[tuple[int, str], list[str]] = {}
    for device in snapshot.get("devices", []):
        if device.get("status") != "drifted":
            continue
        for vlan in (device.get("detail") or {}).get("vlans", []):
            if vlan.get("status") == "drifted":
                key = (int(vlan["vlan_id"]), str(vlan.get("name") or f"VLAN_{vlan['vlan_id']}"))
                missing.setdefault(key, []).append(str(device["device_id"]))
    if not missing:
        raise ValueError("No drifted intents in the last sweep — nothing to remediate.")
    rollouts = []
    for (vlan_id, name), device_ids in sorted(missing.items()):
        rollouts.append(plan_fleet_rollout(
            p, change_type="add_vlan", values={"vlan_id": vlan_id, "name": name},
            device_ids=sorted(set(device_ids)), device_group=None,
            canary_size=min(canary_size, len(set(device_ids))), batch_size=batch_size,
            description=f"Remediation: restore VLAN {vlan_id} ({name}) on {len(set(device_ids))} drifted device(s)",
            requested_by=requested_by, org_id=org_id, created_by_user_id=created_by_user_id,
        ))
    return rollouts


# ── Continuous drift watch ───────────────────────────────────────────────────

_WATCH_LOCK = threading.Lock()
_WATCH_STATE: dict[str, dict[str, Any]] = {}  # org_id -> {minutes, thread, stop}


def set_drift_watch(p: WorkspacePaths, org_id: str, minutes: int,
                    load_intent_fn: Callable[[Path], Any]) -> dict[str, Any]:
    """Enable/disable the periodic fleet sweep for an org. minutes=0 turns it off."""
    with _WATCH_LOCK:
        current = _WATCH_STATE.get(org_id)
        if current:
            current["stop"].set()
            _WATCH_STATE.pop(org_id, None)
        if minutes > 0:
            stop = threading.Event()
            thread = threading.Thread(
                target=_drift_watch_loop, args=(p, org_id, minutes, stop, load_intent_fn),
                name=f"drift-watch-{org_id[:8]}", daemon=True)
            _WATCH_STATE[org_id] = {"minutes": minutes, "thread": thread, "stop": stop}
            thread.start()
    return drift_watch_status(org_id)


def drift_watch_status(org_id: str) -> dict[str, Any]:
    with _WATCH_LOCK:
        state = _WATCH_STATE.get(org_id)
        return {"enabled": bool(state), "minutes": state["minutes"] if state else 0}


def _drift_watch_loop(p: WorkspacePaths, org_id: str, minutes: int,
                      stop: threading.Event, load_intent_fn: Callable[[Path], Any]) -> None:
    # Sweep immediately, then every interval until stopped.
    while not stop.is_set():
        try:
            start_fleet_drift(p, org_id, load_intent_fn)
        except Exception:  # noqa: BLE001 — the watch itself must never die
            pass
        if stop.wait(timeout=minutes * 60):
            return
