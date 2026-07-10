"""Synchronous job runner with durable state.

The interface is job-shaped now so it can be moved behind a queue/worker pool
without changing the API/UI contract later. Phase 0 SaaS split: with
NETCODE_EXECUTION=runner, lab actions are gate-checked here (the cloud gate)
and then QUEUED for an on-prem runner instead of executed locally.
"""

from __future__ import annotations

import os
from pathlib import Path

from netcode.inventory import Inventory
from netcode.lab import run_arista_end_to_end, run_lab_action
from netcode.models import load_intent
from netcode.paths import WorkspacePaths
from netcode.rendering import render_intent
from netcode.store import PlatformStore, record_to_dict
from netcode.ui_config import configured_inventory_path, configured_policy_path
from netcode.workflow import require_action_allowed, state_after_lab_action
from netcode.yamlio import read_yaml


def execution_mode() -> str:
    """'local' (default): execute lab actions in-process. 'runner': queue for an on-prem runner."""
    return os.environ.get("NETCODE_EXECUTION", "local").strip().lower() or "local"


def approval_required() -> bool:
    """Apply requires a second engineer's approval. Explicit env wins; otherwise
    approval is on exactly when auth is on (identities exist to tell requester
    from approver). Solo/local mode stays frictionless."""
    raw = os.environ.get("NETCODE_REQUIRE_APPROVAL", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    from netcode.auth import auth_enabled
    return auth_enabled()


def intrinsic_approval_required(intent_path: Path) -> bool:
    """Machine-sourced drafts carry their own approval gate, independent of env."""
    try:
        intent = read_yaml(intent_path)
    except Exception:
        return False
    metadata = intent.get("metadata") if isinstance(intent, dict) else {}
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("human_approval_required")) or str(metadata.get("source") or "").strip().lower() == "rez_rca"


def runner_pool() -> str:
    return os.environ.get("NETCODE_RUNNER_POOL", "store-lab").strip() or "store-lab"


