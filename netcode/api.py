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
from netcode.drift import compliance_summary, vlan_drift_report
from netcode.gitops import gitops_plan
from netcode.inventory import Inventory
from netcode.jobs import JobRunner
from netcode.lab import lab_status, run_arista_end_to_end, run_lab_action
from netcode.orchestrator import create_add_vlan_intent, run_static_pipeline
from netcode.paths import paths
from netcode.platform import platform_capabilities
from netcode.scale import rollout_plan
from netcode.source_of_truth import provider_catalog, source_of_truth
from netcode.store import PlatformStore, record_to_dict
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


class IntentPathRequest(BaseModel):
    intent_path: str
    device_id: str | None = None
    change_id: str | None = None


class DeviceRequest(BaseModel):
    device_id: str


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


@app.get("/api/templates/{platform}/{name}")
def api_template(platform: str, name: str) -> dict[str, object]:
    if "/" in platform or "/" in name or ".." in platform or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid template path")
    filename = name if name.endswith(".j2") else f"{name}.j2"
    template_path = paths().templates / platform / filename
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
    inventory = Inventory(p.inventories / "lab.yaml")
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
    inventory = Inventory(p.inventories / "lab.yaml")
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().rez.collect_device_state(device)


@app.post("/api/adapters/rez/collect-state")
def api_rez_collect_state(request: DeviceRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(p.inventories / "lab.yaml")
    device_id = request.device_id
    device = inventory.by_id.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().rez.collect_device_state(device)


@app.post("/api/verify/vlan")
def api_verify_vlan(request: VlanVerifyRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(p.inventories / "lab.yaml")
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
    inventory = Inventory(p.inventories / "lab.yaml")
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


@app.post("/api/drift/vlan")
def api_vlan_drift(request: IntentPathRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(p.inventories / "lab.yaml")
    device_id = request.device_id or "v2-store1"
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


app.mount("/static", StaticFiles(directory=str(paths().static)), name="static")
