"""FastAPI backend for the UI."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from netcode.ai_assistant import assistant_response
from netcode.adapters.registry import AdapterRegistry
from netcode.bootstrap import init_workspace
from netcode.discovery import DiscoveryService
from netcode.drift import compliance_summary, vlan_drift_report
from netcode.gitflow import (
    commit_change_artifacts,
    create_change_branch,
    git_evidence,
    git_workspace_status,
    list_git_branches,
    push_current_branch,
    setup_git_workspace,
)
from netcode.gitops import gitops_plan
from netcode.inventory import Inventory
from netcode.intent_utils import lab_write_supported, plan_metadata, production_write_supported
from netcode.jobs import JobRunner, execution_mode, runner_pool
from netcode.lab import AristaEOSLabAdapter, lab_status, run_arista_end_to_end, run_lab_action
from netcode.auth import (
    Principal,
    SYSTEM_PRINCIPAL,
    auth_enabled,
    hash_password,
    mint_session,
    resolve_principal,
    token_hash,
    verify_password,
)
from netcode.models import load_intent
from netcode.runner_hub import (
    authenticate_runner,
    enroll_runner,
    mint_join_token,
    poll_for_job,
    runner_summary,
    submit_job_result,
)
from netcode.orchestrator import create_add_vlan_intent, create_desired_state_intent, run_static_pipeline
from netcode.paths import paths
from netcode.platform import platform_capabilities
from netcode.scale import rollout_plan
from netcode.source_of_truth import netbox_sync, netbox_test, provider_catalog, source_of_truth
from netcode.store import DEFAULT_ORG_ID, PlatformStore, record_to_dict
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


class GitBranchRequest(BaseModel):
    name: str = ""
    base: str = ""


class GitCommitRequest(BaseModel):
    message: str = ""
    change_id: str = ""


class GitPushRequest(BaseModel):
    change_id: str = ""


class JoinTokenRequest(BaseModel):
    pool: str = "store-lab"


class RunnerEnrollRequest(BaseModel):
    join_token: str
    name: str = "runner"


class RunnerPollRequest(BaseModel):
    wait_seconds: float = 20.0


class RunnerResultRequest(BaseModel):
    result: dict[str, object]
    signature: str = ""


class RunnerHeartbeatRequest(BaseModel):
    version: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str
    org_id: str = ""


class NetBoxRequest(BaseModel):
    url: str = ""
    token: str = ""


app = FastAPI(title="Netcode Platform", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_workspace(paths())
    _bootstrap_admin()


def _bootstrap_admin() -> None:
    """Seed the default org + a bootstrap admin so flipping NETCODE_AUTH never locks everyone out.
    Idempotent. Admin credentials come from env; skipped if the admin already exists."""
    store = PlatformStore(paths())
    store.ensure_org(DEFAULT_ORG_ID, "Default", "default")
    email = os.environ.get("NETCODE_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("NETCODE_BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    if email and password and not store.user_exists(DEFAULT_ORG_ID, email):
        store.create_user(DEFAULT_ORG_ID, email, hash_password(password), role="admin")


# ── RBAC middleware (M5) ──────────────────────────────────────────────────
# Auth OFF (default): every request resolves to a system admin — no behavior change,
# UI and all existing tests keep working. Auth ON (NETCODE_AUTH=1): user endpoints
# require a valid session + role; runner endpoints keep their own token auth.

_PUBLIC_EXACT = {"/", "/app", "/app/", "/api/health", "/api/auth/login", "/api/auth/logout"}
_ADMIN_PATHS = {"/api/runners/join-token"}


def _request_principal(request: Request) -> Principal:
    return getattr(request.state, "principal", SYSTEM_PRINCIPAL)


@app.middleware("http")
async def _rbac(request: Request, call_next):
    path = request.url.path
    if auth_enabled():
        request.state.principal = resolve_principal(PlatformStore(paths()), request.headers.get("authorization"))
    else:
        request.state.principal = SYSTEM_PRINCIPAL
    principal = request.state.principal

    # Public UI shell, health, login, static assets, and the runner data plane
    # (which authenticates with its own runner token) bypass user RBAC.
    bypass = path in _PUBLIC_EXACT or path.startswith("/static") or path.startswith("/api/runner/")
    if auth_enabled() and not bypass:
        if not principal.authenticated:
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        required = "admin" if path in _ADMIN_PATHS else ("operator" if request.method in ("POST", "PUT", "PATCH", "DELETE") else "viewer")
        if not principal.has_role(required):
            return JSONResponse({"detail": f"This action requires the '{required}' role."}, status_code=403)
    return await call_next(request)


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


def _lab_summary(status: dict[str, object]) -> dict[str, object]:
    """Shape raw lab status into a UI-safe summary so no raw payload reaches the browser."""
    stdout = str(status.get("stdout") or "")
    running_nodes = len(re.findall(r"\brunning\b", stdout))
    nodes: list[str] = []
    for name in re.findall(r"clab-[A-Za-z0-9_.-]+", stdout):
        if name not in nodes:
            nodes.append(name)
    ok = bool(status.get("ok"))
    if ok:
        message = f"Containerlab reachable. {running_nodes} nodes running." if running_nodes else "Containerlab is reachable."
    else:
        raw_error = str(status.get("message") or status.get("stderr") or "Lab not reachable from this runtime.").strip()
        message = raw_error.splitlines()[0][:200] if raw_error else "Lab not reachable from this runtime."
    return {
        "ok": ok,
        "message": message,
        "running_nodes": running_nodes,
        "nodes": nodes[:24],
    }


@app.get("/api/health")
def health() -> dict[str, object]:
    p = paths()
    return {
        "ok": True,
        "workspace": str(p.root),
        "lab": _lab_summary(lab_status()),
        "execution": {"mode": execution_mode(), "pool": runner_pool()},
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
def wizard_add_vlan(request: AddVlanRequest, http_request: Request) -> dict[str, object]:
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
        principal = _request_principal(http_request)
        change = store.get_or_create_change(intent_path, request.device_id, requested_by=request.requested_by, org_id=principal.org_id, created_by_user_id=principal.user_id)
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
def desired_state_plan(request: DesiredStatePlanRequest, http_request: Request) -> dict[str, object]:
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
        principal = _request_principal(http_request)
        change = store.get_or_create_change(intent_path, request.device_id, requested_by=request.requested_by, org_id=principal.org_id, created_by_user_id=principal.user_id)
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


@app.post("/api/source-of-truth/netbox/test")
def api_netbox_test(request: NetBoxRequest) -> dict[str, object]:
    return netbox_test(paths(), request.url, request.token)


@app.post("/api/source-of-truth/netbox/sync")
def api_netbox_sync(request: NetBoxRequest) -> dict[str, object]:
    return netbox_sync(paths(), request.url, request.token)


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


@app.get("/api/git/branches")
def api_git_branches() -> dict[str, object]:
    return list_git_branches(paths().root)


@app.post("/api/git/branch")
def api_git_branch(request: GitBranchRequest) -> dict[str, object]:
    return create_change_branch(paths().root, name=request.name, base=request.base)


def _record_git_event(p, change_id: str, action: str, result: dict[str, object]) -> dict[str, object]:
    """Attach a git action outcome to a change as a workflow event; honest about lookup failures."""
    if not change_id:
        return {"change_event_recorded": False, "reason": "no change_id supplied"}
    store = PlatformStore(p)
    try:
        change = store.get_change(change_id)
    except Exception as exc:
        return {"change_event_recorded": False, "reason": f"unknown change {change_id}: {exc}"}
    store.record_workflow_event(
        change.id,
        action,
        change.workflow_state,
        change.workflow_state,
        str(result.get("message", "")),
        {
            "ok": bool(result.get("ok")),
            "action": result.get("action"),
            "branch": result.get("branch"),
            "commit": result.get("commit"),
        },
    )
    return {"change_event_recorded": True}


@app.post("/api/git/commit")
def api_git_commit(request: GitCommitRequest) -> dict[str, object]:
    p = paths()
    config = read_ui_config(p)
    default_message = str((config.get("git") or {}).get("default_commit_message") or "Netcode network change")
    result = commit_change_artifacts(p.root, message=request.message or default_message)
    result.update(_record_git_event(p, request.change_id, "git_commit", result))
    return result


@app.post("/api/git/push")
def api_git_push(request: GitPushRequest) -> dict[str, object]:
    p = paths()
    result = push_current_branch(p.root)
    result.update(_record_git_event(p, request.change_id, "git_push", result))
    return result


def _admin_guard(authorization: str | None) -> None:
    """Admin endpoints are open when NETCODE_ADMIN_TOKEN is unset (local dev), token-gated otherwise."""
    required = os.environ.get("NETCODE_ADMIN_TOKEN", "").strip()
    if not required:
        return
    if authorization != f"Bearer {required}":
        raise HTTPException(status_code=401, detail="Admin token required.")


def _require_runner(store: PlatformStore, authorization: str | None):
    token = (authorization or "").removeprefix("Bearer ").strip()
    runner = authenticate_runner(store, token)
    if runner is None:
        raise HTTPException(status_code=401, detail="Runner token is invalid or revoked.")
    return runner


@app.post("/api/auth/login")
def api_login(request: LoginRequest) -> dict[str, object]:
    store = PlatformStore(paths())
    org_id = request.org_id or DEFAULT_ORG_ID
    user = store.get_user_by_email(org_id, request.email)
    if not user or not verify_password(request.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = mint_session(store, str(user["id"]), org_id)
    return {"ok": True, "token": token, "user": {"email": user["email"], "role": user["role"], "org_id": org_id}}


@app.post("/api/auth/logout")
def api_logout(authorization: str | None = Header(default=None)) -> dict[str, object]:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token:
        PlatformStore(paths()).revoke_session(token_hash(token))
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request) -> dict[str, object]:
    principal = _request_principal(request)
    if auth_enabled() and not principal.authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return {
        "ok": True,
        "auth_enabled": auth_enabled(),
        "kind": principal.kind,
        "role": principal.role,
        "org_id": principal.org_id,
        "email": principal.email,
    }


@app.post("/api/runners/join-token")
def api_mint_join_token(request: JoinTokenRequest, http_request: Request, authorization: str | None = Header(default=None)) -> dict[str, object]:
    # Admin role (via middleware when auth on) OR the legacy break-glass admin token.
    if not auth_enabled():
        _admin_guard(authorization)
    principal = _request_principal(http_request)
    return mint_join_token(PlatformStore(paths()), request.pool, org_id=principal.org_id)


@app.get("/api/runners")
def api_list_runners(request: Request) -> dict[str, object]:
    return runner_summary(PlatformStore(paths()), org_id=_request_principal(request).org_id)


@app.post("/api/runner/enroll")
def api_runner_enroll(request: RunnerEnrollRequest) -> dict[str, object]:
    return enroll_runner(PlatformStore(paths()), request.join_token, request.name)


@app.post("/api/runner/poll")
def api_runner_poll(request: RunnerPollRequest, authorization: str | None = Header(default=None)):
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    job = poll_for_job(store, runner, request.wait_seconds)
    if job is None:
        return Response(status_code=204)
    return {"ok": True, "job": record_to_dict(job)}


@app.post("/api/runner/jobs/{job_id}/result")
def api_runner_job_result(job_id: str, request: RunnerResultRequest, authorization: str | None = Header(default=None)) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    return submit_job_result(store, runner, job_id, dict(request.result), request.signature)


@app.post("/api/runner/heartbeat")
def api_runner_heartbeat(request: RunnerHeartbeatRequest, authorization: str | None = Header(default=None)) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    store.touch_runner(runner.id, status="online", version=request.version)
    return {"ok": True, "runner_id": runner.id, "pool": runner.pool}


def _runner_read(p, action: str, payload: dict, org_id: str, timeout: float = 60.0) -> dict[str, object]:
    """Runner mode: queue a device-read job and wait for the on-prem runner to report.
    Keeps the browser API synchronous while the actual device I/O happens on the runner."""
    store = PlatformStore(p)
    job = store.create_read_job(org_id, runner_pool(), action, payload)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = store.get_job(job.id)
        if current.status in ("completed", "failed"):
            return current.result or {"ok": current.status == "completed", "message": current.message}
        time.sleep(0.4)
    return {"ok": False, "error": f"No runner completed the {action} read within {int(timeout)}s. Is a runner online for this pool? (Setup → Runners)"}


@app.post("/api/readiness/devices")
def api_readiness_devices(request: Request) -> dict[str, object]:
    """Live read test: can the platform actually read the trusted devices right now?"""
    p = paths()
    if execution_mode() == "runner":
        return _runner_read(p, "readiness", {}, _request_principal(request).org_id)
    inventory = Inventory(configured_inventory_path(p))
    devices = list(inventory.by_id.values())
    if not devices:
        return {
            "ok": False,
            "tested": 0,
            "readable": 0,
            "devices": [],
            "message": "No devices in source of truth yet. Discover a device first.",
        }
    collected = AdapterRegistry().rez.collect_many(devices)
    results = {str(item.get("device_id")): item for item in collected.get("results", []) if isinstance(item, dict)}
    rows: list[dict[str, object]] = []
    readable = 0
    for device in devices:
        result = results.get(device.id) or {}
        ok = bool(result.get("ok"))
        readable += 1 if ok else 0
        error = ""
        if not ok:
            errors = result.get("errors") or []
            error = str(result.get("error") or (errors[0] if errors else "unreadable"))
        rows.append({"id": device.id, "host": device.host, "platform": device.platform, "ok": ok, "error": error})
    return {
        "ok": readable > 0,
        "tested": len(devices),
        "readable": readable,
        "devices": rows,
        "message": f"{readable}/{len(devices)} trusted devices are readable.",
    }


@app.post("/api/discovery/scan")
def api_discovery_scan(request: DiscoveryScanRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    if execution_mode() == "runner":
        payload = {"host": request.host, "username": request.username, "password": request.password,
                   "platform": request.platform, "port": request.port, "device_id": request.device_id,
                   "site": request.site, "groups": request.groups}
        return _runner_read(p, "discovery", payload, _request_principal(http_request).org_id)
    return DiscoveryService(p).scan(
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
def api_verify_intent(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    if execution_mode() == "runner":
        intent_yaml = Path(request.intent_path).read_text(encoding="utf-8")
        payload = {"intent_yaml": intent_yaml, "device_id": request.device_id, "present": True}
        return _runner_read(p, "verify", payload, _request_principal(http_request).org_id)
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
def api_vlan_drift(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    if execution_mode() == "runner":
        intent_yaml = Path(request.intent_path).read_text(encoding="utf-8")
        device_id = request.device_id or str(read_ui_config(p).get("desired_state", {}).get("common", {}).get("device_id") or "")
        return _runner_read(p, "drift", {"intent_yaml": intent_yaml, "device_id": device_id}, _request_principal(http_request).org_id)
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
def api_changes(request: Request) -> dict[str, object]:
    store = PlatformStore(paths())
    org = _request_principal(request).org_id
    return {"changes": [record_to_dict(record) for record in store.list_changes(org_id=org)]}


@app.get("/api/jobs")
def api_jobs(request: Request) -> dict[str, object]:
    store = PlatformStore(paths())
    org = _request_principal(request).org_id
    return {"jobs": [record_to_dict(record) for record in store.list_jobs(org_id=org)]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, request: Request) -> dict[str, object]:
    """Single job status for UI polling of runner-executed (queued) lab actions."""
    store = PlatformStore(paths())
    org = _request_principal(request).org_id
    try:
        job = store.get_job(job_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}") from exc
    if job.org_id != org:  # 404 (not 403) so job existence never leaks across tenants
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return record_to_dict(job)


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


def _job_lab_result(job: dict[str, object]) -> dict[str, object]:
    result = job.get("result") or {}
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result if isinstance(result, dict) else {}


def _job_transcript(job: dict[str, object]) -> list[dict[str, object]]:
    lab_result = _job_lab_result(job)
    evidence = lab_result.get("evidence", {}) if isinstance(lab_result, dict) else {}
    if not isinstance(evidence, dict):
        return []
    transcript = evidence.get("transcript") or (evidence.get("session") or {}).get("transcript", [])
    return transcript if isinstance(transcript, list) else []


@app.get("/api/change/{change_id}/record")
def api_change_record(change_id: str, request: Request) -> dict[str, object]:
    """One readable change package: request, plan, safety, lab/apply/verify proof, git, rollback, manifest."""
    p = paths()
    store = PlatformStore(p)
    try:
        change = store.get_change(change_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}") from exc
    if change.org_id != _request_principal(request).org_id:  # 404 to avoid cross-tenant existence leak
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}")
    change_dict = record_to_dict(change)
    result = change_dict.get("result") or {}
    plan = result.get("plan") or {}
    validation = result.get("validation") or {}
    render = result.get("render") or {}
    intent_info = result.get("intent") or {}
    jobs = [record_to_dict(j) for j in store.list_jobs(limit=200) if j.change_id == change_id]
    events = [record_to_dict(e) for e in store.list_workflow_events(change_id)]

    def proof_for(fragment: str) -> dict[str, object]:
        for job in jobs:  # list_jobs returns newest first
            if fragment in str(job.get("action", "")):
                lab_result = _job_lab_result(job)
                return {
                    "present": True,
                    "job_id": job["id"],
                    "status": job["status"],
                    "message": job["message"],
                    "at": job["updated_at"],
                    "session_name": lab_result.get("session_name", "") if isinstance(lab_result, dict) else "",
                    "commands": _job_transcript(job),
                }
        return {"present": False}

    def verify_proof() -> dict[str, object]:
        for job in jobs:
            if "apply" in str(job.get("action", "")):
                lab_result = _job_lab_result(job)
                evidence = lab_result.get("evidence", {}) if isinstance(lab_result, dict) else {}
                details = {k: v for k, v in evidence.items() if k != "transcript"} if isinstance(evidence, dict) else {}
                if details:
                    return {"present": True, "job_id": job["id"], "status": job["status"], "details": details}
        return {"present": False}

    intent_path = Path(str(change_dict.get("intent_path") or ""))
    slug = str(plan.get("slug") or intent_path.stem)
    manifest: list[dict[str, object]] = []

    def manifest_entry(artifact: str, path: Path) -> None:
        manifest.append({"artifact": artifact, "path": str(path), "exists": path.exists()})

    manifest_entry("intent.yaml", intent_path)
    manifest_entry("rendered_config.eos", p.rendered / f"{slug}.eos")
    manifest_entry("report.md", p.reports / f"{slug}.md")
    manifest_entry("validation_report.json", p.reports / f"{slug}.json")

    try:
        evidence = git_evidence(p.root, intent_path)
    except Exception as exc:
        evidence = {"available": False, "message": f"Git evidence unavailable: {exc}"}
    git_status = git_workspace_status(p.root)
    git_actions = [e for e in events if str(e.get("action", "")) in ("git_commit", "git_push")]

    return {
        "ok": True,
        "change_id": change_id,
        "workflow_state": change_dict.get("workflow_state"),
        "status": change_dict.get("status"),
        "request": {
            "title": plan.get("title") or slug,
            "change_type": plan.get("change_type") or intent_info.get("change_type"),
            "site": intent_info.get("site"),
            "device_id": change_dict.get("device_id"),
            "requested_by": change_dict.get("requested_by"),
            "created_at": change_dict.get("created_at"),
            "intent_path": str(intent_path),
            "intent_yaml": result.get("intent_yaml") or "",
        },
        "plan": {
            "commands": render.get("config") or "",
            "risk": plan.get("risk"),
            "blast_radius": plan.get("blast_radius") or {},
            "rollback": plan.get("rollback") or {},
            "checks": plan.get("checks") or {},
            "suggested_branch": plan.get("suggested_branch"),
            "lab_write_supported": plan.get("lab_write_supported"),
            "production_write_supported": plan.get("production_write_supported"),
        },
        "safety": {
            "status": validation.get("status"),
            "checks": [
                {
                    "id": check.get("id"),
                    "title": check.get("title"),
                    "status": check.get("status"),
                    "message": check.get("message"),
                }
                for check in (validation.get("checks") or [])
            ],
        },
        "lab_proof": proof_for("dry"),
        "apply_proof": proof_for("apply"),
        "verify_proof": verify_proof(),
        "rollback_record": proof_for("rollback"),
        "git": {
            "branch": git_status.get("branch"),
            "upstream": git_status.get("upstream"),
            "ahead": git_status.get("ahead"),
            "actions": git_actions,
            "evidence": evidence,
        },
        "manifest": manifest,
        "events": events,
    }


app.mount("/static", StaticFiles(directory=str(paths().static)), name="static")
