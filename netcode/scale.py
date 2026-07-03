"""Scale planning primitives for large network changes."""

from __future__ import annotations

from typing import Any

from netcode.inventory import Inventory
from netcode.paths import WorkspacePaths
from netcode.ui_config import configured_inventory_path


def rollout_plan(paths: WorkspacePaths, device_ids: list[str] | None = None, canary_size: int = 1, batch_size: int = 100) -> dict[str, Any]:
    inventory = Inventory(configured_inventory_path(paths))
    selected = [inventory.by_id[device_id] for device_id in device_ids or list(inventory.by_id) if device_id in inventory.by_id]
    canaries = selected[:canary_size]
    remaining = selected[canary_size:]
    batches = [remaining[index:index + batch_size] for index in range(0, len(remaining), batch_size)]
    return {
        "ok": True,
        "device_count": len(selected),
        "canary_size": len(canaries),
        "batch_size": batch_size,
        "canaries": [device.id for device in canaries],
        "batches": [[device.id for device in batch] for batch in batches],
        "controls": {
            "per_device_lock": True,
            "per_site_limit": 25,
            "per_vendor_limit": 100,
            "pause_on_failure": True,
            "idempotency_key_required": True,
            "retry_model": "bounded_retry_with_operator_review",
            "partial_success_handling": "record_success_failures_and_stop_next_batch",
        },
        "future_runtime": {
            "queue": "redis_rq_or_celery",
            "database": "postgresql",
            "artifact_store": "object_storage",
            "metrics": ["job_latency", "adapter_latency", "failure_rate", "evidence_collection_latency"],
        },
    }
