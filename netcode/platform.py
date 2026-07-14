"""Platform capability summary for the simple UI."""

from __future__ import annotations

from netcode.adapters.registry import AdapterRegistry
from netcode.inventory import Inventory
from netcode.paths import WorkspacePaths
from netcode.product_capabilities import product_support_matrix
from netcode.store import PlatformStore
from netcode.ui_config import configured_inventory_path, configured_policy_path, configured_template_dir, read_ui_config


def platform_capabilities(paths: WorkspacePaths) -> dict[str, object]:
    config = read_ui_config(paths)
    inventory_path = configured_inventory_path(paths)
    policy_path = configured_policy_path(paths)
    template_dir = configured_template_dir(paths)
    inventory = Inventory(inventory_path)
    adapters = AdapterRegistry().summary()
    support_matrix = product_support_matrix()
    jobs = PlatformStore(paths).list_jobs(limit=1)
    latest_job = jobs[0] if jobs else None
    sot_summary = {
        "provider": config.get("source_of_truth", {}).get("provider"),
        "inventory": str(inventory_path),
        "policies": str(policy_path),
        "templates": str(template_dir / "arista"),
        "device_count": len(inventory.devices),
        "sites": sorted({device.site for device in inventory.devices if device.site}),
    }

    items = [
        ("source_of_truth", "Source of Truth", "inventory, policy, and template files are the trusted model for this lab slice", sot_summary),
        ("intent_model", "Intent Model", "change requests are captured as structured add_vlan intent", {"workflow": "add_vlan"}),
        ("policy_guardrails", "Policy And Guardrails", "static validator blocks unsafe requests before device contact", {"checks": 7}),
        ("config_generation", "Config Generation", "Jinja renders vendor config from intent and source-of-truth data", {"template": "templates/arista/add_vlan.j2"}),
        ("validation_pipeline", "Validation Pipeline", "schema, target, VLAN, subnet, segmentation, scope, and deterministic render checks run every time", {"fail_closed": True}),
        ("change_workflow", "Change Workflow", "request -> safety check -> dry-run -> apply -> verify -> record", {"ui_locked_apply": True}),
        ("device_adapters", "Device Adapters", "Arista execution adapter plus Rez state adapter registry", adapters),
        ("state_collection", "State Collection", "Rez bridge can collect live device state where Rez dependencies are available", {"provider": "rez"}),
        ("drift_detection", "Drift Detection", "candidate config and verification evidence expose drift for this workflow", {"scope": "workflow-level"}),
        ("evidence_audit", "Evidence And Audit", "reports, jobs, diffs, validation, dry-run, and verification are persisted", {"latest_job": latest_job.id if latest_job else None}),
        ("approval_rbac", "Approval And RBAC", "UI and API keep apply locked until safety checks and dry-run proof pass", {"production_auth_required": True}),
        ("rollback_plan", "Rollback Plan", "rollback action generates and applies no-vlan compensation for this workflow", {"action": "lab rollback"}),
        ("lab_testing", "Lab / Pre-Production Testing", "ORB containerlab Arista cEOS lab is the pre-production proof target", {"lab_type": inventory.raw.get("lab_type")}),
        ("ui_api", "UI And API", "same workflow is exposed through FastAPI, CLI, and a simplified UI", {"api": True, "cli": True, "ui": True}),
        ("reports", "Reports", "Markdown and JSON reports are generated for static and end-to-end runs", {"directory": str(paths.reports)}),
    ]

    return {
        "ok": True,
        "summary": "Safe, reviewable, evidence-backed network changes.",
        "source_of_truth": sot_summary,
        "deliverables": [
            {
                "id": item_id,
                "name": name,
                "status": "implemented_lab_slice",
                "simple_meaning": meaning,
                "evidence": evidence,
            }
            for item_id, name, meaning, evidence in items
        ],
        "support_matrix": support_matrix,
    }