class JobRunner:
    def __init__(self, paths: WorkspacePaths, store: PlatformStore | None = None):
        self.paths = paths
        self.store = store or PlatformStore(paths)

    def run_full_arista(self, intent_path: Path, device_id: str | None, apply: bool = True) -> dict[str, object]:
        change = self.store.create_change(intent_path, device_id)
        job = self.store.create_job(change.id, "arista_full_run")
        self.store.update_job(job.id, "running", "Running static validation and Arista lab phases")
        try:
            result = run_arista_end_to_end(self.paths, intent_path, device_id, apply=apply)
            result_payload = result.model_dump()
            status = "completed" if result.status == "pass" else "failed"
            self.store.update_change(change.id, status, result_payload)
            final_job = self.store.update_job(job.id, status, f"Full run {result.status}", result_payload)
            return {
                "ok": result.status == "pass",
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": result_payload,
            }
        except Exception as exc:
            error = {"error": f"{type(exc).__name__}: {exc}"}
            self.store.update_change(change.id, "failed", error)
            final_job = self.store.update_job(job.id, "failed", str(exc), error)
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": error,
            }

    def run_lab_action(self, intent_path: Path, action: str, device_id: str | None, change_id: str | None = None) -> dict[str, object]:
        change = self.store.get_change(change_id) if change_id else self.store.get_or_create_change(intent_path, device_id)
        intrinsic_gate = intrinsic_approval_required(intent_path)
        needs_approval = approval_required() or intrinsic_gate
        if action == "apply" and needs_approval and change.workflow_state != "approved":
            message = ("Approval gate: a second engineer must approve this change before apply. "
                       "The requester cannot approve their own change.")
            self.store.record_workflow_event(change.id, "apply", change.workflow_state, change.workflow_state,
                                             message, {"blocked": True, "approval_required": True, "intrinsic_approval_required": intrinsic_gate})
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": None,
                "result": {"status": "fail", "message": message,
                           "workflow_state": change.workflow_state, "approval_required": True},
            }
        try:
            require_action_allowed(change.workflow_state, action)
        except Exception as exc:
            blocked = {"status": "fail", "message": str(exc), "workflow_state": change.workflow_state}
            self.store.record_workflow_event(
                change.id,
                action,
                change.workflow_state,
                "blocked",
                str(exc),
                {"blocked": True, "action": action},
            )
            self.store.update_change(change.id, "blocked", blocked, workflow_state="blocked")
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": None,
                "result": blocked,
            }

        if execution_mode() == "runner":
            payload, pool, target_runner_id = self._runner_job_spec(
                intent_path,
                action,
                device_id,
                change.id,
                change.org_id,
            )
            job = self.store.queue_job(
                change.id,
                f"lab_{action}",
                pool,
                payload,
                target_runner_id=target_runner_id,
            )
            self.store.record_workflow_event(
                change.id,
                action,
                change.workflow_state,
                change.workflow_state,
                f"Queued lab {action} for runner pool '{pool}'.",
                {"job_id": job.id, "queued": True, "pool": pool},
            )
            return {
                "ok": True,
                "queued": True,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(job),
                "result": {
                    "status": "queued",
                    "message": f"Queued for runner pool '{pool}'. The on-prem runner executes it and reports back with signed evidence.",
                },
            }

        job = self.store.create_job(change.id, f"lab_{action}")
        self.store.update_job(job.id, "running", f"Running lab {action}")
        try:
            result = run_lab_action(self.paths, intent_path, action, device_id)  # type: ignore[arg-type]
            status = "completed" if result.get("status") == "pass" else "failed"
            workflow = state_after_lab_action(action, result.get("status") == "pass")
            self.store.update_change(change.id, status, result, workflow_state=workflow.state)
            self.store.record_workflow_event(
                change.id,
                action,
                change.workflow_state,
                workflow.state,
                str(result.get("message", "")),
                {"job_id": job.id, "status": status},
            )
            final_job = self.store.update_job(job.id, status, str(result.get("message", "")), result)
            return {
                "ok": result.get("status") == "pass",
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": result,
            }
        except Exception as exc:
            error = {"error": f"{type(exc).__name__}: {exc}"}
            self.store.update_change(change.id, "failed", error, workflow_state="failed")
            self.store.record_workflow_event(change.id, action, change.workflow_state, "failed", str(exc), error)
            final_job = self.store.update_job(job.id, "failed", str(exc), error)
            return {
                "ok": False,
                "change": record_to_dict(self.store.get_change(change.id)),
                "job": record_to_dict(final_job),
                "result": error,
            }

    def _runner_job_spec(
        self,
        intent_path: Path,
        action: str,
        device_id: str | None,
        change_id: str,
        org_id: str,
    ) -> tuple[dict[str, object], str, str | None]:
        """Build the job spec shipped to the runner. Deliberately credential-free:
        the runner resolves credentials from its own local store by device id.

        Legacy YAML inventory remains supported. Devices learned from a runner's
        public catalog are routed back to that exact runner, rather than merely
        to any connector that happens to share its pool.
        """
        intent = load_intent(intent_path)
        render = render_intent(intent, self.paths)
        inventory = Inventory(configured_inventory_path(self.paths))
        requested_id = str(device_id or "").strip()
        if not requested_id and intent.targets.device_ids:
            requested_id = str(intent.targets.device_ids[0]).strip()

        device = inventory.find_device(requested_id) if requested_id else None
        target_runner_id: str | None = None
        pool = runner_pool()
        if device is not None:
            public_device = {
                "id": device.id,
                "host": device.host,
                "platform": device.platform,
                "port": device.port,
            }
        else:
            catalog_device = self.store.resolve_device(org_id, requested_id) if requested_id else None
            if catalog_device is None:
                # Group-only legacy intents still resolve through the YAML source
                # of truth. Catalog-backed execution requires one exact target.
                if not requested_id:
                    legacy_target = inventory.resolve_targets(intent.targets, site=intent.site)[0]
                    public_device = {
                        "id": legacy_target.id,
                        "host": legacy_target.host,
                        "platform": legacy_target.platform,
                        "port": legacy_target.port,
                    }
                else:
                    raise ValueError(f"Unknown target device(s): {requested_id}")
            else:
                public_device = {
                    "id": str(catalog_device["canonical_id"]),
                    "host": str(catalog_device["host"]),
                    "platform": str(catalog_device["platform"]),
                    "port": int(catalog_device["port"]),
                }
                pool = str(catalog_device["runner_pool"])
                target_runner_id = str(catalog_device["runner_id"])
        policy_path = configured_policy_path(self.paths)
        payload = {
            "action": action,
            "change_id": change_id,
            "device": public_device,
            "intent_yaml": intent_path.read_text(encoding="utf-8"),
            "rendered_config": render.config,
            "policy_yaml": policy_path.read_text(encoding="utf-8") if policy_path.exists() else "",
        }
        return payload, pool, target_runner_id

    def _runner_payload(self, intent_path: Path, action: str, device_id: str | None, change_id: str) -> dict[str, object]:
        """Backward-compatible payload helper for legacy callers and tests."""
        payload, _, _ = self._runner_job_spec(intent_path, action, device_id, change_id, "org_default")
        return payload
