"""FastAPI backend for the UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from netcode.ai_assistant import assistant_response
from netcode.adapters.registry import AdapterRegistry
from netcode.bootstrap import init_workspace
from netcode.discovery import DiscoveryService
from netcode.drift import compliance_summary, vlan_drift_report
from netcode.gitflow import git_workspace_status, setup_git_workspace
from netcode.gitops import gitops_plan
from netcode.inventory import Inventory
from netcode.intent_utils import lab_write_supported, plan_metadata, production_write_supported
from netcode.jobs import JobRunner
from netcode.lab import AristaEOSLabAdapter, lab_status, run_arista_end_to_end, run_lab_action
from netcode.models import load_intent
from netcode.orchestrator import create_add_vlan_intent, create_desired_state_intent, run_static_pipeline
from netcode.paths import paths
from netcode.platform import platform_capabilities
from netcode.scale import rollout_plan
from netcode.source_of_truth import provider_catalog, source_of_truth
from netcode.store import PlatformStore, record_to_dict
from netcode.ui_config import (
    configured_inventory_path,
    configured_template_dir,
    desired_state_catalog_from_config,
    read_ui_config,
    reset_ui_config,
    ui_config_history,
    ui_config_path,
    write_ui_config,
)
from netcode.verification import verify_state, verify_vlan_state
from netcode.workflow import state_after_lab_action, state_after_static_validation, workflow_snapshot


class AddVlanRequest(BaseModel):
    site: str = "store-1842"
    device_id: str = "v2-store1"
    vlan_id: int = 90
    name: str = "GUEST_WIFI"
    subnet: str = "10.42.90.0/24"
    purpose: str = "guest"
    pci_reachable: bool = False
    requested_by: str = "lab-engineer"


class DesiredStatePlanRequest(BaseModel):
    change_type: str = "add_vlan"
    site: str = "store-1842"
    device_id: str = "v2-store1"
    requested_by: str = "lab-engineer"
    values: dict[str, object] = {}


class IntentPathRequest(BaseModel):
    intent_path: str
    device_id: str | None = None
    change_id: str | None = None


class DeviceRequest(BaseModel):
    device_id: str


class DiscoveryScanRequest(BaseModel):
    host: str
    username: str = ""
    password: str = ""
    platform: str = ""
    port: int = 22
    device_id: str = ""
    site: str = ""
    groups: list[str] = []


class SourceOfTruthDeviceImportRequest(BaseModel):
    candidate: dict[str, object]


class VlanVerifyRequest(BaseModel):
    device_id: str
    vlan_id: int
    name: str | None = None
    present: bool = True


class GenericVerifyRequest(BaseModel):
    device_id: str
    check: str
    params: dict[str, object] = {}


class AssistantRequest(BaseModel):
    prompt: str
    context: dict[str, object] = {}


class ScalePlanRequest(BaseModel):
    device_ids: list[str] | None = None
    canary_size: int = 1
    batch_size: int = 100


class UiConfigRequest(BaseModel):
    config: dict[str, object]


class GitSetupRequest(BaseModel):
    repo_url: str = ""
    branch: str = ""


app = FastAPI(title="Netcode Platform", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_workspace(paths())


@app.get("/")
def index() -> FileResponse:
    static = paths().static / "index.html"
    if not static.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    return FileResponse(static)


@app.get("/app")
@app.get("/app/")
def app_index() -> FileResponse:
    return index()


@app.get("/api/health")
def health() -> dict[str, object]:
    p = paths()
    return {
        "ok": True,
        "workspace": str(p.root),
        "lab": lab_status(),
    }


@app.post("/api/init")
def init() -> dict[str, object]:
    written = init_workspace(paths())
    return {"ok": True, "written": [str(p) for p in written]}


@app.get("/api/config/ui")
def api_get_ui_config() -> dict[str, object]:
    p = paths()
    return {
        "ok": True,
        "path": str(ui_config_path(p)),
        "config": read_ui_config(p),
        "history": ui_config_history(p),
    }


@app.post("/api/config/ui")
def api_save_ui_config(request: UiConfigRequest) -> dict[str, object]:
    p = paths()
    try:
        config = write_ui_config(p, request.config, actor="ui")
        return {
            "ok": True,
            "path": str(ui_config_path(p)),
            "config": config,
            "history": ui_config_history(p),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/config/ui/reset")
def api_reset_ui_config() -> dict[str, object]:
    p = paths()
    config = reset_ui_config(p, actor="ui")
    return {
        "ok": True,
        "path": str(ui_config_path(p)),
        "config": config,
        "history": ui_config_history(p),
    }


@app.get("/api/config/ui/history")
def api_ui_config_history() -> dict[str, object]:
    return {"history": ui_config_history(paths())}


@app.post("/api/wizard/add-vlan")
def wizard_add_vlan(request: AddVlanRequest) -> dict[str, object]:
    p = paths()
    try:
        intent_path = create_add_vlan_intent(
            p,
            site=request.site,
            device_id=request.device_id,
            vlan_id=request.vlan_id,
            name=request.name,
            subnet=request.subnet,
            purpose=request.purpose,
            pci_reachable=request.pci_reachable,
            requested_by=request.requested_by,
        )
        result = run_static_pipeline(p, intent_path)
        store = PlatformStore(p)
        change = store.get_or_create_change(intent_path, request.device_id, requested_by=request.requested_by)
        workflow = state_after_static_validation(result.status == "pass")
        store.update_change(change.id, "validated" if result.status == "pass" else "blocked", result.model_dump(), workflow_state=workflow.state)
        store.record_workflow_event(
            change.id,
            "check_safety",
            change.workflow_state,
            workflow.state,
            workflow.message,
            {"intent_path": str(intent_path), "checks": len(result.validation.checks)},
        )
        return {
            "ok": result.status == "pass",
            "change": record_to_dict(store.get_change(change.id)),
            "intent_path": str(intent_path),
            "pipeline": result.model_dump(),
            "plan": plan_metadata(load_intent(intent_path)),
            "workflow": workflow.as_dict(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/desired-state/catalog")
def desired_state_catalog() -> dict[str, object]:
    return {
        "change_types": desired_state_catalog_from_config(read_ui_config(paths())),
        "config_path": str(ui_config_path(paths())),
    }


@app.post("/api/desired-state/plan")
def desired_state_plan(request: DesiredStatePlanRequest) -> dict[str, object]:
    p = paths()
    try:
        intent_path = create_desired_state_intent(
            p,
            change_type=request.change_type,
            site=request.site,
            device_id=request.device_id,
            requested_by=request.requested_by,
            values=request.values,
        )
        intent = load_intent(intent_path)
        result = run_static_pipeline(p, intent_path)
        store = PlatformStore(p)
        change = store.get_or_create_change(intent_path, request.device_id, requested_by=request.requested_by)
        workflow = state_after_static_validation(result.status == "pass")
        metadata = plan_metadata(intent)
        result_payload = result.model_dump()
        result_payload["plan"] = metadata
        store.update_change(
            change.id,
            "validated" if result.status == "pass" else "blocked",
            result_payload,
            workflow_state=workflow.state,
        )
        store.record_workflow_event(
            change.id,
            "plan",
            change.workflow_state,
            workflow.state,
            workflow.message,
            {
                "intent_path": str(intent_path),
                "change_type": intent.change_type,
                "checks": len(result.validation.checks),
                "lab_write_supported": lab_write_supported(intent),
                "production_write_supported": production_write_supported(intent),
            },
        )
        return {
            "ok": result.status == "pass",
            "change": record_to_dict(store.get_change(change.id)),
            "intent_path": str(intent_path),
            "pipeline": result.model_dump(),
            "plan": metadata,
            "workflow": workflow.as_dict(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline")
def pipeline(request: IntentPathRequest) -> dict[str, object]:
    p = paths()
    try:
        result = run_static_pipeline(p, Path(request.intent_path))
        return {
            "ok": result.status == "pass",
            "pipeline": result.model_dump(),
            "workflow": state_after_static_validation(result.status == "pass").as_dict(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/lab/status")
def api_lab_status() -> dict[str, object]:
    return lab_status()


@app.post("/api/lab/dry-run")
def api_lab_dry_run(request: IntentPathRequest) -> dict[str, object]:
    try:
        result = JobRunner(paths()).run_lab_action(Path(request.intent_path), "dry-run", request.device_id, request.change_id)
        change = result.get("change")
        if isinstance(change, dict) and change.get("workflow_state"):
            result["workflow"] = workflow_snapshot(str(change["workflow_state"])).as_dict()  # type: ignore[arg-type]
        else:
            result["workflow"] = state_after_lab_action("dry-run", bool(result.get("ok"))).as_dict()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lab/apply")
def api_lab_apply(request: IntentPathRequest) -> dict[str, object]:
    try:
        result = JobRunner(paths()).run_lab_action(Path(request.intent_path), "apply", request.device_id, request.change_id)
        change = result.get("change")
        if isinstance(change, dict) and change.get("workflow_state"):
            result["workflow"] = workflow_snapshot(str(change["workflow_state"])).as_dict()  # type: ignore[arg-type]
        else:
            result["workflow"] = state_after_lab_action("apply", bool(result.get("ok"))).as_dict()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lab/rollback")
def api_lab_rollback(request: IntentPathRequest) -> dict[str, object]:
    try:
        result = JobRunner(paths()).run_lab_action(Path(request.intent_path), "rollback", request.device_id, request.change_id)
        change = result.get("change")
        if isinstance(change, dict) and change.get("workflow_state"):
            result["workflow"] = workflow_snapshot(str(change["workflow_state"])).as_dict()  # type: ignore[arg-type]
        else:
            result["workflow"] = state_after_lab_action("rollback", bool(result.get("ok"))).as_dict()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lab/full-run")
def api_lab_full_run(request: IntentPathRequest) -> dict[str, object]:
    try:
        return JobRunner(paths()).run_full_arista(Path(request.intent_path), request.device_id, apply=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/adapters")
def api_adapters() -> dict[str, object]:
    return AdapterRegistry().summary()


@app.get("/api/platform/capabilities")
def api_platform_capabilities() -> dict[str, object]:
    return platform_capabilities(paths())


@app.get("/api/source-of-truth")
def api_source_of_truth() -> dict[str, object]:
    return source_of_truth(paths())


@app.get("/api/source-of-truth/providers")
def api_source_of_truth_providers() -> dict[str, object]:
    return {"providers": provider_catalog()}


@app.get("/api/git/status")
def api_git_status() -> dict[str, object]:
    return git_workspace_status(paths().root)


@app.post("/api/git/setup")
def api_git_setup(request: GitSetupRequest) -> dict[str, object]:
    p = paths()
    config = read_ui_config(p)
    git_config = config.get("git", {})
    repo_url = request.repo_url or str(git_config.get("repo_url") or "")
    branch = request.branch or str(git_config.get("branch") or "main")
    return setup_git_workspace(p.root, repo_url=repo_url, branch=branch)


@app.post("/api/discovery/scan")
def api_discovery_scan(request: DiscoveryScanRequest) -> dict[str, object]:
    return DiscoveryService(paths()).scan(
        host=request.host,
        username=request.username,
        password=request.password,
        platform=request.platform,
        port=request.port,
        device_id=request.device_id,
        site=request.site,
        groups=request.groups,
    )


@app.post("/api/source-of-truth/devices/import")
def api_source_of_truth_import_device(request: SourceOfTruthDeviceImportRequest) -> dict[str, object]:
    return DiscoveryService(paths()).import_candidate(request.candidate)


@app.get("/api/templates/{platform}/{name}")
def api_template(platform: str, name: str) -> dict[str, object]:
    if "/" in platform or "/" in name or ".." in platform or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid template path")
    filename = name if name.endswith(".j2") else f"{name}.j2"
    template_path = configured_template_dir(paths()) / platform / filename
    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template not found: {platform}/{filename}")
    return {
        "ok": True,
        "platform": platform,
        "name": filename,
        "path": str(template_path),
        "body": template_path.read_text(),
    }


@app.post("/api/gitops/plan")
def api_gitops_plan(request: IntentPathRequest) -> dict[str, object]:
    try:
        return gitops_plan(paths(), Path(request.intent_path))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workflow/state/{state}")
def api_workflow_state(state: str) -> dict[str, object]:
    try:
        return workflow_snapshot(state).as_dict()  # type: ignore[arg-type]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown workflow state {state}") from exc


@app.get("/api/workflow/change/{change_id}")
def api_workflow_change(change_id: str) -> dict[str, object]:
    store = PlatformStore(paths())
    try:
        change = store.get_change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}") from exc
    return {
        "change": record_to_dict(change),
        "workflow": workflow_snapshot(change.workflow_state).as_dict(),  # type: ignore[arg-type]
        "events": [record_to_dict(event) for event in store.list_workflow_events(change_id)],
    }


@app.get("/api/adapters/device/{device_id}")
def api_device_adapters(device_id: str) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().device_capabilities(device)


@app.get("/api/adapters/rez/health")
def api_rez_health() -> dict[str, object]:
    return AdapterRegistry().rez.health()


@app.get("/api/adapters/rez/platforms")
def api_rez_platforms() -> dict[str, object]:
    return AdapterRegistry().rez.platforms()


@app.get("/api/adapters/conformance")
def api_adapter_conformance() -> dict[str, object]:
    return {"conformance": AdapterRegistry().conformance()}


@app.get("/api/adapters/rez/state/{device_id}")
def api_rez_device_state(device_id: str) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().rez.collect_device_state(device)


@app.post("/api/adapters/rez/collect-state")
def api_rez_collect_state(request: DeviceRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device_id = request.device_id
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().rez.collect_device_state(device)


@app.post("/api/verify/vlan")
def api_verify_vlan(request: VlanVerifyRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.by_id.get(request.device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {request.device_id}")
    state = AdapterRegistry().rez.collect_device_state(device)
    verification = verify_vlan_state(state, request.vlan_id, request.name, present=request.present)
    return {
        "ok": verification.get("ok"),
        "device_id": request.device_id,
        "platform": device.platform,
        "state": {
            "ok": state.get("ok"),
            "adapter": state.get("adapter"),
            "collection_time": state.get("collection_time"),
            "warnings": state.get("warnings", []),
            "errors": state.get("errors", []),
            "error": state.get("error"),
        },
        "verification": verification,
    }


@app.post("/api/verify")
def api_verify(request: GenericVerifyRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.by_id.get(request.device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {request.device_id}")
    state = AdapterRegistry().rez.collect_device_state(device)
    verification = verify_state(state, request.check, **request.params)
    return {
        "ok": verification.get("ok"),
        "device_id": request.device_id,
        "platform": device.platform,
        "check": request.check,
        "state": {
            "ok": state.get("ok"),
            "adapter": state.get("adapter"),
            "collection_time": state.get("collection_time"),
            "warnings": state.get("warnings", []),
            "errors": state.get("errors", []),
            "error": state.get("error"),
        },
        "verification": verification,
    }


@app.post("/api/verify/intent")
def api_verify_intent(request: IntentPathRequest) -> dict[str, object]:
    p = paths()
    intent = load_intent(Path(request.intent_path))
    inventory = Inventory(configured_inventory_path(p))
    device_id = request.device_id or (intent.targets.device_ids[0] if intent.targets.device_ids else "")
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    adapter = AristaEOSLabAdapter(device)
    try:
        adapter.connect()
        verification = adapter.verify_intent(intent, present=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        adapter.disconnect()
    return {
        "ok": verification.status == "pass",
        "device_id": device.id,
        "platform": device.platform,
        "change_type": intent.change_type,
        "verification": verification.__dict__,
    }


@app.post("/api/drift/vlan")
def api_vlan_drift(request: IntentPathRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device_id = request.device_id or str(read_ui_config(p).get("desired_state", {}).get("common", {}).get("device_id") or "")
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    state = AdapterRegistry().rez.collect_device_state(device)
    return vlan_drift_report(p, Path(request.intent_path), state)


@app.get("/api/compliance/summary")
def api_compliance_summary() -> dict[str, object]:
    return compliance_summary(paths())


@app.post("/api/scale/plan")
def api_scale_plan(request: ScalePlanRequest) -> dict[str, object]:
    return rollout_plan(paths(), request.device_ids, request.canary_size, request.batch_size)


@app.post("/api/assistant")
def api_assistant(request: AssistantRequest) -> dict[str, object]:
    return assistant_response(request.prompt, request.context)


@app.get("/api/changes")
def api_changes() -> dict[str, object]:
    store = PlatformStore(paths())
    return {"changes": [record_to_dict(record) for record in store.list_changes()]}


@app.get("/api/jobs")
def api_jobs() -> dict[str, object]:
    store = PlatformStore(paths())
    return {"jobs": [record_to_dict(record) for record in store.list_jobs()]}


@app.get("/api/audit/sessions")
def api_audit_sessions() -> dict[str, object]:
    store = PlatformStore(paths())
    changes = [record_to_dict(record) for record in store.list_changes(limit=100)]
    jobs = [record_to_dict(record) for record in store.list_jobs(limit=100)]
    events = []
    for change in changes:
        events.extend(record_to_dict(event) for event in store.list_workflow_events(str(change["id"])))
    sessions = []
    for job in jobs:
        result = job.get("result") or {}
        lab_result = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
        evidence = lab_result.get("evidence", {}) if isinstance(lab_result, dict) else {}
        transcript = evidence.get("transcript") or evidence.get("session", {}).get("transcript", [])
        if transcript:
            sessions.append(
                {
                    "job_id": job["id"],
                    "change_id": job["change_id"],
                    "action": job["action"],
                    "status": job["status"],
                    "message": job["message"],
                    "created_at": job["created_at"],
                    "updated_at": job["updated_at"],
                    "session_name": lab_result.get("session_name", ""),
                    "commands": transcript,
                }
            )
    return {"changes": changes, "jobs": jobs, "events": events, "sessions": sessions}


app.mount("/static", StaticFiles(directory=str(paths().static)), name="static")
