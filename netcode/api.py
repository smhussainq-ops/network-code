"""FastAPI backend for the UI."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from netcode.ai_assistant import assistant_response
from netcode.ansible_backend import (
    build_ansible_pack_plan,
    build_guided_ansible_playbook,
    package_ansible_playbooks,
)
from netcode.adapters.registry import AdapterRegistry
from netcode.adapters.rez import READ_TRANSPORTS
from netcode.bootstrap import init_workspace
from netcode.discovery import DiscoveryService
from netcode.diagnostics_handoff import attach_verification_handoff, build_verification_handoff
from netcode.entitlements import (
    EntitlementError,
    canonical_org_id,
    enforce_capacity,
    enforcement_enabled,
    get_entitlements,
    job_requires_production_writes,
    require_production_writes,
)
from netcode.cross_domain import (
    CheckEvidence,
    CrossDomainPlan,
    build_cross_domain_plan,
    evaluate_service_assurance,
)
from netcode.firewall_managers import (
    ApplicationFlow,
    ApprovalProof,
    FirewallNatChange,
    FirewallPolicyChange,
    ManagerCapabilities,
    ManagerJobRequest,
    WRITE_ACTIONS,
)
from netcode.drift import (
    aggregate_device_vlans,
    baseline_for_state,
    compliance_summary,
    device_drift_from_state,
    vlan_drift_report,
)
from netcode.gitflow import (
    commit_change_artifacts,
    create_change_branch,
    git_evidence,
    git_workspace_status,
    list_git_branches,
    materialize_change_artifacts,
    push_current_branch,
    setup_git_workspace,
)
from netcode.fleet import (
    annotate_rollout_audit,
    approve_rollout,
    cancel_rollout,
    create_remediation_rollouts,
    device_audit_ref,
    drift_watch_status,
    fleet_drift_snapshot,
    plan_fleet_rollout,
    reconcile_rollouts_on_startup,
    request_halt,
    retry_rollout,
    rollout_failure_handoff,
    rollout_status,
    set_drift_watch,
    start_fleet_drift,
    start_rollout,
)
from netcode.gitops import gitops_plan
from netcode.inventory import Inventory
from netcode.intent_utils import lab_write_supported, plan_metadata, production_write_supported, rollback_config
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
from netcode.models import load_intent, load_intent_data
from netcode.change_types import spec_for
from netcode.runner_hub import (
    authenticate_runner,
    confirm_runner_token_rotation,
    enroll_runner,
    mint_join_token,
    poll_for_job,
    prepare_runner_token_rotation,
    runner_summary,
    submit_job_progress,
    submit_job_result,
)
from netcode.orchestrator import create_add_vlan_intent, create_desired_state_intent, run_static_pipeline
from netcode.paths import paths
from netcode.platform import platform_capabilities
from netcode.production_readiness import collect_netcode_production_issues, is_production_environment
from netcode.scale import rollout_plan
from netcode.shell_desktop import build_desktop_shell_profile
from netcode.source_of_truth import netbox_sync, netbox_test, provider_catalog, source_of_truth
from netcode.network_model import NETWORK_OBSERVATION_SCHEMA, NetworkModelError
from netcode.network_model_store import NetworkModelRepository
from netcode.network_model_import import (
    import_approved_network_design,
    import_catalog_candidate,
    import_local_yaml_candidate,
)
from netcode.network_model_compiler import (
    compile_effective_device,
    compile_site_context,
    to_rez_network_design,
)
from netcode.network_model_reconcile import reconcile_revision
from netcode.network_model_lifecycle import (
    activate_change_candidates,
    activate_verified_revision,
    approve_change_candidates,
    approve_with_git,
    create_candidate_for_change_intent,
    create_candidate_for_change_set,
    create_candidate_from_change,
    model_diff,
    rollback_active_revision,
)
from netcode.store import (
    DEFAULT_ORG_ID,
    JobQueueFullError,
    TERMINAL_JOB_STATUSES,
    PlatformStore,
    change_audit_id,
    change_summary_to_dict,
    job_lease_seconds,
    record_to_dict,
    utc_now,
)
from netcode.trusted_hosts import PrivateReadinessTrustedHostMiddleware
from netcode.troubleshooting import troubleshoot_state
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
from netcode.windows_runner_package import build_windows_runner_package, package_manifest
from netcode.workflow import state_after_lab_action, state_after_static_validation, workflow_snapshot
from netcode.workflow_packs import entitled_change_types, workflow_pack_catalog
from netcode.yamlio import write_yaml


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
    environment_id: str = ""
    model_revision_id: str = ""


class AnsiblePackPlanRequest(BaseModel):
    playbook_path: str
    rollback_playbook_path: str = ""
    targets: list[str] = []
    mode: str = "check"
    requested_by: str = "operator"
    change_id: str = ""


class GuidedAnsibleRequest(BaseModel):
    name: str = "Guided network workflow"
    platform: str = "arista_eos"
    operation: str = "show"
    commands: list[str] = []
    rollback_commands: list[str] = []
    targets: list[str] = []
    requested_by: str = "operator"


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


class CrossDomainPlanRequest(BaseModel):
    title: str
    requested_by: str = "netcode-user"
    ticket_id: str
    flow: ApplicationFlow
    routing_owner: str
    sdwan_owner: str | None = None
    firewall_policy: FirewallPolicyChange
    firewall_nat: FirewallNatChange | None = None


class ManagerActionQueueRequest(BaseModel):
    capabilities: ManagerCapabilities
    operation_id: str = ""
    manager_task_id: str | None = None
    pre_change_revision: str | None = None


class CrossDomainVerifyRequest(BaseModel):
    manager_job_id: str
    evidence_job_ids: list[str]


class TroubleshootRequest(BaseModel):
    device_id: str
    check: str = "live_state"
    target: str = ""
    expected: str = ""
    change_id: str | None = None


class VerificationHandoffRequest(BaseModel):
    device_id: str
    check: str
    expected: str = ""
    actual: str = ""
    verification: dict[str, object] = {}
    change_id: str = ""
    intent_path: str = ""


class ShellOpenRequest(BaseModel):
    device_id: str
    guard_enabled: bool = False


class ShellManualDeviceRequest(BaseModel):
    device_id: str
    host: str
    platform: str = "arista_eos"
    hostname: str = ""
    username: str = ""
    password: str = ""
    port: int = 22
    site: str = "manual"
    groups: list[str] = []


class ShellInputRequest(BaseModel):
    session_id: str
    input: str


class ShellAttachRequest(BaseModel):
    session_id: str
    change_id: str


class ShellQuickChangeRequest(BaseModel):
    session_id: str
    title: str = ""
    ticket: str = ""


class FleetRolloutRequest(BaseModel):
    change_type: str = "add_vlan"
    values: dict = {}
    device_ids: list[str] | None = None
    device_group: str | None = None
    canary_size: int = 1
    batch_size: int = 3
    description: str = ""
    environment_id: str = ""
    model_revision_id: str = ""


class FleetHaltRequest(BaseModel):
    reason: str = ""


class FleetRetryRequest(BaseModel):
    scope: str = "failed_and_untouched"


class ApproveRequest(BaseModel):
    approved_by: str = ""  # used when auth is off; with auth on the principal is the approver


class DriftWatchRequest(BaseModel):
    minutes: int = 0


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
    repo_url: str | None = None
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
    lease_token: str


class RunnerProgressRequest(BaseModel):
    event: dict[str, object]
    signature: str = ""
    lease_token: str


class RunnerLeaseRequest(BaseModel):
    lease_token: str


class RunnerHeartbeatRequest(BaseModel):
    version: str = ""
    state: str = "online"


class JobCancelRequest(BaseModel):
    reason: str = "operator request"


class RunnerInventorySyncRequest(BaseModel):
    revision: str
    devices: list[dict[str, object]] = []
    replace: bool = True


class RunnerReadRequest(BaseModel):
    action: str
    payload: dict = {}
    timeout: float = 60.0


class RcaRemediationProposalRequest(BaseModel):
    source: str = "rez"
    proposal_schema: str = ""
    proposal_source: str = ""
    root_confirmed: bool = False
    root_atom_id: str = ""
    incident_id: str
    target_device: str = ""
    suggested_pack: str = "custom_config"
    proposed_intent: dict[str, object] = {}
    rationale: str = ""
    confidence: float = 0.0
    evidence_refs: list[str] = []
    requested_by: str = "rez-rca"
    title: str = ""
    environment_id: str = ""
    model_revision_id: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str
    org_id: str = ""


class NetBoxRequest(BaseModel):
    url: str = ""
    token: str = ""


TROUBLESHOOT_READ_TIMEOUT_SECONDS = 20


_PRODUCTION_RUNTIME = is_production_environment(os.environ)
app = FastAPI(
    title="Netcode Platform",
    version="0.1.0",
    docs_url=None if _PRODUCTION_RUNTIME else "/docs",
    redoc_url=None if _PRODUCTION_RUNTIME else "/redoc",
    openapi_url=None if _PRODUCTION_RUNTIME else "/openapi.json",
)

_ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("NETCODE_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]
if _ALLOWED_HOSTS:
    app.add_middleware(PrivateReadinessTrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)


@app.exception_handler(EntitlementError)
async def _entitlement_error(_request: Request, exc: EntitlementError) -> JSONResponse:
    message = str(exc)
    unavailable = "unavailable" in message.lower() or "not configured" in message.lower()
    return JSONResponse(
        status_code=503 if unavailable else 403,
        content={"error": "entitlement_unavailable" if unavailable else "plan_limit_reached", "message": message},
    )


@app.exception_handler(JobQueueFullError)
async def _job_queue_full(_request: Request, exc: JobQueueFullError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": "job_queue_full", "message": str(exc)},
        headers={"Retry-After": "30"},
    )


@app.on_event("startup")
def _startup() -> None:
    workspace = paths()
    workspace.ensure()
    issues = _netcode_production_readiness_issues()
    if issues:
        raise RuntimeError("Netcode production configuration is not ready: " + "; ".join(issues))
    init_workspace(workspace, include_examples=not is_production_environment(os.environ))
    _bootstrap_admin()
    store = PlatformStore(workspace)
    store.recover_expired_jobs()
    for session in store.terminate_active_shell_sessions(reason="control_plane_restarted"):
        _shell_append(
            workspace,
            str(session["id"]),
            {"event": "session_terminated", "reason": "control_plane_restarted"},
        )
    # Rollout orchestrator threads die with the process: fail any orphaned
    # running rollouts closed (halted + queued jobs cancelled) at boot.
    try:
        reconcile_rollouts_on_startup(paths())
    except Exception:  # noqa: BLE001 — reconciliation must never block startup
        pass
    # Continuous drift watch (env-driven; 0/unset = off). UI can change it later.
    try:
        minutes = int(os.environ.get("NETCODE_DRIFT_WATCH_MINUTES", "0") or "0")
        if minutes > 0:
            set_drift_watch(paths(), DEFAULT_ORG_ID, minutes, load_intent)
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("NETCODE_ENV", "").strip().lower() == "production" and not enforcement_enabled():
        raise RuntimeError("NETCODE_LICENSE_ENFORCEMENT must be enabled in production.")


def _bootstrap_admin() -> None:
    """Seed the default org + a bootstrap admin so flipping NETCODE_AUTH never locks everyone out.
    Idempotent. Admin credentials come from env; skipped if the admin already exists."""
    store = PlatformStore(paths())
    store.ensure_org(DEFAULT_ORG_ID, "Default", "default")
    email = os.environ.get("NETCODE_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("NETCODE_BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    if email and password and not store.user_exists(DEFAULT_ORG_ID, email):
        store.create_user(DEFAULT_ORG_ID, email, hash_password(password), role="admin")


def _netcode_production_readiness_issues() -> list[str]:
    store = PlatformStore(paths())
    return collect_netcode_production_issues(
        os.environ,
        persisted_auth_users=store.active_user_count() > 0,
    )


def _enforce_catalog_growth(
    store: PlatformStore,
    org_id: str,
    device_ids: list[str],
    *,
    replace_runner_id: str | None = None,
) -> None:
    normalized = {store.normalize_device_identifier(value) for value in device_ids if str(value).strip()}
    if replace_runner_id:
        current = store.catalog_device_count(org_id) - store.catalog_device_count(org_id, runner_id=replace_runner_id)
        additional = len(normalized)
    else:
        current = store.catalog_device_count(org_id)
        additional = sum(1 for device_id in normalized if store.resolve_device(org_id, device_id) is None)
    enforce_capacity("devices", current=current, additional=additional, org_id=org_id)


# ── RBAC middleware (M5) ──────────────────────────────────────────────────
# Auth OFF (default): every request resolves to a system admin — no behavior change,
# UI and all existing tests keep working. Auth ON (NETCODE_AUTH=1): user endpoints
# require a valid session + role; runner endpoints keep their own token auth.

_PUBLIC_EXACT = {"/", "/app", "/app/", "/api/health", "/api/ready", "/api/auth/login"}
_ADMIN_PATHS = {"/api/runners/join-token"}
_VIEWER_MUTATION_PATHS = {"/api/auth/logout"}
_RESERVED_DOC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


def _is_rez_bridge_request(path: str, authorization: str | None) -> bool:
    token = os.environ.get("NETCODE_REZ_BRIDGE_TOKEN", "").strip()
    read_only_paths = {
        "/api/rez/runner-read",
        "/api/network-model/active",
        "/api/network-model/active/rez-design",
    }
    return bool(token) and path in read_only_paths and authorization == f"Bearer {token}"


def _request_principal(request: Request) -> Principal:
    return getattr(request.state, "principal", SYSTEM_PRINCIPAL)


def _trusted_rez_service_principal(headers, authorization: str | None) -> Principal:
    """Resolve the Rez proxy identity only inside the private admin-token envelope."""
    expected = os.environ.get("NETCODE_ADMIN_TOKEN", "").strip()
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise PermissionError("Trusted Rez service token is invalid.")
    org_id = canonical_org_id(headers.get("x-rezonance-org-id"))
    role = str(headers.get("x-rezonance-role") or "viewer").strip().lower()
    if role == "user":
        role = "operator"
    if role not in {"viewer", "operator", "admin"}:
        role = "viewer"
    actor = str(headers.get("x-rezonance-user") or "rez-user").strip()[:160] or "rez-user"
    return Principal(kind="system", org_id=org_id, role=role, email=actor)


def _websocket_principal(ws: WebSocket) -> Principal:
    if ws.headers.get("x-rezonance-org-id"):
        try:
            return _trusted_rez_service_principal(ws.headers, ws.headers.get("authorization"))
        except (EntitlementError, PermissionError):
            return Principal(kind="anon", org_id=DEFAULT_ORG_ID, role=None)
    if not auth_enabled():
        return SYSTEM_PRINCIPAL
    authorization = ws.headers.get("authorization")
    cookie_token = (ws.cookies.get("netcode_session") or "").strip()
    if not authorization and cookie_token:
        authorization = f"Bearer {cookie_token}"
    return resolve_principal(PlatformStore(paths()), authorization)


_RCA_ALLOWED_CHANGE_TYPES = {
    "add_vlan",
    "interface_config",
    "bgp_neighbor",
    "acl_rule",
    "site_device_intent",
    "custom_config",
    "ntp_standardize",
    "routing_redistribution",
}

_RCA_PROPOSAL_SCHEMA = "netcode.remediation.v1"
_RCA_PROPOSAL_SOURCES = {"rez_structured_rca", "site_operational_context"}
_RCA_NON_ACTIONABLE_ROOTS = {"AGENT_VALIDATED_FINDING"}
_RCA_NON_ACTIONABLE_PREFIXES = ("CI_", "DATA_GAP", "XL_")

_RCA_TOP_LEVEL_SECTIONS = {
    "add_vlan": "vlan",
    "interface_config": "interface",
    "bgp_neighbor": "bgp",
    "acl_rule": "acl",
    "site_device_intent": "device",
    "ntp_standardize": "ntp",
    "routing_redistribution": "redistribution",
}

_RCA_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "passphrase",
)


def _safe_slug(value: str, default: str = "rca-remediation") -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-._").lower()
    return slug[:80] or default


def _require_confirmed_rca_provenance(request: RcaRemediationProposalRequest) -> None:
    atom_id = request.root_atom_id.strip()
    if request.proposal_schema.strip() != _RCA_PROPOSAL_SCHEMA:
        raise HTTPException(status_code=400, detail="A structured Netcode remediation proposal is required.")
    if request.proposal_source.strip() not in _RCA_PROPOSAL_SOURCES:
        raise HTTPException(status_code=400, detail="The RCA proposal source is not trusted for remediation.")
    if not request.root_confirmed or not atom_id:
        raise HTTPException(status_code=400, detail="A confirmed primary root cause is required before creating a draft.")
    if atom_id in _RCA_NON_ACTIONABLE_ROOTS or atom_id.startswith(_RCA_NON_ACTIONABLE_PREFIXES):
        raise HTTPException(status_code=400, detail="The confirmed root is not an actionable device condition.")


def _proposal_targets(request: RcaRemediationProposalRequest) -> dict[str, object]:
    raw_targets = request.proposed_intent.get("targets")
    if isinstance(raw_targets, dict):
        device_ids = raw_targets.get("device_ids")
        device_group = raw_targets.get("device_group")
        if isinstance(device_ids, list) and any(str(item).strip() for item in device_ids):
            return {"device_ids": [str(item).strip() for item in device_ids if str(item).strip()]}
        if str(device_group or "").strip():
            return {"device_group": str(device_group).strip()}
    target_device = request.target_device.strip()
    if target_device:
        return {"device_ids": [target_device]}
    raise HTTPException(
        status_code=400,
        detail="RCA remediation proposals must include target_device or proposed_intent.targets.",
    )


def _proposal_lines(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if str(item).strip()).strip()
    return str(value or "").strip()


def _strip_sensitive_proposal_fields(value: object) -> object:
    """Drop credential-shaped keys before any Rez proposal becomes CP intent YAML."""
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _RCA_SENSITIVE_KEY_PARTS):
                continue
            cleaned[str(key)] = _strip_sensitive_proposal_fields(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_sensitive_proposal_fields(item) for item in value]
    return value


def _safe_proposal_dict(value: object) -> dict[str, object]:
    cleaned = _strip_sensitive_proposal_fields(value)
    return cleaned if isinstance(cleaned, dict) else {}


def _typed_proposal_section(change_type: str, proposed: dict[str, object]) -> dict[str, object]:
    section = _RCA_TOP_LEVEL_SECTIONS.get(change_type)
    if section and isinstance(proposed.get(section), dict):
        typed = {section: _safe_proposal_dict(proposed.get(section))}
        if change_type == "routing_redistribution":
            if isinstance(proposed.get("reverse_redistribution"), dict):
                typed["reverse_redistribution"] = _safe_proposal_dict(proposed.get("reverse_redistribution"))
            if isinstance(proposed.get("reachability_checks"), list):
                typed["reachability_checks"] = _strip_sensitive_proposal_fields(proposed.get("reachability_checks"))
        return typed
    # Some callers may send the same field values used by desired-state plans.
    # Use the registry builder to produce a typed section without copying extra keys.
    values = proposed.get("values") if isinstance(proposed.get("values"), dict) else None
    if isinstance(values, dict):
        from netcode.change_types import spec_for

        built: dict[str, object] = {}
        spec_for(change_type).build(built, _safe_proposal_dict(values), "")
        return {key: value for key, value in built.items() if key in _RCA_TOP_LEVEL_SECTIONS.values()}
    return {}


def _intent_from_rca_proposal(request: RcaRemediationProposalRequest) -> dict[str, object]:
    proposed = _safe_proposal_dict(request.proposed_intent or {})
    if request.root_atom_id.strip().upper() == "L1_INTERFACE_ADMIN_DOWN":
        interface_section = proposed.get("interface") if isinstance(proposed.get("interface"), dict) else {}
        values_section = proposed.get("values") if isinstance(proposed.get("values"), dict) else {}
        interface_name = str(
            interface_section.get("name") or values_section.get("interface") or ""
        ).strip()
        if not interface_name:
            raise HTTPException(
                status_code=400,
                detail="Confirmed interface admin-state RCA is missing an exact interface identity.",
            )
        proposed = {
            key: value for key, value in proposed.items() if key not in {"interface", "values"}
        }
        proposed["values"] = {
            "interface": interface_name,
            "enabled": True,
            "apply_scope": "admin_state",
        }
        proposed["interface"] = {
            "name": interface_name,
            "enabled": True,
            "apply_scope": "admin_state",
        }
    requested_type = str(proposed.get("change_type") or request.suggested_pack or "custom_config").strip()
    change_type = requested_type if requested_type in _RCA_ALLOWED_CHANGE_TYPES else "custom_config"
    targets = _proposal_targets(request)
    site = str(proposed.get("site") or proposed.get("scope") or "rca-remediation").strip() or "rca-remediation"
    policy = _safe_proposal_dict(proposed.get("policy")) if isinstance(proposed.get("policy"), dict) else {}
    metadata = {
        "requested_by": request.requested_by.strip() or "rez-rca",
        "ticket_id": request.incident_id.strip(),
        "learning_mode": True,
        "source": "rez_rca",
        "draft_only": True,
        "human_approval_required": True,
        "rationale": request.rationale,
        "evidence_refs": request.evidence_refs,
        "confidence": request.confidence,
        "proposal_schema": request.proposal_schema.strip(),
        "proposal_source": request.proposal_source.strip(),
        "confirmed_root_atom_id": request.root_atom_id.strip(),
    }

    if change_type == "custom_config":
        config_lines = (
            _proposal_lines(proposed.get("config_lines"))
            or _proposal_lines(proposed.get("commands"))
            or _proposal_lines(proposed.get("config"))
            or "! Rez RCA draft requires engineer command review before apply"
        )
        rollback_lines = _proposal_lines(proposed.get("rollback_lines") or proposed.get("rollback"))
        return {
            "change_type": "custom_config",
            "site": site,
            "targets": targets,
            "custom": {
                "config_lines": config_lines,
                "rollback_lines": rollback_lines,
                "verify_contains": str(proposed.get("verify_contains") or "").strip(),
                "description": request.rationale.strip() or request.title.strip() or "Draft created from Rez RCA.",
                "acknowledge_no_rollback": not bool(rollback_lines.strip()),
            },
            "policy": policy,
            "metadata": metadata,
        }

    intent: dict[str, object] = {
        "change_type": change_type,
        "site": site,
        "targets": targets,
        "policy": policy,
        "metadata": metadata,
    }
    intent.update(_typed_proposal_section(change_type, proposed))
    return intent


@app.middleware("http")
async def _rbac(request: Request, call_next):
    path = request.url.path
    normalized_path = path.rstrip("/") or "/"
    if _PRODUCTION_RUNTIME and (
        normalized_path in _RESERVED_DOC_PATHS
        or normalized_path.startswith(("/docs/", "/redoc/"))
    ):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    authorization = request.headers.get("authorization")
    cookie_token = (request.cookies.get("netcode_session") or "").strip()
    using_cookie_auth = not authorization and bool(cookie_token)
    if not authorization and cookie_token:
        authorization = f"Bearer {cookie_token}"
    service_scoped = bool(request.headers.get("x-rezonance-org-id"))
    if service_scoped:
        try:
            request.state.principal = _trusted_rez_service_principal(request.headers, authorization)
        except PermissionError:
            return JSONResponse({"detail": "Trusted Rez service token is invalid."}, status_code=401)
        except EntitlementError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
    elif auth_enabled():
        request.state.principal = resolve_principal(PlatformStore(paths()), authorization)
    else:
        request.state.principal = SYSTEM_PRINCIPAL
    principal = request.state.principal

    # Public UI shell, health, login, static assets, and the runner data plane
    # (which authenticates with its own runner token) bypass user RBAC.
    bypass = (
        path in _PUBLIC_EXACT
        or path.startswith("/static")
        or path.startswith("/api/runner/")
        or _is_rez_bridge_request(path, authorization)
    )
    if auth_enabled() and not bypass:
        if not principal.authenticated:
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        if using_cookie_auth and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = (request.headers.get("origin") or request.headers.get("referer") or "").strip()
            origin_host = (urlparse(origin).hostname or "").strip().lower()
            request_host = (request.url.hostname or "").strip().lower()
            if _PRODUCTION_RUNTIME and origin_host != request_host:
                return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
        required = (
            "admin"
            if path in _ADMIN_PATHS
            else "viewer"
            if path in _VIEWER_MUTATION_PATHS
            else "operator"
            if request.method in ("POST", "PUT", "PATCH", "DELETE")
            else "viewer"
        )
        if not principal.has_role(required):
            return JSONResponse({"detail": f"This action requires the '{required}' role."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self' ws: wss:; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


@app.get("/")
def index() -> FileResponse:
    static = paths().static / "index.html"
    if not static.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    return FileResponse(static, headers={"Cache-Control": "no-store"})


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
    try:
        entitlement_status: dict[str, object] = {"ok": True, **get_entitlements().as_dict()}
    except EntitlementError as exc:
        entitlement_status = {"ok": False, "enforced": enforcement_enabled(), "error": str(exc)}
    return {
        "ok": True,
        "workspace": str(p.root),
        "lab": _lab_summary(lab_status()),
        "execution": {"mode": execution_mode(), "pool": runner_pool()},
        "entitlements": entitlement_status,
    }


@app.get("/api/ready")
def ready() -> JSONResponse:
    """Load-balancer readiness probe with no configuration or secret disclosure."""
    issues = _netcode_production_readiness_issues()
    return JSONResponse(
        status_code=200 if not issues else 503,
        content={"status": "ready" if not issues else "not_ready"},
    )


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
        principal = _request_principal(http_request)
        _require_entitled_change_type(principal.org_id, "add_vlan")
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
        result = run_static_pipeline(p, intent_path, org_id=principal.org_id)
        store = PlatformStore(p)
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
    except EntitlementError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_entitled_change_type(org_id: str, change_type: str) -> None:
    entitlements = get_entitlements(org_id=org_id)
    if change_type not in entitled_change_types(entitlements.max_workflow_packs):
        raise EntitlementError(
            f"Change type '{change_type}' is not included in the {entitlements.plan_id} workflow pack allowance."
        )


@app.get("/api/desired-state/catalog")
def desired_state_catalog(request: Request) -> dict[str, object]:
    entitlements = get_entitlements(org_id=_request_principal(request).org_id)
    allowed = entitled_change_types(entitlements.max_workflow_packs)
    return {
        "change_types": [
            item
            for item in desired_state_catalog_from_config(read_ui_config(paths()))
            if str(item.get("id") or "") in allowed
        ],
        "config_path": str(ui_config_path(paths())),
    }


@app.get("/api/workflow-packs")
def api_workflow_packs(request: Request) -> dict[str, object]:
    entitlements = get_entitlements(org_id=_request_principal(request).org_id)
    return workflow_pack_catalog(entitlements.max_workflow_packs)


@app.post("/api/workflow-packs/ansible/plan")
def api_ansible_pack_plan(request: AnsiblePackPlanRequest) -> dict[str, object]:
    try:
        return build_ansible_pack_plan(
            paths().root,
            playbook_path=request.playbook_path,
            rollback_playbook_path=request.rollback_playbook_path,
            targets=request.targets,
            mode=request.mode,
            requested_by=request.requested_by,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workflow-packs/ansible/generate")
def api_ansible_pack_generate(request: GuidedAnsibleRequest) -> dict[str, object]:
    try:
        generated = build_guided_ansible_playbook(
            paths().root,
            name=request.name,
            platform=request.platform,
            operation=request.operation,
            commands=request.commands,
            rollback_commands=request.rollback_commands,
        )
        generated["plan"] = build_ansible_pack_plan(
            paths().root,
            playbook_path=str(generated["playbook_path"]),
            rollback_playbook_path=str(generated["rollback_playbook_path"]),
            targets=request.targets,
            mode="check",
            requested_by=request.requested_by,
        )
        return generated
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workflow-packs/ansible/run")
def api_ansible_pack_run(request: AnsiblePackPlanRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    try:
        plan = build_ansible_pack_plan(
            p.root,
            playbook_path=request.playbook_path,
            rollback_playbook_path=request.rollback_playbook_path,
            targets=request.targets,
            mode=request.mode,
            requested_by=request.requested_by,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not plan.get("ok"):
        return {"ok": False, "queued": False, "status": "blocked", "plan": plan, "message": "Ansible plan is blocked."}
    mode = str(plan.get("mode") or "check")
    if mode in {"canary", "apply", "rollback"}:
        require_production_writes(org_id=_request_principal(http_request).org_id)
    if not request.targets:
        return {
            "ok": False,
            "queued": False,
            "status": "blocked",
            "plan": plan,
            "message": "Ansible execution requires explicit target device IDs.",
        }
    try:
        playbook_bundle = package_ansible_playbooks(
            p.root,
            playbook_path=request.playbook_path,
            rollback_playbook_path=request.rollback_playbook_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if execution_mode() != "runner":
        return {
            "ok": False,
            "queued": False,
            "status": "blocked",
            "plan": plan,
            "message": "Ansible execution is runner-only because device credentials stay local.",
        }
    store = PlatformStore(p)
    principal = _request_principal(http_request)
    change = None
    if request.change_id:
        try:
            change = store.get_change(request.change_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Unknown change {request.change_id}") from exc
        if change.org_id != principal.org_id:
            raise HTTPException(status_code=404, detail=f"Unknown change {request.change_id}")
    if mode in {"canary", "apply"}:
        if not change:
            return {
                "ok": False,
                "queued": False,
                "status": "blocked",
                "plan": plan,
                "message": "Canary/apply Ansible execution requires an approved Netcode change.",
            }
        if change.workflow_state != "approved":
            return {
                "ok": False,
                "queued": False,
                "status": "blocked",
                "plan": plan,
                "message": f"Approval required before Ansible {mode} (state: {change.workflow_state}).",
            }
    if not change:
        marker = p.intents / "ansible" / f"ansible-{uuid.uuid4().hex[:8]}.yaml"
        write_yaml(marker, {
            "kind": "ansible_pack",
            "playbook_path": request.playbook_path,
            "rollback_playbook_path": request.rollback_playbook_path,
            "targets": request.targets,
            "mode": mode,
            "metadata": {"requested_by": request.requested_by, "source": "netcode_ansible"},
        })
        change = store.create_change(
            marker,
            request.targets[0] if request.targets else None,
            requested_by=request.requested_by,
            org_id=principal.org_id,
            created_by_user_id=principal.user_id,
        )
        change = store.update_change(change.id, "validated", {"source": "ansible", "plan": plan}, workflow_state="validated")
    payload = {
        "action": "ansible_pack",
        "mode": mode,
        "playbook_path": request.playbook_path,
        "rollback_playbook_path": request.rollback_playbook_path,
        "targets": request.targets,
        "plan": plan,
        **playbook_bundle,
    }
    job = store.queue_job(change.id, f"ansible_{mode}", runner_pool(), payload)
    store.record_workflow_event(
        change.id,
        f"ansible_{mode}",
        change.workflow_state,
        change.workflow_state,
        f"Queued Ansible {mode} for runner pool '{runner_pool()}'.",
        {"job_id": job.id, "mode": mode, "runner_only": True},
    )
    return {
        "ok": True,
        "queued": True,
        "change": record_to_dict(store.get_change(change.id)),
        "job": record_to_dict(job),
        "plan": plan,
    }


@app.post("/api/desired-state/plan")
def desired_state_plan(request: DesiredStatePlanRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    try:
        principal = _request_principal(http_request)
        _require_entitled_change_type(principal.org_id, request.change_type)
        store = PlatformStore(p)
        configured_inventory = Inventory(configured_inventory_path(p))
        inventory_device = configured_inventory.find_device(request.device_id)
        catalog_device = store.resolve_device(principal.org_id, request.device_id)
        target_platform = (
            inventory_device.platform
            if inventory_device is not None
            else str(catalog_device.get("platform") or "unknown")
            if catalog_device is not None
            else str(configured_inventory.defaults.get("platform") or "unknown")
        )
        change_spec = spec_for(request.change_type)
        if change_spec.lab_write:
            AdapterRegistry.require_execution_support(target_platform, request.change_type)
        model_context: dict[str, object] | None = None
        if request.environment_id.strip():
            active_model = NetworkModelRepository(store).active_revision(
                principal.org_id, request.environment_id.strip()
            )
            if active_model is None:
                raise ValueError("No active Network Model exists for the selected environment.")
            if request.model_revision_id.strip() and request.model_revision_id.strip() != active_model["revision_id"]:
                raise ValueError(
                    f"Network Model revision changed from {request.model_revision_id} "
                    f"to {active_model['revision_id']}; refresh the plan."
                )
            canonical_device_id = str(catalog_device.get("canonical_id")) if catalog_device else request.device_id
            domain_by_change = {
                "add_vlan": "topology",
                "interface_config": "topology",
                "bgp_neighbor": "routing",
                "routing_redistribution": "route_propagation",
                "acl_rule": "security_policy",
                "ntp_standardize": "golden_standards",
            }
            required_domain = domain_by_change.get(request.change_type, "")
            model_context = compile_effective_device(
                active_model,
                canonical_device_id,
                required_domains=[required_domain] if required_domain else [],
            )
            if not model_context["operationally_usable"]:
                raise ValueError(
                    "The active Network Model does not cover the domain required by this change: "
                    + ", ".join(model_context["missing_coverage"])
                )
        intent_path = create_desired_state_intent(
            p,
            change_type=request.change_type,
            site=request.site,
            device_id=request.device_id,
            requested_by=request.requested_by,
            values=request.values,
        )
        intent = load_intent(intent_path)
        result = run_static_pipeline(
            p,
            intent_path,
            org_id=principal.org_id,
            platform=target_platform if change_spec.lab_write else "arista_eos",
        )
        change = store.get_or_create_change(intent_path, request.device_id, requested_by=request.requested_by, org_id=principal.org_id, created_by_user_id=principal.user_id)
        candidate = None
        if model_context is not None and result.status == "pass":
            candidate = create_candidate_for_change_intent(
                NetworkModelRepository(store),
                store,
                org_id=principal.org_id,
                environment_id=str(active_model["environment_id"]),
                parent_revision_id=str(active_model["revision_id"]),
                change_id=change.id,
                intent=intent.model_dump(mode="json"),
                device_id=str(model_context["device_id"]),
                created_by=principal.email or principal.user_id or request.requested_by,
            )
        workflow = state_after_static_validation(result.status == "pass")
        metadata = plan_metadata(intent)
        result_payload = result.model_dump()
        result_payload["plan"] = metadata
        if model_context is not None:
            result_payload["network_model"] = {
                "revision_id": model_context["revision_id"],
                "device_id": model_context["device_id"],
                "site_id": model_context["site_id"],
                "coverage": model_context["coverage"],
                "candidate_revision_id": candidate["revision_id"] if candidate else "",
            }
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
                "network_model_revision": model_context["revision_id"] if model_context else "",
            },
        )
        return {
            "ok": result.status == "pass",
            "change": record_to_dict(store.get_change(change.id)),
            "intent_path": str(intent_path),
            "pipeline": result.model_dump(),
            "plan": metadata,
            "network_model": result_payload.get("network_model"),
            "workflow": workflow.as_dict(),
        }
    except EntitlementError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline")
def pipeline(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    try:
        principal = _request_principal(http_request)
        result = run_static_pipeline(p, Path(request.intent_path), org_id=principal.org_id)
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
def api_lab_dry_run(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    try:
        result = JobRunner(paths()).run_lab_action(
            Path(request.intent_path),
            "dry-run",
            request.device_id,
            request.change_id,
            org_id=_request_principal(http_request).org_id,
        )
        change = result.get("change")
        if isinstance(change, dict) and change.get("workflow_state"):
            result["workflow"] = workflow_snapshot(str(change["workflow_state"])).as_dict()  # type: ignore[arg-type]
        else:
            result["workflow"] = state_after_lab_action("dry-run", bool(result.get("ok"))).as_dict()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lab/apply")
def api_lab_apply(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    require_production_writes(org_id=_request_principal(http_request).org_id)
    try:
        result = JobRunner(paths()).run_lab_action(
            Path(request.intent_path),
            "apply",
            request.device_id,
            request.change_id,
            org_id=_request_principal(http_request).org_id,
        )
        change = result.get("change")
        if isinstance(change, dict) and change.get("workflow_state"):
            result["workflow"] = workflow_snapshot(str(change["workflow_state"])).as_dict()  # type: ignore[arg-type]
        else:
            result["workflow"] = state_after_lab_action("apply", bool(result.get("ok"))).as_dict()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lab/rollback")
def api_lab_rollback(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    require_production_writes(org_id=_request_principal(http_request).org_id)
    try:
        result = JobRunner(paths()).run_lab_action(
            Path(request.intent_path),
            "rollback",
            request.device_id,
            request.change_id,
            org_id=_request_principal(http_request).org_id,
        )
        change = result.get("change")
        if isinstance(change, dict) and change.get("workflow_state"):
            result["workflow"] = workflow_snapshot(str(change["workflow_state"])).as_dict()  # type: ignore[arg-type]
        else:
            result["workflow"] = state_after_lab_action("rollback", bool(result.get("ok"))).as_dict()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lab/full-run")
def api_lab_full_run(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    require_production_writes(org_id=_request_principal(http_request).org_id)
    try:
        return JobRunner(paths()).run_full_arista(
            Path(request.intent_path),
            request.device_id,
            apply=True,
            org_id=_request_principal(http_request).org_id,
        )
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


@app.post("/api/network-model/revisions")
def api_network_model_create_revision(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    document = dict(payload)
    document["org_id"] = principal.org_id
    try:
        revision = NetworkModelRepository(PlatformStore(paths())).create_revision(
            document,
            created_by=principal.email or principal.user_id or "system",
        )
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "revision": revision}


@app.get("/api/network-model/revisions")
def api_network_model_list_revisions(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    status: str = Query(default="", max_length=32),
    cursor: str = Query(default="", max_length=500),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, object]:
    result = NetworkModelRepository(PlatformStore(paths())).list_revisions(
        _request_principal(request).org_id,
        environment_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return {"ok": True, **result, "device_connections_opened": 0}


@app.get("/api/network-model/revisions/{revision_id}")
def api_network_model_get_revision(
    request: Request,
    revision_id: str,
    environment_id: str = Query(..., min_length=1, max_length=128),
) -> dict[str, object]:
    try:
        revision = NetworkModelRepository(PlatformStore(paths())).get_revision(
            _request_principal(request).org_id,
            environment_id,
            revision_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "revision": revision, "device_connections_opened": 0}


@app.get("/api/network-model/revisions/{revision_id}/diff")
def api_network_model_revision_diff(
    request: Request,
    revision_id: str,
    environment_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    repository = NetworkModelRepository(PlatformStore(paths()))
    try:
        revision = repository.get_revision(
            _request_principal(request).org_id, environment_id, revision_id
        )
        parent_model: dict[str, object] = {}
        if revision.get("parent_revision_id"):
            parent_model = repository.get_revision(
                _request_principal(request).org_id,
                environment_id,
                str(revision["parent_revision_id"]),
            )["model"]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    changes = model_diff(parent_model, revision["model"])
    return {
        "ok": True,
        "revision_id": revision_id,
        "total": len(changes),
        "changes": changes[:limit],
        "truncated": len(changes) > limit,
        "device_connections_opened": 0,
    }


@app.get("/api/network-model/entities")
def api_network_model_entities(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    revision_id: str = Query(..., min_length=1, max_length=128),
    entity_type: str = Query(default="", max_length=128),
    site: str = Query(default="", max_length=160),
    device_id: str = Query(default="", max_length=200),
    cursor: str = Query(default="", max_length=500),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    repository = NetworkModelRepository(PlatformStore(paths()))
    repository.ensure_materialized_entities(
        _request_principal(request).org_id,
        environment_id,
        revision_id,
    )
    result = repository.list_entities(
        _request_principal(request).org_id,
        environment_id,
        revision_id,
        entity_type=entity_type,
        site=site,
        device_id=device_id,
        cursor=cursor,
        limit=limit,
    )
    return {"ok": True, **result, "device_connections_opened": 0}


@app.post("/api/network-model/import/catalog")
def api_network_model_import_catalog(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    delegated_actor = str(payload.get("reviewed_by") or "").strip() if principal.has_role("admin") else ""
    try:
        result = import_catalog_candidate(
            PlatformStore(paths()),
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or "default"),
            revision_id=str(payload.get("revision_id") or f"catalog-{uuid.uuid4().hex[:12]}"),
            created_by=delegated_actor or principal.email or principal.user_id or "system",
        )
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "device_connections_opened": 0}


@app.get("/api/network-model/conflicts")
def api_network_model_conflicts(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    status: str = Query(default="open", max_length=32),
    cursor: str = Query(default="", max_length=500),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    result = NetworkModelRepository(PlatformStore(paths())).list_conflicts(
        _request_principal(request).org_id,
        environment_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return {"ok": True, **result, "device_connections_opened": 0}


@app.post("/api/network-model/conflicts/{conflict_id}/resolve")
def api_network_model_resolve_conflict(
    request: Request,
    conflict_id: str,
    payload: dict[str, object],
) -> dict[str, object]:
    principal = _request_principal(request)
    resolution = payload.get("resolution")
    delegated_actor = str(payload.get("reviewed_by") or "").strip() if principal.has_role("admin") else ""
    if not isinstance(resolution, dict) or not resolution:
        raise HTTPException(status_code=400, detail="resolution is required")
    try:
        conflict = NetworkModelRepository(PlatformStore(paths())).resolve_conflict(
            principal.org_id,
            str(payload.get("environment_id") or ""),
            conflict_id,
            resolved_by=delegated_actor or principal.email or principal.user_id or "system",
            resolution=resolution,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "conflict": conflict, "device_connections_opened": 0}


@app.post("/api/network-model/import/local-yaml")
def api_network_model_import_local_yaml(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    try:
        result = import_local_yaml_candidate(
            paths(),
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or "default"),
            revision_id=str(payload.get("revision_id") or "yaml-import"),
            created_by=principal.email or principal.user_id or "system",
        )
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "device_connections_opened": 0}


@app.post("/api/network-model/import/approved-design")
def api_network_model_import_approved_design(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    delegated_actor = str(payload.get("reviewed_by") or "").strip() if principal.has_role("admin") else ""
    design = payload.get("design")
    if not isinstance(design, dict):
        raise HTTPException(status_code=400, detail="design must be an approved network-design document")
    try:
        repository = NetworkModelRepository(PlatformStore(paths()))
        result = import_approved_network_design(
            repository,
            design,
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or design.get("namespace") or "default"),
            created_by=delegated_actor or principal.email or principal.user_id or "system",
        )
        checkpoint = approve_with_git(
            repository,
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or design.get("namespace") or "default"),
            revision_id=result["revision"]["revision_id"],
            approved_by=delegated_actor or principal.email or principal.user_id or "system",
            git_root=paths().git_workspace,
        )
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        **result,
        "revision": checkpoint["revision"],
        "git": checkpoint["git"],
        "device_connections_opened": 0,
    }


@app.get("/api/network-model/effective")
def api_network_model_effective(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    revision_id: str = Query(..., min_length=1, max_length=128),
    device_id: str = Query(default="", max_length=200),
    site_id: str = Query(default="", max_length=160),
    required_domains: str = Query(default="", max_length=1000),
    preview: bool = Query(default=False),
) -> dict[str, object]:
    if bool(device_id) == bool(site_id):
        raise HTTPException(status_code=400, detail="provide exactly one of device_id or site_id")
    repository = NetworkModelRepository(PlatformStore(paths()))
    try:
        revision = repository.get_revision(
            _request_principal(request).org_id, environment_id, revision_id
        )
        domains = [item.strip() for item in required_domains.split(",") if item.strip()]
        context = (
            compile_effective_device(
                revision, device_id, required_domains=domains, require_approved=not preview
            )
            if device_id
            else compile_site_context(
                revision, site_id, required_domains=domains, require_approved=not preview
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NetworkModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "context": context, "device_connections_opened": 0}


@app.get("/api/network-model/rez-design")
def api_network_model_rez_design(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    revision_id: str = Query(..., min_length=1, max_length=128),
) -> dict[str, object]:
    try:
        revision = NetworkModelRepository(PlatformStore(paths())).get_revision(
            _request_principal(request).org_id, environment_id, revision_id
        )
        design = to_rez_network_design(revision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NetworkModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "design": design, "device_connections_opened": 0}


@app.post("/api/network-model/observations")
def api_network_model_observation(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    document = dict(payload)
    document["org_id"] = principal.org_id
    document["source"] = "manual_review"
    document["collector_id"] = f"user:{principal.user_id or 'system'}"
    document["validation_grade"] = "unknown"
    try:
        result = NetworkModelRepository(PlatformStore(paths())).record_observation(document)
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "device_connections_opened": 0}


@app.get("/api/network-model/observations")
def api_network_model_observations(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    domain: str = Query(default="", max_length=128),
    subject_id: str = Query(default="", max_length=200),
    cursor: str = Query(default="", max_length=500),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    result = NetworkModelRepository(PlatformStore(paths())).list_observations(
        _request_principal(request).org_id,
        environment_id,
        domain=domain,
        subject_id=subject_id,
        cursor=cursor,
        limit=limit,
    )
    return {"ok": True, **result, "device_connections_opened": 0}


@app.post("/api/network-model/reconcile")
def api_network_model_reconcile(request: Request, payload: dict[str, object]) -> dict[str, object]:
    environment_id = str(payload.get("environment_id") or "")
    revision_id = str(payload.get("revision_id") or "")
    if not environment_id or not revision_id:
        raise HTTPException(status_code=400, detail="environment_id and revision_id are required")
    repository = NetworkModelRepository(PlatformStore(paths()))
    try:
        revision = repository.get_revision(
            _request_principal(request).org_id, environment_id, revision_id
        )
        result = reconcile_revision(
            repository,
            revision,
            site_id=str(payload.get("site_id") or ""),
            device_id=str(payload.get("device_id") or ""),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NetworkModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "reconciliation": result, "device_connections_opened": 0}


@app.post("/api/network-model/revisions/from-change")
def api_network_model_from_change(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    model_patch = payload.get("model_patch")
    domains = payload.get("domains")
    if not isinstance(model_patch, dict) or not isinstance(domains, list):
        raise HTTPException(status_code=400, detail="model_patch and domains are required")
    store = PlatformStore(paths())
    try:
        revision = create_candidate_from_change(
            NetworkModelRepository(store),
            store,
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or ""),
            parent_revision_id=str(payload.get("parent_revision_id") or ""),
            revision_id=str(payload.get("revision_id") or ""),
            change_id=str(payload.get("change_id") or ""),
            domains=[str(item) for item in domains],
            model_patch=model_patch,
            created_by=principal.email or principal.user_id or "system",
        )
    except (KeyError, NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "revision": revision, "device_connections_opened": 0}


@app.post("/api/network-model/revisions/{revision_id}/approve")
def api_network_model_approve(request: Request, revision_id: str, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    delegated_actor = str(payload.get("reviewed_by") or "").strip() if principal.has_role("admin") else ""
    environment_id = str(payload.get("environment_id") or "")
    repository = NetworkModelRepository(PlatformStore(paths()))
    cursor = ""
    while True:
        page = repository.list_conflicts(
            principal.org_id,
            environment_id,
            status="",
            cursor=cursor,
            limit=100,
        )
        if any(
            str(item.get("details", {}).get("revision_id") or "") == revision_id
            for item in page["conflicts"]
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This proposal contains identity or site conflicts. Correct the source, "
                    "resolve the conflict record, and build a fresh proposal before approval."
                ),
            )
        cursor = str(page.get("next_cursor") or "")
        if not cursor:
            break
    try:
        result = approve_with_git(
            repository,
            org_id=principal.org_id,
            environment_id=environment_id,
            revision_id=revision_id,
            approved_by=delegated_actor or principal.email or principal.user_id or "system",
            git_root=paths().git_workspace,
        )
    except (KeyError, NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "device_connections_opened": 0}


@app.post("/api/network-model/revisions/{revision_id}/activate")
def api_network_model_activate(request: Request, revision_id: str, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    delegated_actor = str(payload.get("reviewed_by") or "").strip() if principal.has_role("admin") else ""
    store = PlatformStore(paths())
    try:
        result = activate_verified_revision(
            NetworkModelRepository(store),
            store,
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or ""),
            revision_id=revision_id,
            actor=delegated_actor or principal.email or principal.user_id or "system",
            git_root=paths().git_workspace,
            initial_baseline=bool(payload.get("initial_baseline")),
        )
    except (KeyError, NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "device_connections_opened": 0}


@app.post("/api/network-model/rollback")
def api_network_model_rollback(request: Request, payload: dict[str, object]) -> dict[str, object]:
    principal = _request_principal(request)
    store = PlatformStore(paths())
    try:
        result = rollback_active_revision(
            NetworkModelRepository(store),
            store,
            org_id=principal.org_id,
            environment_id=str(payload.get("environment_id") or ""),
            target_revision_id=str(payload.get("target_revision_id") or ""),
            rollback_change_id=str(payload.get("rollback_change_id") or ""),
            actor=principal.email or principal.user_id or "system",
            git_root=paths().git_workspace,
        )
    except (KeyError, NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "device_connections_opened": 0}


@app.get("/api/network-model/active")
def api_network_model_active(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
    include_model: bool = Query(default=False),
) -> dict[str, object]:
    repository = NetworkModelRepository(PlatformStore(paths()))
    revision = (
        repository.active_revision(
            _request_principal(request).org_id, environment_id
        )
        if include_model
        else repository.active_revision_summary(
            _request_principal(request).org_id, environment_id
        )
    )
    return {"ok": True, "revision": revision, "device_connections_opened": 0}


@app.get("/api/network-model/active/rez-design")
def api_network_model_active_rez_design(
    request: Request,
    environment_id: str = Query(..., min_length=1, max_length=128),
) -> dict[str, object]:
    revision = NetworkModelRepository(PlatformStore(paths())).active_revision(
        _request_principal(request).org_id, environment_id
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="No active Network Model revision exists for this environment")
    try:
        design = to_rez_network_design(revision)
    except NetworkModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "revision_id": revision["revision_id"],
        "design": design,
        "device_connections_opened": 0,
    }


@app.get("/api/devices")
def api_devices(
    request: Request,
    q: str = Query(default="", max_length=200),
    site: str = Query(default="", max_length=120),
    role: str = Query(default="", max_length=120),
    platform: str = Query(default="", max_length=120),
    cursor: str = Query(default="", max_length=300),
    limit: int = Query(default=50, ge=1, le=50),
) -> dict[str, object]:
    """Search public runner inventory metadata without touching a device."""
    result = PlatformStore(paths()).query_devices(
        _request_principal(request).org_id,
        query=q,
        site=site,
        role=role,
        platform=platform,
        cursor=cursor,
        limit=limit,
    )
    channels = globals().get("_RUNNER_CHANNELS", {})
    for device in result["devices"]:
        runner_id = str(device.get("runner_id") or "")
        connected = runner_id in channels
        device["runner_connected"] = connected
        device["connectable"] = connected
    result.update({"ok": True, "device_connections_opened": 0})
    return result


@app.get("/api/devices/resolve")
def api_devices_resolve(
    request: Request,
    ids: str = Query(default="", max_length=10_000),
) -> dict[str, object]:
    identifiers = [item.strip() for item in ids.split(",") if item.strip()][:50]
    devices = PlatformStore(paths()).devices_by_identifiers(_request_principal(request).org_id, identifiers)
    channels = globals().get("_RUNNER_CHANNELS", {})
    for device in devices:
        connected = str(device.get("runner_id") or "") in channels
        device["runner_connected"] = connected
        device["connectable"] = connected
    return {
        "ok": True,
        "devices": devices,
        "returned": len(devices),
        "total": len(devices),
        "next_cursor": None,
        "facets": {},
        "device_connections_opened": 0,
    }


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
    return git_workspace_status(paths().git_workspace)


@app.post("/api/git/setup")
def api_git_setup(request: GitSetupRequest) -> dict[str, object]:
    p = paths()
    config = read_ui_config(p)
    git_config = config.get("git", {})
    # Omitted means use the configured remote; an explicit blank selects a
    # local Community repository with no cloud dependency.
    repo_url = (
        str(git_config.get("repo_url") or "")
        if request.repo_url is None
        else request.repo_url.strip()
    )
    branch = request.branch or str(git_config.get("branch") or "main")
    return setup_git_workspace(p.git_workspace, repo_url=repo_url, branch=branch)


@app.get("/api/git/branches")
def api_git_branches() -> dict[str, object]:
    return list_git_branches(paths().git_workspace)


@app.post("/api/git/branch")
def api_git_branch(request: GitBranchRequest) -> dict[str, object]:
    return create_change_branch(paths().git_workspace, name=request.name, base=request.base)


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
def api_git_commit(request: GitCommitRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    if not request.change_id:
        return {
            "ok": False,
            "action": "blocked",
            "message": "Select one reviewed change before creating a history checkpoint.",
            "change_event_recorded": False,
        }
    config = read_ui_config(p)
    default_message = str((config.get("git") or {}).get("default_commit_message") or "Netcode network change")
    git_root = p.git_workspace
    if not git_workspace_status(git_root).get("available"):
        setup_git_workspace(git_root, branch="main")
    branch_name = f"change/{request.change_id.replace('-', '')[:12].lower()}"
    branch = create_change_branch(git_root, name=branch_name, base="main")
    if not branch.get("ok"):
        result = dict(branch)
        result.update(_record_git_event(p, request.change_id, "git_commit", result))
        return result
    try:
        record = api_change_record(request.change_id, http_request)
        materialized = materialize_change_artifacts(
            git_root,
            workspace_root=p.root,
            change_id=request.change_id,
            record=record,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "action": "blocked",
            "message": f"Could not prepare reviewed change artifacts: {exc}",
            "branch": branch.get("current"),
        }
        result.update(_record_git_event(p, request.change_id, "git_commit", result))
        return result
    result = commit_change_artifacts(
        git_root,
        message=request.message or default_message,
        add_paths=list(materialized.get("paths") or []),
    )
    result["workspace"] = str(git_root)
    result["change_directory"] = materialized.get("change_directory")
    result["change_history_protected"] = True
    result.update(_record_git_event(p, request.change_id, "git_commit", result))
    return result


@app.post("/api/git/push")
def api_git_push(request: GitPushRequest) -> dict[str, object]:
    p = paths()
    result = push_current_branch(p.git_workspace)
    result.update(_record_git_event(p, request.change_id, "git_push", result))
    return result


def _admin_guard(authorization: str | None) -> None:
    """Admin endpoints are open when NETCODE_ADMIN_TOKEN is unset (local dev), token-gated otherwise."""
    required = os.environ.get("NETCODE_ADMIN_TOKEN", "").strip()
    if not required:
        return
    if authorization != f"Bearer {required}":
        raise HTTPException(status_code=401, detail="Admin token required.")


def _runner_bearer_token(authorization: str | None) -> str:
    return (authorization or "").removeprefix("Bearer ").strip()


def _require_runner(store: PlatformStore, authorization: str | None):
    token = _runner_bearer_token(authorization)
    runner = authenticate_runner(store, token)
    if runner is None:
        raise HTTPException(status_code=401, detail="Runner token is invalid or revoked.")
    return runner


@app.post("/api/auth/login")
def api_login(request: LoginRequest) -> JSONResponse:
    store = PlatformStore(paths())
    org_id = request.org_id or DEFAULT_ORG_ID
    user = store.get_user_by_email(org_id, request.email)
    if not user or not verify_password(request.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = mint_session(store, str(user["id"]), org_id)
    response = JSONResponse(
        {
            "ok": True,
            "token": token,
            "user": {"email": user["email"], "role": user["role"], "org_id": org_id},
        }
    )
    response.set_cookie(
        "netcode_session",
        token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=_PRODUCTION_RUNTIME,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def api_logout(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    token = (authorization or "").removeprefix("Bearer ").strip() or (request.cookies.get("netcode_session") or "").strip()
    if token:
        PlatformStore(paths()).revoke_session(token_hash(token))
    response = JSONResponse({"ok": True})
    response.delete_cookie("netcode_session", path="/", httponly=True, secure=_PRODUCTION_RUNTIME, samesite="lax")
    return response


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
    store = PlatformStore(paths())
    enforce_capacity(
        "connectors",
        current=store.active_runner_count(principal.org_id),
        additional=1,
        org_id=principal.org_id,
    )
    return mint_join_token(store, request.pool, org_id=principal.org_id)


@app.get("/api/runners")
def api_list_runners(request: Request) -> dict[str, object]:
    return runner_summary(PlatformStore(paths()), org_id=_request_principal(request).org_id)


def _admin_runner_for_request(runner_id: str, request: Request) -> tuple[PlatformStore, object]:
    principal = _request_principal(request)
    if not principal.has_role("admin"):
        raise HTTPException(status_code=403, detail="This action requires the 'admin' role.")
    store = PlatformStore(paths())
    try:
        runner = store.get_runner(runner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown runner {runner_id}") from exc
    if runner.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail=f"Unknown runner {runner_id}")
    return store, runner


@app.post("/api/runners/{runner_id}/drain")
def api_runner_drain(runner_id: str, request: Request) -> dict[str, object]:
    store, _runner = _admin_runner_for_request(runner_id, request)
    runner = store.set_runner_drain(runner_id, _request_principal(request).org_id, requested=True)
    return {
        "ok": True,
        "runner": record_to_dict(runner),
        "message": "Connector is draining; its active job may finish, but no new work will be claimed.",
    }


@app.post("/api/runners/{runner_id}/resume")
def api_runner_resume(runner_id: str, request: Request) -> dict[str, object]:
    store, _runner = _admin_runner_for_request(runner_id, request)
    runner = store.set_runner_drain(runner_id, _request_principal(request).org_id, requested=False)
    return {"ok": True, "runner": record_to_dict(runner), "message": "Connector resumed."}


@app.post("/api/runners/{runner_id}/revoke")
async def api_runner_revoke(runner_id: str, request: Request) -> dict[str, object]:
    store, _runner = _admin_runner_for_request(runner_id, request)
    principal = _request_principal(request)
    actor = principal.email or principal.user_id or "netcode-admin"
    try:
        runner = store.revoke_runner(runner_id, principal.org_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.record_runner_security_event(
        runner.id,
        runner.org_id,
        "token_revoked",
        actor,
        {"reason": "administrator request"},
    )
    channel = _RUNNER_CHANNELS.pop(runner_id, None)
    _RUNNER_CHANNEL_POOLS.pop(runner_id, None)
    await _terminate_shells_for_runner(runner_id, "connector_revoked")
    if channel is not None:
        try:
            await channel.close(code=4403)
        except Exception:  # noqa: BLE001 - the connector may already be disconnected.
            pass
    return {
        "ok": True,
        "runner": record_to_dict(runner),
        "message": "Connector credential revoked; re-enrollment is required.",
    }


@app.get("/api/runners/{runner_id}/security-events")
def api_runner_security_events(runner_id: str, request: Request) -> dict[str, object]:
    store, _runner = _admin_runner_for_request(runner_id, request)
    principal = _request_principal(request)
    events = store.list_runner_security_events(runner_id, principal.org_id)
    return {"ok": True, "runner_id": runner_id, "events": events, "count": len(events)}


@app.post("/api/runner/enroll")
def api_runner_enroll(request: RunnerEnrollRequest) -> dict[str, object]:
    store = PlatformStore(paths())
    return enroll_runner(store, request.join_token, request.name)


@app.post("/api/runner/token/rotate")
def api_runner_token_rotate(authorization: str | None = Header(default=None)) -> dict[str, object]:
    store = PlatformStore(paths())
    token = _runner_bearer_token(authorization)
    runner = _require_runner(store, authorization)
    try:
        return prepare_runner_token_rotation(store, runner, token)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/runner/token/confirm")
def api_runner_token_confirm(authorization: str | None = Header(default=None)) -> dict[str, object]:
    store = PlatformStore(paths())
    token = _runner_bearer_token(authorization)
    runner = _require_runner(store, authorization)
    try:
        return confirm_runner_token_rotation(store, runner, token)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/runner/poll")
def api_runner_poll(request: RunnerPollRequest, authorization: str | None = Header(default=None)):
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    # Check before claim so suspension never converts queued work to running.
    get_entitlements(org_id=runner.org_id)
    job = poll_for_job(store, runner, request.wait_seconds)
    if job is None:
        return Response(status_code=204)
    if job_requires_production_writes(job.action):
        try:
            require_production_writes(org_id=runner.org_id)
        except EntitlementError as exc:
            store.update_job(
                job.id,
                "failed",
                f"Blocked before runner execution by platform entitlement: {exc}",
                {"error": "production_write_not_entitled", "message": str(exc)},
            )
            return Response(status_code=204)
    serialized = record_to_dict(job)
    serialized["lease_token"] = job.lease_token
    serialized["lease_seconds"] = job_lease_seconds()
    return {"ok": True, "job": serialized}


@app.post("/api/runner/jobs/{job_id}/lease")
def api_runner_job_lease(
    job_id: str,
    request: RunnerLeaseRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    expires_at = store.renew_job_lease(job_id, runner.id, request.lease_token)
    if not expires_at:
        raise HTTPException(status_code=409, detail="Connector job lease is missing, stale, or expired.")
    store.touch_runner(runner.id, status="online")
    return {"ok": True, "job_id": job_id, "lease_expires_at": expires_at}


@app.post("/api/runner/jobs/{job_id}/result")
def api_runner_job_result(job_id: str, request: RunnerResultRequest, authorization: str | None = Header(default=None)) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    result = submit_job_result(
        store,
        runner,
        job_id,
        dict(request.result),
        request.signature,
        request.lease_token,
    )
    if not result.get("ok") and "lease" in str(result.get("message") or "").lower():
        raise HTTPException(status_code=409, detail=str(result.get("message") or "Result rejected."))
    return result


@app.post("/api/runner/jobs/{job_id}/progress")
def api_runner_job_progress(
    job_id: str,
    request: RunnerProgressRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    result = submit_job_progress(
        store,
        runner,
        job_id,
        dict(request.event),
        request.signature,
        request.lease_token,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("message") or "Progress rejected."))
    return result


@app.post("/api/runner/heartbeat")
def api_runner_heartbeat(request: RunnerHeartbeatRequest, authorization: str | None = Header(default=None)) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    try:
        runner = store.heartbeat_runner(runner.id, version=request.version, state=request.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "runner_id": runner.id,
        "pool": runner.pool,
        "status": runner.status,
        "drain_requested": runner.drain_requested,
    }


_RUNNER_INVENTORY_FIELDS = {
    "id", "hostname", "host", "port", "platform", "site", "role", "groups", "aliases", "management", "serial",
    "building", "floor", "closet", "location",
}
_RUNNER_INVENTORY_SECRET_MARKERS = {
    "username", "password", "passwd", "pwd", "secret", "token", "credential",
    "passphrase", "api_key", "apikey", "private_key", "privatekey", "enable_secret",
}


def _sanitize_runner_inventory(devices: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(devices) > 100_000:
        raise HTTPException(status_code=413, detail="Runner inventory exceeds the 100,000-device sync limit.")
    public: list[dict[str, object]] = []
    for index, raw in enumerate(devices):
        keys = {str(key).strip().lower() for key in raw}
        secret_keys = sorted(
            key for key in keys if any(marker in key for marker in _RUNNER_INVENTORY_SECRET_MARKERS)
        )
        if secret_keys:
            raise HTTPException(
                status_code=400,
                detail=f"Runner inventory item {index} contains forbidden credential fields: {', '.join(secret_keys)}.",
            )
        unknown = sorted(keys - _RUNNER_INVENTORY_FIELDS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Runner inventory item {index} contains unsupported fields: {', '.join(unknown)}.",
            )
        device_id = str(raw.get("id") or "").strip()
        host = str(raw.get("host") or "").strip()
        scalar_fields = (
            "id", "hostname", "host", "port", "platform", "site", "role", "serial",
            "building", "floor", "closet",
        )
        invalid_scalar = next(
            (field for field in scalar_fields if isinstance(raw.get(field), (dict, list, tuple, set))),
            None,
        )
        if invalid_scalar:
            raise HTTPException(
                status_code=400,
                detail=f"Runner inventory item {index} field {invalid_scalar} must be a scalar value.",
            )
        if not device_id or not host:
            raise HTTPException(status_code=400, detail=f"Runner inventory item {index} requires id and host.")
        try:
            port = int(raw.get("port") or 22)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Runner inventory item {index} has an invalid port.") from exc
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=400, detail=f"Runner inventory item {index} has an invalid port.")
        groups = raw.get("groups") or []
        aliases = raw.get("aliases") or []
        management = raw.get("management") or {}
        location = raw.get("location") or {}
        if not isinstance(groups, list) or not isinstance(aliases, list):
            raise HTTPException(status_code=400, detail=f"Runner inventory item {index} groups and aliases must be lists.")
        if not isinstance(management, dict):
            raise HTTPException(status_code=400, detail=f"Runner inventory item {index} management must be a mapping.")
        if not isinstance(location, dict):
            raise HTTPException(status_code=400, detail=f"Runner inventory item {index} location must be a mapping.")
        allowed_location_fields = {"campus", "building", "floor", "closet", "room", "rack", "zone"}
        unknown_location = sorted(str(key).strip().lower() for key in location if str(key).strip().lower() not in allowed_location_fields)
        if unknown_location:
            raise HTTPException(
                status_code=400,
                detail=f"Runner inventory item {index} location contains unsupported fields: {', '.join(unknown_location)}.",
            )
        invalid_location = next(
            (key for key, value in location.items() if not isinstance(value, (str, int, float))),
            None,
        )
        if invalid_location:
            raise HTTPException(
                status_code=400,
                detail=f"Runner inventory item {index} location field {invalid_location} must be a scalar value.",
            )
        if any(not isinstance(item, str) for item in [*groups, *aliases]):
            raise HTTPException(
                status_code=400,
                detail=f"Runner inventory item {index} groups and aliases may contain strings only.",
            )
        public.append({
            "id": device_id,
            "hostname": str(raw.get("hostname") or device_id).strip(),
            "host": host,
            "port": port,
            "platform": str(raw.get("platform") or "unknown").strip().lower(),
            "site": str(raw.get("site") or "").strip(),
            "role": str(raw.get("role") or "").strip(),
            "groups": [str(item).strip() for item in groups if str(item).strip()],
            "aliases": [str(item).strip() for item in aliases if str(item).strip()],
            "serial": str(raw.get("serial") or "").strip(),
            "building": str(raw.get("building") or "").strip()[:256],
            "floor": str(raw.get("floor") or "").strip()[:256],
            "closet": str(raw.get("closet") or "").strip()[:256],
            "location": {
                key: str(location[key]).strip()[:256]
                for key in ("campus", "building", "floor", "closet", "room", "rack", "zone")
                if isinstance(location.get(key), (str, int, float)) and str(location[key]).strip()
            },
            "management": management,
        })
    return public


@app.post("/api/runner/inventory-sync")
def api_runner_inventory_sync(
    request: RunnerInventorySyncRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    public = _sanitize_runner_inventory(list(request.devices))
    _enforce_catalog_growth(
        store,
        runner.org_id,
        [str(item.get("id") or "") for item in public],
        replace_runner_id=runner.id if request.replace else None,
    )
    synced = store.sync_runner_devices(runner, public, revision=request.revision.strip(), replace=request.replace)
    return {"ok": True, "runner_id": runner.id, "pool": runner.pool, **synced}


@app.post("/api/runner/network-model/observations")
def api_runner_network_model_observation(
    payload: dict[str, object],
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    store = PlatformStore(paths())
    runner = _require_runner(store, authorization)
    document = dict(payload)
    document["org_id"] = runner.org_id
    document["collector_id"] = runner.id
    try:
        result = NetworkModelRepository(store).record_observation(document)
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "runner_id": runner.id, **result}


def _runner_route_for_payload(
    store: PlatformStore,
    org_id: str,
    payload: dict[str, object],
) -> tuple[str, str | None]:
    """Route known-device reads to the connector that advertised the device.

    Discovery of a not-yet-known host intentionally falls back to the configured
    pool; existing catalog devices must never be claimed by a sibling connector.
    """
    identifier = str(
        payload.get("device")
        or payload.get("device_id")
        or payload.get("host")
        or (
            str(payload.get("seed_node") or "").split(",", 1)[0].strip()
            if not any(marker in str(payload.get("seed_node") or "") for marker in ("/", "-"))
            else ""
        )
        or ""
    ).strip()
    catalog_device = store.resolve_device(org_id, identifier) if identifier else None
    if catalog_device is None:
        return runner_pool(), None
    return str(catalog_device["runner_pool"]), str(catalog_device["runner_id"])


def _runner_read(
    p,
    action: str,
    payload: dict,
    org_id: str,
    timeout: float = 60.0,
    *,
    change_id: str | None = None,
    target_runner_id: str | None = None,
) -> dict[str, object]:
    """Runner mode: queue a device-read job and wait for the on-prem runner to report.
    Keeps the browser API synchronous while the actual device I/O happens on the runner."""
    store = PlatformStore(p)
    effective_change_id = change_id or str(payload.get("_progress_change_id") or "") or None
    if target_runner_id:
        try:
            target_runner = store.get_runner(target_runner_id)
        except ValueError:
            return {"ok": False, "error": "Unknown Local Connector."}
        if target_runner.org_id != org_id:
            return {"ok": False, "error": "Unknown Local Connector."}
        pool = target_runner.pool
    else:
        pool, target_runner_id = _runner_route_for_payload(store, org_id, payload)
    job = store.create_read_job(
        org_id,
        pool,
        action,
        payload,
        target_runner_id=target_runner_id,
        change_id=effective_change_id or "__read__",
    )
    if effective_change_id:
        store.record_execution_event(
            event_id=str(uuid.uuid4()),
            job_id=job.id,
            change_id=effective_change_id,
            org_id=org_id,
            device_id=str(payload.get("device_id") or ""),
            phase=action,
            stage="queued",
            status="queued",
            message=f"Queued {action} on the assigned runner.",
            sequence=0,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = store.get_job(job.id)
        if current.status in TERMINAL_JOB_STATUSES:
            result = dict(current.result or {"ok": current.status == "completed", "message": current.message})
            result["_job_id"] = current.id
            result["_runner_id"] = current.claimed_by
            result["_runner_pool"] = current.pool
            return result
        time.sleep(0.4)
    store.cancel_job_if_queued(job.id, f"read deadline: {action} exceeded {int(timeout)}s")
    return {"ok": False, "error": f"No runner completed the {action} read within {int(timeout)}s. Is a runner online for this pool? (Setup → Runners)"}


def _record_rez_refresh_observations(
    store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    runner_id: str,
    result: dict[str, object],
) -> int:
    """Project fresh normalized state into observations without storing raw config."""
    states = result.get("device_states")
    if not isinstance(states, dict) or not environment_id or not runner_id:
        return 0
    repository = NetworkModelRepository(store)
    recorded = 0
    for raw_device_id, raw_state in states.items():
        if not isinstance(raw_state, dict):
            continue
        device_id = PlatformStore.normalize_device_identifier(raw_device_id)
        raw_observed_at = raw_state.get("_collected_at") or result.get("timestamp")
        try:
            observed = datetime.fromisoformat(str(raw_observed_at).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("runner timestamp must include a timezone")
            observed = observed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            observed = datetime.now(timezone.utc)
        expires = observed + timedelta(minutes=10)
        interfaces_source = raw_state.get("interfaces")
        interfaces = interfaces_source if isinstance(interfaces_source, dict) else {}
        safe_interfaces: dict[str, dict[str, object]] = {}
        for raw_name, raw_interface in interfaces.items():
            if not isinstance(raw_interface, dict):
                continue
            ip_prefixes_source = raw_interface.get("ip_prefixes")
            ip_prefixes = ip_prefixes_source if isinstance(ip_prefixes_source, list) else []
            safe_interfaces[str(raw_name)] = {
                "admin_status": str(
                    raw_interface.get("admin_state")
                    or raw_interface.get("admin_status")
                    or raw_interface.get("admin")
                    or "unknown"
                ),
                "oper_status": str(
                    raw_interface.get("oper_state")
                    or raw_interface.get("oper_status")
                    or raw_interface.get("status")
                    or "unknown"
                ),
                "ip_prefixes": [str(item) for item in ip_prefixes if str(item).strip()][:32],
            }
        lldp_source = raw_state.get("lldp_neighbors")
        lldp = lldp_source if isinstance(lldp_source, list) else []
        safe_lldp = [
            {
                "neighbor_id": str(item.get("neighbor_id") or ""),
                "local_interface": str(item.get("local_interface") or ""),
                "neighbor_interface": str(item.get("neighbor_interface") or ""),
            }
            for item in lldp[:256]
            if isinstance(item, dict)
        ]
        routing = raw_state.get("routing") if isinstance(raw_state.get("routing"), dict) else {}
        routes = routing.get("entries") if isinstance(routing.get("entries"), list) else raw_state.get("routes")
        route_rows = routes if isinstance(routes, list) else []
        bgp_source = raw_state.get("bgp_neighbors")
        bgp = bgp_source if isinstance(bgp_source, list) else []
        ospf_source = raw_state.get("ospf_neighbors")
        ospf = ospf_source if isinstance(ospf_source, list) else []
        safe_bgp = [
            {
                "neighbor_ip": str(item.get("neighbor_ip") or item.get("neighbor") or ""),
                "state": str(item.get("state") or "unknown"),
                "remote_as": item.get("remote_as"),
            }
            for item in bgp[:512]
            if isinstance(item, dict)
        ]
        safe_ospf = [
            {
                "neighbor_id": str(item.get("neighbor_id") or item.get("router_id") or ""),
                "state": str(item.get("state") or "unknown"),
                "interface": str(item.get("interface") or item.get("local_interface") or ""),
            }
            for item in ospf[:512]
            if isinstance(item, dict)
        ]
        base = {
            "schema": NETWORK_OBSERVATION_SCHEMA,
            "org_id": org_id,
            "environment_id": environment_id,
            "source": "ssh_api_collection",
            "collector_id": runner_id,
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "validation_grade": "device_authoritative",
            "subject_id": device_id,
            "metadata": {
                "privacy_policy": "rez_control_plane_policy",
                "raw_configuration_stored": False,
            },
        }
        for domain, facts in (
            (
                "topology",
                {"actual": {"interfaces": safe_interfaces, "lldp_neighbors": safe_lldp}},
            ),
            (
                "routing",
                {
                    "actual": {
                        "route_count": len(route_rows),
                        "bgp_neighbors": safe_bgp,
                        "ospf_neighbors": safe_ospf,
                    }
                },
            ),
        ):
            identity = json.dumps(
                {
                    "runner_id": runner_id,
                    "observed_at": base["observed_at"],
                    "device_id": device_id,
                    "domain": domain,
                    "facts": facts,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            document = {
                **base,
                "observation_id": f"obs-{hashlib.sha256(identity).hexdigest()[:20]}",
                "domain": domain,
                "facts": facts,
            }
            stored = repository.record_observation(document)
            if stored["created"]:
                recorded += 1
    return recorded


@app.get("/api/local-connectors/{runner_id}/readiness")
def api_local_connector_readiness(runner_id: str, request: Request) -> dict[str, object]:
    """Return connector-local capability metadata without opening a device session."""
    store = PlatformStore(paths())
    org_id = _request_principal(request).org_id
    try:
        runner = store.get_runner(runner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown Local Connector.") from exc
    if runner.org_id != org_id:
        raise HTTPException(status_code=404, detail="Unknown Local Connector.")
    if str(runner.status or "").lower() != "online":
        return {
            "ok": False,
            "connector": {"id": runner.id, "name": runner.name, "status": runner.status},
            "message": "Local Connector is offline.",
        }
    result = _runner_read(
        paths(),
        "connector_capabilities",
        {},
        org_id,
        timeout=20,
        target_runner_id=runner.id,
    )
    return {
        "ok": bool(result.get("ok")),
        "connector": {
            "id": runner.id,
            "name": runner.name,
            "pool": runner.pool,
            "status": runner.status,
            "version": runner.version,
            "device_count": runner.device_count,
        },
        "readiness": result,
    }


def _reject_cloud_credentials_in_runner_mode(*values: str) -> None:
    if execution_mode() != "runner":
        return
    if any(str(value or "").strip() for value in values):
        raise HTTPException(
            status_code=400,
            detail="Runner mode keeps device credentials local. Configure credentials on the local runner, then submit only public device facts.",
        )


@app.post("/api/rez/runner-read")
def api_rez_runner_read(request: RunnerReadRequest, http_request: Request, authorization: str | None = Header(default=None)) -> dict[str, object]:
    """Bridge endpoint for Rez control-plane tools to execute device reads on the runner.

    The runner resolves credentials from its local inventory; this endpoint
    strips credential-shaped fields before queueing the job.
    """
    bridge_token = os.environ.get("NETCODE_REZ_BRIDGE_TOKEN", "").strip()
    if bridge_token and authorization != f"Bearer {bridge_token}":
        raise HTTPException(status_code=401, detail="Rez bridge token required.")
    if request.action not in {
        "rez_ssh_command",
        "rez_api_query",
        "rez_api_get_state",
        "rez_refresh_targeted",
        "rez_scan_device",
        "rez_discover_network",
        "rez_server_listener_probe",
        "rez_http_flow_probe",
    }:
        raise HTTPException(status_code=400, detail="Unsupported Rez runner action.")
    payload = dict(request.payload or {})
    for secret_key in ("username", "password", "passwd", "secret", "api_token", "private_key"):
        payload.pop(secret_key, None)
    timeout_ceiling = 900.0 if request.action == "rez_discover_network" else 120.0
    timeout = max(1.0, min(float(request.timeout or 60.0), timeout_ceiling))
    payload["_runner_timeout_seconds"] = timeout
    target_runner_id = str(payload.pop("connector_id", "") or "").strip() or None
    store = PlatformStore(paths())
    principal = _request_principal(http_request)
    if target_runner_id:
        result = _runner_read(
            paths(),
            request.action,
            payload,
            principal.org_id,
            timeout=timeout,
            target_runner_id=target_runner_id,
        )
    else:
        result = _runner_read(paths(), request.action, payload, principal.org_id, timeout=timeout)
    if request.action == "rez_refresh_targeted" and result.get("ok"):
        result["network_model_observations_recorded"] = _record_rez_refresh_observations(
            store,
            org_id=principal.org_id,
            environment_id=str(
                payload.get("environment_id")
                or os.environ.get("NETCODE_REZ_ENVIRONMENT_ID")
                or ""
            ),
            runner_id=str(result.get("_runner_id") or ""),
            result=result,
        )
    if request.action == "rez_discover_network":
        result["candidate_disposition"] = {
            "status": "review_required",
            "source_of_truth_written": False,
            "message": "Recursive discovery returns observations. Review identity conflicts and approve imports separately.",
        }
    return result


def _collect_rez_state_for_troubleshooting(device) -> dict[str, object]:  # noqa: ANN001
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(AdapterRegistry().rez.collect_device_state, device)
    try:
        return future.result(timeout=TROUBLESHOOT_READ_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        return {
            "ok": False,
            "device_id": device.id,
            "platform": device.platform,
            "adapter": "rez",
            "state": None,
            "warnings": [],
            "errors": [f"Rez state collection timed out after {TROUBLESHOOT_READ_TIMEOUT_SECONDS}s."],
            "error": f"Rez state collection timed out after {TROUBLESHOOT_READ_TIMEOUT_SECONDS}s.",
        }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _catalog_runner_candidate(
    p: WorkspacePaths,
    org_id: str,
    candidate: dict[str, object],
    runner_result: dict[str, object],
) -> dict[str, object]:
    runner_id = str(runner_result.get("_runner_id") or "").strip()
    if not runner_id:
        return {"ok": False, "error": "Runner result did not identify the local connector."}
    store = PlatformStore(p)
    try:
        runner = store.get_runner(runner_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if runner.org_id != org_id:
        return {"ok": False, "error": "Runner and device belong to different organizations."}
    public = _sanitize_runner_inventory([candidate])
    _enforce_catalog_growth(store, org_id, [str(candidate.get("id") or "")])
    synced = store.sync_runner_devices(
        runner,
        public,
        revision=f"discovery:{uuid.uuid4().hex}",
        replace=False,
    )
    return {"ok": True, "runner_id": runner.id, "runner_pool": runner.pool, **synced}


def _import_runner_discovery_candidate(
    p: WorkspacePaths,
    discovery_result: dict[str, object],
    org_id: str = DEFAULT_ORG_ID,
) -> dict[str, object]:
    """Persist public discovery facts returned by the local runner.

    Runner discovery owns device access and credentials. The control plane only
    receives/imports the sanitized source_of_truth_candidate shape, using the
    same import path as the manual review button.
    """
    result = dict(discovery_result)
    if not result.get("ok"):
        return result
    candidate = result.get("source_of_truth_candidate")
    if not isinstance(candidate, dict):
        result["source_of_truth"] = {
            "ok": False,
            "source_of_truth_written": False,
            "error": "Runner discovery did not return a source_of_truth_candidate.",
        }
        return result
    public_keys = {"id", "hostname", "host", "platform", "site", "role", "groups", "port", "aliases", "serial"}
    candidate = {key: value for key, value in candidate.items() if key in public_keys}

    _enforce_catalog_growth(PlatformStore(p), org_id, [str(candidate.get("id") or "")])

    source_result = DiscoveryService(p).import_candidate(candidate)
    result["source_of_truth"] = source_result
    safety = dict(result.get("safety") or {})
    safety["source_of_truth_written"] = bool(source_result.get("ok"))
    if source_result.get("ok"):
        safety["message"] = "Discovery used runner-local collection and imported public facts into the control-plane source of truth."
        result["device"] = source_result.get("device")
        catalog_result = _catalog_runner_candidate(p, org_id, candidate, result)
        result["device_catalog"] = catalog_result
        if not catalog_result.get("ok"):
            safety["catalog_pending"] = True
            safety["message"] = (
                catalog_result.get("error")
                or "Discovery succeeded; the local connector will synchronize the Shell catalog shortly."
            )
    else:
        safety["message"] = source_result.get("error") or "Source-of-truth import failed."
        result["ok"] = False
    result["safety"] = safety
    return result


@app.post("/api/readiness/devices")
def api_readiness_devices(
    request: Request,
    payload: dict[str, object] = Body(default_factory=dict),
) -> dict[str, object]:
    """Live read test scoped to explicitly selected targets when provided."""
    p = paths()
    requested_ids = [
        str(value).strip()
        for value in (payload.get("device_ids") or [])
        if str(value).strip()
    ]
    if execution_mode() == "runner":
        return _runner_read(
            p,
            "readiness",
            {"device_ids": requested_ids},
            _request_principal(request).org_id,
        )
    inventory = Inventory(configured_inventory_path(p))
    missing: list[str] = []
    if requested_ids:
        devices = []
        for device_id in requested_ids:
            device = inventory.find_device(device_id)
            if device is None:
                missing.append(device_id)
            elif device not in devices:
                devices.append(device)
    else:
        devices = list(inventory.by_id.values())
    if not devices:
        return {
            "ok": False,
            "tested": 0,
            "readable": 0,
            "devices": [
                {"id": device_id, "ok": False, "eligible": False, "error": "unknown_target"}
                for device_id in missing
            ],
            "message": (
                "No selected target exists in source of truth."
                if requested_ids
                else "No devices in source of truth yet. Discover a device first."
            ),
        }
    registry = AdapterRegistry()
    supported = []
    excluded_rows: list[dict[str, object]] = []
    for device in devices:
        normalized_platform = registry.rez.normalize_platform(device.platform)
        if normalized_platform not in READ_TRANSPORTS:
            excluded_rows.append({
                "id": device.id,
                "host": device.host,
                "platform": device.platform,
                "site": device.site,
                "ok": False,
                "eligible": False,
                "error": f"unsupported_platform:{device.platform}",
            })
        else:
            supported.append(device)
    collected = registry.rez.collect_many(supported) if supported else {"results": []}
    results = {str(item.get("device_id")): item for item in collected.get("results", []) if isinstance(item, dict)}
    rows: list[dict[str, object]] = []
    readable = 0
    for device in supported:
        result = results.get(device.id) or {}
        ok = bool(result.get("ok"))
        readable += 1 if ok else 0
        error = ""
        if not ok:
            errors = result.get("errors") or []
            error = str(result.get("error") or (errors[0] if errors else "unreadable"))
        rows.append({
            "id": device.id,
            "host": device.host,
            "platform": device.platform,
            "site": device.site,
            "ok": ok,
            "eligible": True,
            "error": error,
        })
    rows.extend(excluded_rows)
    rows.extend(
        {"id": device_id, "ok": False, "eligible": False, "error": "unknown_target"}
        for device_id in missing
    )
    tested = len(supported)
    return {
        "ok": tested > 0 and readable == tested and not missing and not excluded_rows,
        "tested": tested,
        "readable": readable,
        "devices": rows,
        "excluded": len(excluded_rows) + len(missing),
        "requested": len(requested_ids),
        "message": f"{readable}/{tested} selected supported devices are readable.",
    }


@app.post("/api/discovery/scan")
def api_discovery_scan(request: DiscoveryScanRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    principal = _request_principal(http_request)
    if execution_mode() == "runner":
        _reject_cloud_credentials_in_runner_mode(request.username, request.password)
        payload = {"host": request.host,
                   "platform": request.platform, "port": request.port, "device_id": request.device_id,
                   "site": request.site, "groups": request.groups}
        discovery_result = _runner_read(p, "discovery", payload, principal.org_id)
        return _import_runner_discovery_candidate(p, discovery_result, principal.org_id)
    inventory = Inventory(configured_inventory_path(p))
    identifier = request.device_id or request.host
    enforce_capacity(
        "devices",
        current=len(inventory.devices),
        additional=0 if inventory.find_device(identifier) else 1,
        org_id=principal.org_id,
    )
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
def api_source_of_truth_import_device(
    request: SourceOfTruthDeviceImportRequest,
    http_request: Request,
) -> dict[str, object]:
    candidate_id = str(request.candidate.get("id") or request.candidate.get("hostname") or "")
    inventory = Inventory(configured_inventory_path(paths()))
    enforce_capacity(
        "devices",
        current=len(inventory.devices),
        additional=0 if inventory.find_device(candidate_id) else 1,
        org_id=_request_principal(http_request).org_id,
    )
    return DiscoveryService(paths()).import_candidate(request.candidate)


@app.post("/api/shell/devices/manual")
def api_shell_manual_device(request: ShellManualDeviceRequest, http_request: Request) -> dict[str, object]:
    """Add or update a device from the Shell.

    Source of truth receives only non-secret device facts. In runner mode, the
    credentialed runner inventory is updated by an outbound runner job so the
    next shell session can connect without the browser ever handling SSH.
    """
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    principal = _request_principal(http_request)
    enforce_capacity(
        "devices",
        current=len(inventory.devices),
        additional=0 if inventory.find_device(request.device_id) else 1,
        org_id=principal.org_id,
    )
    _reject_cloud_credentials_in_runner_mode(request.username, request.password)
    groups = request.groups or ["manual"]
    public_candidate = {
        "id": request.device_id,
        "hostname": request.hostname or request.device_id,
        "host": request.host,
        "platform": request.platform,
        "site": request.site or "manual",
        "groups": groups,
        "port": request.port,
    }
    if execution_mode() == "runner":
        _enforce_catalog_growth(
            PlatformStore(p),
            principal.org_id,
            [request.device_id],
        )
    source_result = DiscoveryService(p).import_candidate(public_candidate)
    if not source_result.get("ok"):
        return {"ok": False, "source_of_truth": source_result, "message": source_result.get("error") or "Source of truth update failed."}

    runner_result: dict[str, object] | None = None
    catalog_result: dict[str, object] | None = None
    if execution_mode() == "runner":
        runner_candidate = dict(public_candidate)
        runner_result = _runner_read(
            p,
            "manual_device_add",
            {"candidate": runner_candidate},
            _request_principal(http_request).org_id,
            timeout=30,
        )
        if not runner_result.get("ok"):
            return {
                "ok": False,
                "source_of_truth": source_result,
                "runner_inventory": runner_result,
                "message": runner_result.get("error") or runner_result.get("message") or "Runner inventory update failed.",
            }
        catalog_result = _catalog_runner_candidate(
            p,
            _request_principal(http_request).org_id,
            dict(source_result.get("device") or public_candidate),
            runner_result,
        )
        if not catalog_result.get("ok"):
            return {
                "ok": False,
                "source_of_truth": source_result,
                "runner_inventory": runner_result,
                "device_catalog": catalog_result,
                "message": catalog_result.get("error") or "Device catalog assignment failed.",
            }

    return {
        "ok": True,
        "source_of_truth": source_result,
        "runner_inventory": runner_result,
        "device_catalog": catalog_result,
        "device": source_result.get("device"),
        "message": f"Device {public_candidate['id']} is ready for Shell sessions.",
    }


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
    device = inventory.find_device(device_id)
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
    device = inventory.find_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().rez.collect_device_state(device)


@app.post("/api/adapters/rez/collect-state")
def api_rez_collect_state(request: DeviceRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device_id = request.device_id
    device = inventory.find_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return AdapterRegistry().rez.collect_device_state(device)


@app.post("/api/troubleshoot/run")
def api_troubleshoot_run(request: TroubleshootRequest, http_request: Request) -> dict[str, object]:
    """Read-only investigation: collect live state with Rez, summarize it, and optionally attach it to a change."""
    p = paths()
    org = _request_principal(http_request).org_id
    if execution_mode() == "runner":
        result = _runner_read(
            p,
            "troubleshoot",
            {
                "device_id": request.device_id,
                "check": request.check,
                "target": request.target,
                "expected": request.expected,
            },
            org,
            timeout=TROUBLESHOOT_READ_TIMEOUT_SECONDS,
        )
    else:
        inventory = Inventory(configured_inventory_path(p))
        device = inventory.find_device(request.device_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"Unknown device {request.device_id}")
        state = _collect_rez_state_for_troubleshooting(device)
        result = troubleshoot_state(
            state,
            check=request.check,
            target=request.target,
            expected=request.expected,
        )

    result.setdefault("change_event_recorded", False)
    if request.change_id:
        store = PlatformStore(p)
        try:
            change = store.get_change(request.change_id)
            if change.org_id != org:
                raise KeyError(request.change_id)
            event = store.record_workflow_event(
                change.id,
                "troubleshoot",
                change.workflow_state,
                change.workflow_state,
                str(result.get("message") or "Read-only investigation completed."),
                {
                    "device_id": request.device_id,
                    "check": request.check,
                    "target": request.target,
                    "expected": request.expected,
                    "status": result.get("status"),
                    "summary": result.get("summary"),
                    "read_path": result.get("read_path"),
                    "device_config": result.get("device_config"),
                    "evidence_rows": result.get("evidence_rows", [])[:10],
                },
            )
            result["change_event_recorded"] = True
            result["event_id"] = event.id
        except Exception as exc:  # noqa: BLE001
            result["change_event_recorded"] = False
            result["change_event_error"] = str(exc)
    return result


@app.post("/api/diagnostics/verification-handoff")
def api_diagnostics_verification_handoff(request: VerificationHandoffRequest) -> dict[str, object]:
    """Build a read-only Rez Diagnostics handoff from a failed Netcode verification."""
    return build_verification_handoff(
        device_id=request.device_id,
        check=request.check,
        expected=request.expected,
        actual=request.actual,
        verification=dict(request.verification or {}),
        change_id=request.change_id,
        intent_path=request.intent_path,
    )


# ---- Netcode Shell (governed SSH, MVP1/2) -----------------------------------
# Live transport state stays in memory, while session metadata and the complete
# transcript are durable. The GUARD runs on the runner (trust boundary); the
# control plane only brokers and records.
_SHELL_SESSIONS: dict[str, dict[str, object]] = {}
_SHELL_BACKFILLED_WORKSPACES: set[tuple[str, str]] = set()


@app.get("/api/shell/desktop/profile")
def api_shell_desktop_profile(request: Request) -> dict[str, object]:
    return build_desktop_shell_profile(str(request.base_url).rstrip("/"), runner_pool=runner_pool())


@app.get("/api/runner/download/windows/manifest")
def api_windows_runner_manifest(request: Request) -> dict[str, object]:
    base_url = _public_base_url(request)
    enabled, _artifact_path = _signed_windows_artifact()
    return {
        "ok": True,
        "product": "Rezonance Local Connector",
        "version": "0.3.3-community-preview",
        "platform": "windows-x64",
        "artifact_kind": "compiled_windows_connector",
        "package_kind": "signed_windows_package",
        "control_plane_url": base_url,
        "runner_pool": runner_pool(),
        "network": "outbound_https_wss_only",
        "credentials": "local_connector_dpapi_machine_scope",
        "install_mode": "windows_service_or_system_scheduled_task",
        "python_required": False,
        "source_bundle_included": False,
        "build_scripts_included": False,
        "sample_workflows_included": False,
        "unsigned_download_public": False,
        "production_code_signing_complete": enabled,
        "available": enabled,
        "download_status": "available" if enabled else "blocked_pending_signed_package",
    }


@app.get("/api/runner/download/windows")
def api_windows_runner_download(request: Request) -> Response:
    enabled, artifact_path = _signed_windows_artifact()
    if not enabled or artifact_path is None:
        return JSONResponse(
            status_code=401,
            content={
                "ok": False,
                "error": "windows_download_not_available",
                "message": "Windows Local Connector downloads are blocked until a signed package is configured.",
            },
        )
    package = artifact_path.read_bytes()
    return Response(
        content=package,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{artifact_path.name}"'},
    )


def _public_base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    proto = (forwarded_proto or request.url.scheme or "https").split(",", 1)[0].strip()
    host = (forwarded_host or request.headers.get("host") or request.url.netloc).split(",", 1)[0].strip()
    if proto == "http" and host.endswith(".rezonancenetworks.com"):
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


def _signed_windows_artifact() -> tuple[bool, Path | None]:
    enabled = os.environ.get("NETCODE_WINDOWS_DOWNLOAD_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    artifact = os.environ.get("NETCODE_WINDOWS_SIGNED_ARTIFACT_PATH", "").strip()
    if not enabled or not artifact:
        return False, None
    path = Path(artifact)
    if not path.is_file():
        return False, None
    return True, path


def _shell_transcript_path(p, session_id: str) -> Path:
    return p.reports / f"shell-{session_id}.jsonl"


def _shell_append(p, session_id: str, entry: dict[str, object]) -> None:
    path = _shell_transcript_path(p, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(entry)
    payload.setdefault("timestamp", utc_now())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def _record_shell_output(session_id: str, encoded: str) -> None:
    """Persist one terminal-output frame without changing its byte content."""
    if not encoded:
        return
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:  # noqa: BLE001 — malformed frames must not break the broker
        return
    p = paths()
    _shell_append(p, session_id, {"event": "output", "data_b64": encoded})
    PlatformStore(p).update_shell_session(session_id, output_bytes_delta=len(raw))


def _backfill_shell_sessions(p, org_id: str) -> None:
    """Index legacy JSONL transcripts so pre-index sessions remain discoverable."""
    migration_key = (str(p.database), org_id)
    if migration_key in _SHELL_BACKFILLED_WORKSPACES:
        return
    store = PlatformStore(p)
    for path in p.reports.glob("shell-*.jsonl"):
        session_id = path.stem.removeprefix("shell-")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", session_id):
            continue
        if store.get_shell_session(session_id):
            continue
        try:
            entries = [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception:  # noqa: BLE001 — one corrupt legacy artifact must not hide history
            continue
        opened = next((item for item in entries if item.get("event") == "session_opened"), None)
        if not isinstance(opened, dict) or str(opened.get("org_id") or DEFAULT_ORG_ID) != org_id:
            continue
        device_id = str(opened.get("device_id") or "unknown")
        started = str(opened.get("timestamp") or "")
        if not started:
            started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
        change_id = ""
        output_bytes = 0
        for item in entries:
            candidate = str(item.get("change_id") or "").strip()
            if candidate:
                change_id = candidate
            encoded = str(item.get("data_b64") or "")
            if encoded:
                try:
                    output_bytes += len(base64.b64decode(encoded, validate=True))
                except Exception:  # noqa: BLE001
                    pass
        store.create_shell_session(
            session_id=session_id,
            org_id=org_id,
            device_id=device_id,
            display_id=str(opened.get("display_id") or device_id),
            platform=str(opened.get("platform") or "unknown"),
            runner_id=str(opened.get("runner_id") or ""),
            runner_pool=str(opened.get("runner_pool") or ""),
            status="archived",
            guard_enabled=bool(opened.get("guard_enabled")),
            change_id=change_id,
            started_at=started,
            last_activity=started,
            ended_at=started,
            transcript_path=str(path),
            command_count=sum(1 for item in entries if item.get("event") == "command"),
            output_bytes=output_bytes,
            device_touched=any(
                item.get("device_touched") is True
                or str(item.get("kind") or "").startswith("config")
                for item in entries
            ),
        )
    _SHELL_BACKFILLED_WORKSPACES.add(migration_key)


def _record_shell_command(session_id: str, ev: dict[str, object]) -> None:
    """Fold a command run in a governed shell session into its change record so
    the change report shows exactly what was done on the device, and when."""
    change_id = str(ev.get("change_id") or "").strip()
    line = str(ev.get("line") or "").strip()
    if not line:
        return
    session = _SHELL_SESSIONS.get(session_id) or {}
    device_id = str(session.get("device_id") or "?")
    kind = str(ev.get("kind") or "")
    p = paths()
    if change_id:
        try:
            store = PlatformStore(p)
            change = store.get_change(change_id)
            current = change.workflow_state
            store.record_workflow_event(
                change_id, action="shell_command", from_state=current, to_state=current,
                message=f"[shell · {device_id}] {line}",
                evidence={"source": "shell", "device_id": device_id, "command": line,
                          "kind": kind, "session_id": session_id},
            )
        except Exception:  # noqa: BLE001 — reporting must never break the live stream
            pass
    _shell_append(p, session_id, {"event": "command", "device_id": device_id,
                                  "command": line, "kind": kind, "change_id": change_id})
    PlatformStore(p).update_shell_session(
        session_id,
        change_id=change_id or None,
        command_delta=1,
        device_touched=bool((session.get("state") or {}).get("device_touched")),
    )


@app.post("/api/shell/open")
def api_shell_open(request: ShellOpenRequest, http_request: Request) -> dict[str, object]:
    """Open a CLI session against a device."""
    p = paths()
    org = _request_principal(http_request).org_id
    get_entitlements(org_id=org)
    if execution_mode() == "runner":
        catalog_device = PlatformStore(p).resolve_device(org, request.device_id)
        if not catalog_device:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown device {request.device_id}. Wait for the local connector to synchronize discovery.",
            )
        runner_id = str(catalog_device.get("runner_id") or "")
        if runner_id not in _RUNNER_CHANNELS:
            raise HTTPException(
                status_code=409,
                detail=f"Local connector for {catalog_device['id']} is offline. Start it and retry.",
            )
        assigned_runner = PlatformStore(p).get_runner(runner_id)
        if assigned_runner.drain_requested:
            raise HTTPException(
                status_code=409,
                detail=f"Local connector for {catalog_device['id']} is in maintenance drain.",
            )
        device_id = str(catalog_device["canonical_id"])
        display_id = str(catalog_device["id"])
        platform = str(catalog_device["platform"])
        assigned_pool = str(catalog_device["runner_pool"])
    else:
        inventory = Inventory(configured_inventory_path(p))
        device = inventory.find_device(request.device_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"Unknown device {request.device_id}")
        device_id = device.id
        display_id = device.id
        platform = device.platform
        runner_id = ""
        assigned_pool = runner_pool()
    session_id = uuid.uuid4().hex[:16]
    mode = "guarded" if request.guard_enabled else "direct"
    state = {
        "mode": mode,
        "change_id": None,
        "in_config": False,
        "device_touched": False,
        "guard_enabled": bool(request.guard_enabled),
    }
    _SHELL_SESSIONS[session_id] = {
        "org_id": org,
        "device_id": device_id,
        "display_id": display_id,
        "platform": platform,
        "runner_id": runner_id,
        "runner_pool": assigned_pool,
        "state": state,
    }
    transcript_path = _shell_transcript_path(p, session_id)
    PlatformStore(p).create_shell_session(
        session_id=session_id,
        org_id=org,
        device_id=device_id,
        display_id=display_id,
        platform=platform,
        runner_id=runner_id,
        runner_pool=assigned_pool,
        status="opened",
        guard_enabled=bool(request.guard_enabled),
        transcript_path=str(transcript_path),
    )
    _shell_append(p, session_id, {"event": "session_opened", "device_id": device_id,
                                  "display_id": display_id, "runner_id": runner_id, "org_id": org,
                                  "runner_pool": assigned_pool, "platform": platform,
                                  "guard_enabled": bool(request.guard_enabled)})
    if request.guard_enabled:
        message = f"Live shell open on {display_id}. Safety prompts remain enabled; transcript logging is on."
    else:
        message = f"Full live shell open on {display_id}. Commands run on the device; transcript logging remains on."
    return {"ok": True, "session_id": session_id, "device_id": device_id, "display_id": display_id,
            "platform": platform, "runner_id": runner_id, "state": state, "message": message}


def _shell_session_or_404(session_id: str, org: str) -> dict[str, object]:
    session = _SHELL_SESSIONS.get(session_id)
    if not session or session.get("org_id") != org:
        raise HTTPException(status_code=404, detail=f"Unknown shell session {session_id}")
    return session


@app.post("/api/shell/attach")
def api_shell_attach(request: ShellAttachRequest, http_request: Request) -> dict[str, object]:
    """Attach a change record as optional audit metadata for this live session."""
    p = paths()
    org = _request_principal(http_request).org_id
    session = _shell_session_or_404(request.session_id, org)
    try:
        change = PlatformStore(p).get_change(request.change_id)
        if change.org_id != org:
            raise KeyError(request.change_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Unknown change {request.change_id}")
    state = dict(session["state"])  # type: ignore[arg-type]
    state["mode"] = "change_attached"
    state["change_id"] = request.change_id
    session["state"] = state
    _shell_append(p, request.session_id, {"event": "change_attached", "change_id": request.change_id})
    PlatformStore(p).update_shell_session(request.session_id, change_id=request.change_id)
    return {"ok": True, "session_id": request.session_id, "state": state,
            "message": f"Change {request.change_id} attached as session metadata. The shell remains live."}


@app.post("/api/shell/quick-change")
def api_shell_quick_change(request: ShellQuickChangeRequest, http_request: Request) -> dict[str, object]:
    """Create a lightweight change record for ad-hoc shell config work, so the
    engineer can attach and configure without leaving the terminal. Everything
    typed under it is captured into this change's report."""
    p = paths()
    principal = _request_principal(http_request)
    org = principal.org_id
    session = _shell_session_or_404(request.session_id, org)
    device_id = str(session.get("device_id") or "")
    title = request.title.strip() or f"Shell change on {device_id}"
    intent_dir = p.intents / "shell"
    intent_dir.mkdir(parents=True, exist_ok=True)
    intent_path = intent_dir / f"{request.session_id}.yaml"
    intent_path.write_text(
        "change_type: shell_session\n"
        f"description: {json.dumps(title)}\n"
        f"device: {json.dumps(device_id)}\n"
        f"ticket: {json.dumps(request.ticket.strip())}\n"
        "commands: []  # captured live from the governed shell session\n",
        encoding="utf-8",
    )
    change = PlatformStore(p).create_change(
        intent_path, device_id, requested_by=principal.email or "netcode-user",
        org_id=org, created_by_user_id=getattr(principal, "user_id", None))
    return {"ok": True, "change_id": change.id, "title": title, "device_id": device_id,
            "message": f"Change {change.id[:8]} created for this session."}


@app.post("/api/shell/input")
def api_shell_input(request: ShellInputRequest, http_request: Request) -> dict[str, object]:
    """Submit a line (or paste) to the live shell session. Any optional guard
    runs on the runner; unattended automation gates live outside this path."""
    p = paths()
    org = _request_principal(http_request).org_id
    session = _shell_session_or_404(request.session_id, org)
    payload = {
        "device_id": session["device_id"],
        "session_id": request.session_id,
        "input": request.input,
        "state": session["state"],
    }
    if execution_mode() == "runner":
        result = _runner_read(p, "shell", payload, org)
    else:
        result = {"ok": False, "cleared": False, "output": "",
                  "events": [{"type": "guard", "action": "no_runner",
                              "message": "Governed shell executes on the on-prem runner; none is online for this pool."}],
                  "state": session["state"], "device_touched": bool((session["state"] or {}).get("device_touched"))}
    if isinstance(result.get("state"), dict):
        session["state"] = result["state"]
    _shell_append(p, request.session_id, {"event": "input", "input": request.input,
                                          "cleared": result.get("cleared"), "executed": result.get("executed"),
                                          "guard": [e.get("action") for e in result.get("events", [])],
                                          "output_len": len(str(result.get("output") or ""))})
    output = str(result.get("output") or "")
    if output:
        _record_shell_output(request.session_id, base64.b64encode(output.encode("utf-8")).decode("ascii"))
    PlatformStore(p).update_shell_session(
        request.session_id,
        status="active",
        device_touched=bool((session.get("state") or {}).get("device_touched")),
    )
    return result


# ---- Interactive streaming shell (MVP4) -------------------------------------
# Transport (Teleport/HCP-agent pattern, since the runner is outbound-only and
# the devices are reachable only from it):
#   browser xterm  <--WS-->  control plane  <--persistent WS-->  runner  <-->  device PTY
# The control plane is a pure broker: it holds one control-channel WS per runner
# and one browser WS per session, and pipes JSON frames between them. The
# guard and the device credentials live on the runner; the control plane never
# sees a live device session or a credential.
_RUNNER_CHANNELS: dict[str, WebSocket] = {}          # runner_id -> runner control WS
_RUNNER_CHANNEL_POOLS: dict[str, str] = {}           # runner_id -> pool
_BROWSER_SOCKETS: dict[str, WebSocket] = {}          # session_id -> browser WS


async def _terminate_shells_for_runner(runner_id: str, reason: str) -> None:
    store = PlatformStore(paths())
    sessions = store.terminate_active_shell_sessions(reason=reason, runner_id=runner_id)
    for stored in sessions:
        sid = str(stored["id"])
        _SHELL_SESSIONS.pop(sid, None)
        _shell_append(paths(), sid, {"event": "session_terminated", "reason": reason, "runner_id": runner_id})
        browser = _BROWSER_SOCKETS.pop(sid, None)
        if browser is not None:
            try:
                await browser.send_json({"t": "status", "s": "terminated", "m": reason})
                await browser.close(code=1012)
            except Exception:  # noqa: BLE001 - browser may already be gone.
                pass


@app.websocket("/api/runner/stream")
async def ws_runner_stream(ws: WebSocket) -> None:
    """Persistent outbound control channel from a runner. The runner authenticates,
    then this coroutine forwards the runner's output/event frames to the matching
    browser sockets. Input frames flow the other way from the browser coroutine."""
    await ws.accept()
    runner_id = None
    try:
        auth = await ws.receive_json()
        token = str(auth.get("token", ""))
        store = PlatformStore(paths())
        runner = authenticate_runner(store, token)
        if runner is None:
            await ws.close(code=4401)
            return
        runner_id = runner.id
        previous = _RUNNER_CHANNELS.pop(runner.id, None)
        _RUNNER_CHANNEL_POOLS.pop(runner.id, None)
        if previous is not None and previous is not ws:
            await _terminate_shells_for_runner(runner.id, "connector_channel_replaced")
            try:
                await previous.close(code=1012)
            except Exception:  # noqa: BLE001
                pass
        _RUNNER_CHANNELS[runner.id] = ws
        _RUNNER_CHANNEL_POOLS[runner.id] = runner.pool
        await ws.send_json({"t": "ready", "runner_id": runner.id, "pool": runner.pool})
        while True:
            frame = await ws.receive_json()
            sid = str(frame.get("sid", ""))
            if frame.get("t") == "out":
                _record_shell_output(sid, str(frame.get("d") or ""))
            elif frame.get("t") == "status":
                shell_status = str(frame.get("s") or "active")
                PlatformStore(paths()).update_shell_session(sid, status=shell_status)
            if frame.get("t") == "event":
                ev = frame.get("e") or {}
                if isinstance(ev, dict):
                    session = _SHELL_SESSIONS.get(sid)
                    if session:
                        state = dict(session.get("state") or {})
                        if "in_config" in ev:
                            state["in_config"] = bool(ev.get("in_config"))
                        elif ev.get("action") == "config_mode_entered":
                            state["in_config"] = True
                        elif ev.get("action") == "config_mode_exited":
                            state["in_config"] = False
                        if "device_touched" in ev:
                            state["device_touched"] = bool(ev.get("device_touched"))
                        elif ev.get("action") == "config_mode_entered":
                            state["device_touched"] = True
                        session["state"] = state
                        PlatformStore(paths()).update_shell_session(
                            sid, device_touched=bool(state.get("device_touched"))
                        )
                    if ev.get("type") == "command":
                        _record_shell_command(sid, ev)
            browser = _BROWSER_SOCKETS.get(sid)
            if browser is not None:
                try:
                    await browser.send_json(frame)
                except Exception:  # noqa: BLE001
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — auth failure or malformed frame ends the channel
        pass
    finally:
        if runner_id and _RUNNER_CHANNELS.get(runner_id) is ws:
            _RUNNER_CHANNELS.pop(runner_id, None)
            _RUNNER_CHANNEL_POOLS.pop(runner_id, None)
            await _terminate_shells_for_runner(runner_id, "connector_disconnected")


def _runner_channel_for_session(session: dict[str, object]) -> tuple[str, WebSocket | None]:
    assigned = str(session.get("runner_id") or "")
    if assigned:
        return assigned, _RUNNER_CHANNELS.get(assigned)
    pool = str(session.get("runner_pool") or runner_pool())
    for runner_id, channel in _RUNNER_CHANNELS.items():
        if _RUNNER_CHANNEL_POOLS.get(runner_id) == pool:
            return runner_id, channel
    return "", None


def _shell_entitlement_recheck_seconds() -> int:
    try:
        configured = int(os.environ.get("NETCODE_SHELL_ENTITLEMENT_RECHECK_SECONDS", "60"))
    except ValueError:
        configured = 60
    return max(5, min(configured, 300))


async def _shell_entitlement_watchdog(
    ws: WebSocket,
    org_id: str,
    *,
    interval_seconds: float | None = None,
) -> None:
    """Close an active PTY when its organization loses platform entitlement."""
    interval = (
        float(_shell_entitlement_recheck_seconds())
        if interval_seconds is None
        else max(0.0, float(interval_seconds))
    )
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(get_entitlements, org_id=org_id, force=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an active privileged session fails closed.
            try:
                await ws.send_json({
                    "t": "status",
                    "s": "license_suspended",
                    "m": "This Shell session ended because the organization license is not active.",
                })
                await ws.close(code=4403)
            except Exception:  # noqa: BLE001 - socket may already be closing.
                pass
            return


@app.websocket("/api/shell/session/{session_id}")
async def ws_shell_session(ws: WebSocket, session_id: str) -> None:
    """Browser terminal socket for one governed interactive session. Opens the
    device PTY on the runner and pipes keystrokes down / device bytes up."""
    await ws.accept()
    session = _SHELL_SESSIONS.get(session_id)
    if not session:
        await ws.send_json({"t": "status", "s": "error", "m": "Unknown or expired session."})
        await ws.close()
        return
    principal = _websocket_principal(ws)
    if not principal.authenticated:
        await ws.send_json({"t": "status", "s": "error", "m": "Authentication required."})
        await ws.close(code=4401)
        return
    if principal.org_id != session.get("org_id"):
        await ws.send_json({"t": "status", "s": "error", "m": "Unknown or expired session."})
        await ws.close(code=4404)
        return
    try:
        get_entitlements(org_id=principal.org_id)
    except EntitlementError as exc:
        await ws.send_json({"t": "status", "s": "error", "m": str(exc)})
        await ws.close(code=4403)
        return
    runner_id, runner_ws = _runner_channel_for_session(session)
    if runner_ws is None:
        await ws.send_json({"t": "status", "s": "error", "m": "The assigned local connector is offline."})
        await ws.close()
        return
    _BROWSER_SOCKETS[session_id] = ws
    state = session.get("state") or {}
    entitlement_watchdog: asyncio.Task | None = None
    try:
        await runner_ws.send_json({"t": "open", "sid": session_id,
                                   "device_id": session["device_id"], "state": state})
        _shell_append(paths(), session_id, {"event": "interactive_opened", "device_id": session["device_id"]})
        PlatformStore(paths()).update_shell_session(session_id, status="active")
        entitlement_watchdog = asyncio.create_task(
            _shell_entitlement_watchdog(ws, principal.org_id)
        )
        while True:
            frame = await ws.receive_json()
            kind = frame.get("t")
            if kind == "in":
                await runner_ws.send_json({"t": "in", "sid": session_id, "d": frame.get("d", "")})
            elif kind == "resize":
                await runner_ws.send_json({"t": "resize", "sid": session_id,
                                           "cols": frame.get("cols", 120), "rows": frame.get("rows", 40)})
            elif kind == "attach":
                change_id = str(frame.get("change_id", "")).strip()
                org = session.get("org_id")
                try:
                    change = PlatformStore(paths()).get_change(change_id)
                    if change.org_id != org:
                        raise KeyError(change_id)
                except Exception:  # noqa: BLE001
                    await ws.send_json({"t": "status", "s": "attach_error",
                                        "m": f"Unknown change {change_id}."})
                    continue
                new_state = dict(session.get("state") or {})
                new_state["mode"] = "change_attached"
                new_state["change_id"] = change_id
                session["state"] = new_state
                await runner_ws.send_json({"t": "attach", "sid": session_id, "change_id": change_id})
                _shell_append(paths(), session_id, {"event": "change_attached", "change_id": change_id})
                PlatformStore(paths()).update_shell_session(session_id, change_id=change_id)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        if entitlement_watchdog is not None:
            entitlement_watchdog.cancel()
            await asyncio.gather(entitlement_watchdog, return_exceptions=True)
        _BROWSER_SOCKETS.pop(session_id, None)
        current = _RUNNER_CHANNELS.get(runner_id)
        if current is not None:
            try:
                await current.send_json({"t": "close", "sid": session_id})
            except Exception:  # noqa: BLE001
                pass
        final_state = dict(session.get("state") or {})
        shell_store = PlatformStore(paths())
        stored = shell_store.get_shell_session(session_id)
        if stored and stored.get("status") == "terminated":
            _shell_append(paths(), session_id, {
                "event": "session_transport_closed",
                "reason": stored.get("end_reason") or "connector_terminated",
                "device_touched": bool(final_state.get("device_touched")),
            })
        else:
            _shell_append(paths(), session_id, {
                "event": "session_closed",
                "device_touched": bool(final_state.get("device_touched")),
            })
            shell_store.update_shell_session(
                session_id,
                status="closed",
                device_touched=bool(final_state.get("device_touched")),
                ended=True,
                end_reason="browser_disconnected",
            )


@app.get("/api/shell/sessions")
def api_shell_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    device_id: str = Query(default="", max_length=255),
    before: str = Query(default="", max_length=100),
) -> dict[str, object]:
    """List durable Shell sessions owned by the caller's organization."""
    p = paths()
    org = _request_principal(request).org_id
    _backfill_shell_sessions(p, org)
    page = PlatformStore(p).list_shell_sessions(
        org, limit=limit + 1, device_id=device_id, before=before
    )
    sessions = page[:limit]
    next_before = None
    if len(page) > limit and sessions:
        last = sessions[-1]
        next_before = f"{last['last_activity']}|{last['id']}"
    return {
        "ok": True,
        "sessions": sessions,
        "returned": len(sessions),
        "next_before": next_before,
    }


@app.get("/api/shell/{session_id}/transcript")
def api_shell_transcript(session_id: str, request: Request) -> dict[str, object]:
    """The durable session transcript — the shell's evidence artifact."""
    p = paths()
    org = _request_principal(request).org_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", session_id):
        raise HTTPException(status_code=404, detail=f"Unknown shell session {session_id}")
    _backfill_shell_sessions(p, org)
    stored = PlatformStore(p).get_shell_session(session_id)
    if not stored or stored.get("org_id") != org:
        raise HTTPException(status_code=404, detail=f"Unknown shell session {session_id}")
    path = _shell_transcript_path(p, session_id)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entries = [json.loads(line) for line in lines if line.strip()]
    for entry in entries:
        encoded = str(entry.get("data_b64") or "")
        if entry.get("event") == "output" and encoded:
            try:
                entry["output"] = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                entry["output"] = "[unreadable terminal output]"
    state = _SHELL_SESSIONS.get(session_id, {}).get("state") or {}
    touched = bool(state.get("device_touched")) if state else bool(stored.get("device_touched"))
    return {"ok": True, "session_id": session_id, "entries": entries,
            "session": stored, "device_touched": touched,
            "artifact": str(path), "device_config": "touched" if touched else "not_touched"}


@app.post("/api/verify/vlan")
def api_verify_vlan(request: VlanVerifyRequest) -> dict[str, object]:
    p = paths()
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.find_device(request.device_id)
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
    device = inventory.find_device(request.device_id)
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


def _persist_intent_verification(
    store: PlatformStore,
    *,
    change_id: str | None,
    verification: dict[str, object],
    passed: bool,
    actor: str = "system",
) -> dict[str, object]:
    if not change_id:
        return {"ok": True, "linked_revisions": []}
    try:
        change = store.get_change(change_id)
    except Exception:
        return {"ok": True, "linked_revisions": []}
    prior_state = change.workflow_state
    prior_status = change.status
    result = dict(change.result or {})
    result["verify_proof"] = verification
    next_state = "verified" if passed else change.workflow_state
    store.update_change(
        change.id,
        "completed" if passed else change.status,
        result,
        workflow_state=next_state,
    )
    store.record_workflow_event(
        change.id,
        "verify",
        change.workflow_state,
        next_state,
        str(verification.get("message") or ("Post-change verification passed." if passed else "Post-change verification failed.")),
        verification,
    )
    if not passed:
        return {"ok": True, "linked_revisions": []}

    try:
        activations = activate_change_candidates(
            NetworkModelRepository(store),
            store,
            org_id=change.org_id,
            change_id=change.id,
            actor=actor,
            git_root=store.paths.git_workspace,
        )
    except (KeyError, NetworkModelError, ValueError) as exc:
        failed_result = dict(result)
        failed_result["network_model_activation"] = {
            "ok": False,
            "error": str(exc),
        }
        store.update_change(
            change.id,
            prior_status,
            failed_result,
            workflow_state=prior_state,
        )
        store.record_workflow_event(
            change.id,
            "network_model_activation",
            "verified",
            prior_state,
            "Live verification passed, but the Network Model checkpoint failed; the change remains unclosed.",
            {"error": str(exc)},
        )
        return {"ok": False, "error": str(exc), "linked_revisions": []}

    summaries = [
        {
            "environment_id": item["revision"]["environment_id"],
            "revision_id": item["revision"]["revision_id"],
            "status": item["revision"]["status"],
            "pending_change_ids": list(item.get("pending_change_ids") or []),
        }
        for item in activations
    ]
    if summaries:
        pending = any(item["pending_change_ids"] for item in summaries)
        completed_result = dict(store.get_change(change.id).result or {})
        completed_result["network_model_activation"] = {
            "ok": True,
            "revisions": summaries,
            "pending": pending,
        }
        store.update_change(
            change.id,
            "completed",
            completed_result,
            workflow_state="verified",
        )
        store.record_workflow_event(
            change.id,
            "network_model_pending" if pending else "network_model_activation",
            "verified",
            "verified",
            (
                "Live verification passed; the shared model candidate is waiting for the remaining fleet targets."
                if pending
                else "Verified intended state promoted to the active Network Model."
            ),
            {"revisions": summaries},
        )
    return {"ok": True, "linked_revisions": summaries}


@app.post("/api/verify/intent")
def api_verify_intent(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    if execution_mode() == "runner":
        intent_yaml = Path(request.intent_path).read_text(encoding="utf-8")
        payload = {
            "intent_yaml": intent_yaml,
            "device_id": request.device_id,
            "present": True,
            "_progress_change_id": request.change_id,
        }
        result = _runner_read(p, "verify", payload, _request_principal(http_request).org_id)
        verification = dict(result.get("verification") or result)
        verification.setdefault("ok", result.get("ok"))
        model_lifecycle = _persist_intent_verification(
            PlatformStore(p),
            change_id=request.change_id,
            verification=verification,
            passed=bool(result.get("ok")),
            actor=(
                _request_principal(http_request).email
                or _request_principal(http_request).user_id
                or "system"
            ),
        )
        result["network_model"] = model_lifecycle
        if not model_lifecycle.get("ok"):
            result["ok"] = False
            result["message"] = (
                "Live verification passed, but the Network Model checkpoint failed: "
                + str(model_lifecycle.get("error") or "unknown error")
            )
        handoff = attach_verification_handoff(
            PlatformStore(p),
            change_id=request.change_id,
            device_id=str(result.get("device_id") or request.device_id or ""),
            check="post_change_verify",
            verification=verification,
            actual=str(result.get("message") or result.get("error") or verification.get("message") or ""),
            intent_path=request.intent_path,
        )
        if handoff:
            result["diagnostics_handoff"] = handoff
        return result
    intent = load_intent(Path(request.intent_path))
    inventory = Inventory(configured_inventory_path(p))
    device_id = request.device_id or (intent.targets.device_ids[0] if intent.targets.device_ids else "")
    device = inventory.find_device(device_id)
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
    response = {
        "ok": verification.status == "pass",
        "device_id": device.id,
        "platform": device.platform,
        "change_type": intent.change_type,
        "verification": verification.__dict__,
    }
    model_lifecycle = _persist_intent_verification(
        PlatformStore(p),
        change_id=request.change_id,
        verification={"ok": verification.status == "pass", **verification.__dict__},
        passed=verification.status == "pass",
        actor=(
            _request_principal(http_request).email
            or _request_principal(http_request).user_id
            or "system"
        ),
    )
    response["network_model"] = model_lifecycle
    if not model_lifecycle.get("ok"):
        response["ok"] = False
    handoff = attach_verification_handoff(
        PlatformStore(p),
        change_id=request.change_id,
        device_id=device.id,
        check="post_change_verify",
        verification={"ok": verification.status == "pass", **verification.__dict__},
        actual=verification.message,
        intent_path=request.intent_path,
    )
    if handoff:
        response["diagnostics_handoff"] = handoff
    return response


@app.post("/api/drift/vlan")
def api_vlan_drift(request: IntentPathRequest, http_request: Request) -> dict[str, object]:
    p = paths()
    device_id = request.device_id or str(read_ui_config(p).get("desired_state", {}).get("common", {}).get("device_id") or "")
    # Baseline = what the device SHOULD look like given this change's lifecycle state,
    # so a rolled-back change reads as in-sync (absent), not a false high-severity drift.
    workflow_state = None
    if request.change_id:
        try:
            workflow_state = PlatformStore(p).get_change(request.change_id).workflow_state
        except Exception:
            workflow_state = None
    base = baseline_for_state(workflow_state)
    if execution_mode() == "runner":
        intent_yaml = Path(request.intent_path).read_text(encoding="utf-8")
        payload = {"intent_yaml": intent_yaml, "device_id": device_id,
                   "expected_present": base["expected_present"], "baseline": base["label"], "context": base["context"]}
        return _runner_read(p, "drift", payload, _request_principal(http_request).org_id)
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.find_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    state = AdapterRegistry().rez.collect_device_state(device)
    return vlan_drift_report(p, Path(request.intent_path), state, expected_present=base["expected_present"], baseline=base["label"], context=base["context"])


@app.post("/api/drift/device")
def api_device_drift(request: DeviceRequest, http_request: Request) -> dict[str, object]:
    """Per-device drift: compare live state against the device's committed baseline —
    the aggregate of every applied (live, not rolled-back) VLAN intent on that device."""
    p = paths()
    org = _request_principal(http_request).org_id
    store = PlatformStore(p)
    device_changes = [record_to_dict(c) for c in store.list_changes(limit=500, org_id=org) if c.device_id == request.device_id]
    expected = aggregate_device_vlans(device_changes, load_intent)
    if execution_mode() == "runner":
        return _runner_read(p, "device_drift", {"device_id": request.device_id, "expected": expected}, org)
    inventory = Inventory(configured_inventory_path(p))
    device = inventory.find_device(request.device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Unknown device {request.device_id}")
    state = AdapterRegistry().rez.collect_device_state(device)
    return device_drift_from_state(expected, state, request.device_id)


@app.get("/api/compliance/summary")
def api_compliance_summary() -> dict[str, object]:
    return compliance_summary(paths())


@app.post("/api/scale/plan")
def api_scale_plan(request: ScalePlanRequest) -> dict[str, object]:
    return rollout_plan(paths(), request.device_ids, request.canary_size, request.batch_size)


# ── Fleet rollouts: canary -> batch waves with auto-halt ─────────────────────

def _rollout_or_404(rollout_id: str, org: str) -> dict[str, object]:
    try:
        rollout = PlatformStore(paths()).get_rollout(rollout_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown rollout {rollout_id}") from exc
    if rollout.get("org_id") != org:  # 404 (not 403) so existence never leaks across tenants
        raise HTTPException(status_code=404, detail=f"Unknown rollout {rollout_id}")
    return rollout


@app.post("/api/fleet/rollouts")
def api_fleet_rollout_create(request: FleetRolloutRequest, http_request: Request) -> dict[str, object]:
    principal = _request_principal(http_request)
    try:
        rollout = plan_fleet_rollout(
            paths(),
            change_type=request.change_type, values=request.values,
            device_ids=request.device_ids, device_group=request.device_group,
            canary_size=request.canary_size, batch_size=request.batch_size,
            description=request.description,
            requested_by=principal.email or "netcode-user",
            org_id=principal.org_id, created_by_user_id=principal.user_id,
        )
        environment_id = request.environment_id.strip()
        model_revision_id = request.model_revision_id.strip()
        if bool(environment_id) != bool(model_revision_id):
            raise ValueError("environment_id and model_revision_id must be supplied together")
        if environment_id:
            targets = [
                target
                for wave in rollout.get("waves", [])
                for target in wave.get("targets", [])
                if isinstance(target, dict)
            ]
            if not targets:
                raise ValueError("rollout has no model candidate targets")
            intent = load_intent(Path(str(targets[0]["intent_path"]))).model_dump(mode="json")
            candidate = create_candidate_for_change_set(
                NetworkModelRepository(PlatformStore(paths())),
                PlatformStore(paths()),
                org_id=principal.org_id,
                environment_id=environment_id,
                parent_revision_id=model_revision_id,
                revision_id=f"rollout-{str(rollout['id']).lower()}",
                change_ids=[str(target.get("change_id") or "") for target in targets],
                intent=intent,
                device_ids=[str(target.get("device_id") or "") for target in targets],
                created_by=principal.email or principal.user_id or "netcode-user",
            )
            rollout["network_model"] = {
                "environment_id": environment_id,
                "parent_revision_id": model_revision_id,
                "candidate_revision_id": candidate["revision_id"] if candidate else "",
            }
        return rollout
    except (NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fleet/rollouts")
def api_fleet_rollouts(request: Request) -> dict[str, object]:
    p = paths()
    org = _request_principal(request).org_id
    store = PlatformStore(p)
    rollouts = []
    for rollout in store.list_rollouts(org_id=org):
        targets = store.list_rollout_targets(str(rollout["id"]))
        rollout, targets = annotate_rollout_audit(rollout, targets)
        counts: dict[str, int] = {}
        for t in targets:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        rollout["target_counts"] = counts
        rollout["device_count"] = len(targets)
        rollouts.append(rollout)
    return {"ok": True, "rollouts": rollouts}


@app.get("/api/fleet/rollouts/{rollout_id}")
def api_fleet_rollout(rollout_id: str, request: Request) -> dict[str, object]:
    _rollout_or_404(rollout_id, _request_principal(request).org_id)
    return rollout_status(paths(), rollout_id)


@app.get("/api/fleet/rollouts/{rollout_id}/activity")
def api_fleet_rollout_activity(
    rollout_id: str,
    request: Request,
    q: str = Query(default="", max_length=200),
    status: str = Query(default="all"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_events: bool = Query(default=False),
) -> dict[str, object]:
    principal = _request_principal(request)
    rollout = _rollout_or_404(rollout_id, principal.org_id)
    store = PlatformStore(paths())
    try:
        targets, filtered_total = store.list_rollout_targets_page(
            rollout_id,
            query=q,
            category=status,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    counts = store.rollout_target_counts(rollout_id)
    category_counts = {
        "all": sum(counts.values()),
        "running": sum(counts.get(key, 0) for key in ("running", "in_progress", "in-progress")),
        "passed": sum(counts.get(key, 0) for key in ("passed", "completed", "verified", "success")),
        "failed": sum(counts.get(key, 0) for key in ("failed", "blocked", "error")),
        "untouched": sum(counts.get(key, 0) for key in ("pending", "planned", "queued", "skipped", "cancelled")),
    }
    enriched_targets = [
        {**target, "audit_ref": device_audit_ref(str(target.get("change_id") or ""))}
        for target in targets
    ]
    events_by_device: dict[str, list[dict[str, object]]] = {}
    if include_events:
        change_ids = [str(target.get("change_id") or "") for target in enriched_targets]
        # One device lifecycle is currently under 60 milestones. Keep a bounded
        # 100-event history so "All activity" includes dry-run through rollback
        # without loading events for devices outside the current page.
        grouped = store.list_execution_events_for_changes(change_ids, per_change=100)
        for target in enriched_targets:
            change_id = str(target.get("change_id") or "")
            events_by_device[str(target.get("device_id") or "")] = [
                record_to_dict(event) for event in grouped.get(change_id, [])
            ]
    compact_rollout = {
        **rollout,
        "target_counts": counts,
        "category_counts": category_counts,
        "device_count": category_counts["all"],
        "waves": store.rollout_wave_counts(rollout_id),
    }
    return {
        "ok": True,
        "rollout": compact_rollout,
        "targets": enriched_targets,
        "events_by_device": events_by_device,
        "page": {
            "limit": limit,
            "offset": offset,
            "returned": len(enriched_targets),
            "filtered_total": filtered_total,
            "has_more": offset + len(enriched_targets) < filtered_total,
        },
    }


@app.post("/api/fleet/rollouts/{rollout_id}/start")
def api_fleet_rollout_start(rollout_id: str, request: Request) -> dict[str, object]:
    principal = _request_principal(request)
    require_production_writes(org_id=principal.org_id)
    _rollout_or_404(rollout_id, principal.org_id)
    try:
        return start_rollout(paths(), rollout_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fleet/rollouts/{rollout_id}/halt")
def api_fleet_rollout_halt(rollout_id: str, request: FleetHaltRequest, http_request: Request) -> dict[str, object]:
    _rollout_or_404(rollout_id, _request_principal(http_request).org_id)
    return request_halt(paths(), rollout_id, request.reason)


@app.delete("/api/fleet/rollouts/{rollout_id}")
def api_fleet_rollout_delete(rollout_id: str, request: Request) -> dict[str, object]:
    principal = _request_principal(request)
    _rollout_or_404(rollout_id, principal.org_id)
    actor = principal.email or principal.user_id or "netcode-user"
    try:
        return cancel_rollout(paths(), rollout_id, actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fleet/rollouts/{rollout_id}/retry")
def api_fleet_rollout_retry(
    rollout_id: str,
    retry: FleetRetryRequest,
    request: Request,
) -> dict[str, object]:
    principal = _request_principal(request)
    require_production_writes(org_id=principal.org_id)
    _rollout_or_404(rollout_id, principal.org_id)
    actor = principal.email or principal.user_id or "netcode-user"
    try:
        return retry_rollout(
            paths(),
            rollout_id,
            scope=retry.scope,
            requested_by=actor,
            created_by_user_id=principal.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fleet/rollouts/{rollout_id}/diagnostics-handoff")
def api_fleet_rollout_diagnostics_handoff(rollout_id: str, request: Request) -> dict[str, object]:
    principal = _request_principal(request)
    _rollout_or_404(rollout_id, principal.org_id)
    try:
        return rollout_failure_handoff(paths(), rollout_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _approver_identity(principal, fallback_name: str, requester: str, requester_user_id: str | None,
                       created_by: str | None = None) -> str:
    """Resolve who is approving and enforce requester != approver. With auth on,
    the logged-in principal IS the approver; with auth off, a named approver is
    required (advisory but still recorded and still must differ)."""
    if principal.kind == "user" or (principal.kind == "system" and principal.email):
        approver = principal.email or principal.user_id or ""
        if principal.user_id and requester_user_id and principal.user_id == requester_user_id:
            raise HTTPException(status_code=403, detail="The requester cannot approve their own change.")
        if approver and approver == requester:
            raise HTTPException(status_code=403, detail="The requester cannot approve their own change.")
        return approver
    approver = (fallback_name or "").strip()
    if not approver:
        raise HTTPException(status_code=400, detail="Approver name is required (auth is off, so name the second engineer).")
    if approver == requester:
        raise HTTPException(status_code=400, detail="The requester cannot approve their own change — name a second engineer.")
    return approver


@app.post("/api/change/{change_id}/approve")
def api_change_approve(change_id: str, request: ApproveRequest, http_request: Request) -> dict[str, object]:
    """Approval gate: a second engineer approves a proven (dry-run-passed) change,
    unlocking apply. The approver identity is part of the evidence record."""
    p = paths()
    principal = _request_principal(http_request)
    store = PlatformStore(p)
    try:
        change = store.get_change(change_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}") from exc
    if change.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}")
    if change.workflow_state != "dry_run_passed":
        raise HTTPException(status_code=400,
                            detail=f"Only a dry-run-proven change can be approved (state: {change.workflow_state}).")
    approver = _approver_identity(principal, request.approved_by, change.requested_by,
                                  getattr(principal, "user_id", None), change.created_by_user_id)
    try:
        model_approvals = approve_change_candidates(
            NetworkModelRepository(store),
            org_id=principal.org_id,
            change_id=change_id,
            approved_by=approver,
            git_root=p.git_workspace,
        )
    except (KeyError, NetworkModelError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Network Model approval failed; the device change remains locked: {exc}",
        ) from exc
    store.record_workflow_event(
        change_id, "approve", change.workflow_state, "approved",
        f"Approved by {approver} (requester: {change.requested_by}).",
        {
            "approved_by": approver,
            "requested_by": change.requested_by,
            "network_model_revisions": [
                item["revision"]["revision_id"] for item in model_approvals
            ],
        },
    )
    return {"ok": True, "change": record_to_dict(store.get_change(change_id)),
            "approved_by": approver,
            "network_model": {
                "approved_revisions": [item["revision"]["revision_id"] for item in model_approvals]
            },
            "message": f"Approved by {approver}. Apply is now unlocked."}


@app.post("/api/cross-domain/plans")
def api_cross_domain_plan(request: CrossDomainPlanRequest, http_request: Request) -> dict[str, object]:
    """Create a reviewable cross-domain plan. This endpoint never queues work."""
    principal = _request_principal(http_request)
    requested_by = request.requested_by.strip() or principal.email or principal.user_id or "netcode-user"
    plan = build_cross_domain_plan(
        title=request.title,
        requested_by=requested_by,
        ticket_id=request.ticket_id,
        flow=request.flow,
        routing_owner=request.routing_owner,
        sdwan_owner=request.sdwan_owner,
        firewall_policy=request.firewall_policy,
        firewall_nat=request.firewall_nat,
    )
    p = paths()
    intent_path = p.intents / "generated" / f"{plan.plan_id.lower()}.yaml"
    write_yaml(intent_path, {"schema": "netcode.cross-domain.v1", "plan": plan.model_dump(mode="json")})
    store = PlatformStore(p)
    change = store.create_change(
        intent_path,
        request.firewall_policy.ownership.device_id,
        requested_by=requested_by,
        org_id=principal.org_id,
        created_by_user_id=principal.user_id,
    )
    result = {
        "source": "cross_domain_plan",
        "plan": plan.model_dump(mode="json"),
        "manager_results": [],
        "service_assurance": None,
        "human_approval_required": True,
        "device_writes_queued": False,
    }
    store.update_change(change.id, "validated", result, workflow_state="validated")
    store.record_workflow_event(
        change.id,
        "cross_domain_plan",
        "draft",
        "validated",
        "Cross-domain dependency plan created. No manager or device write was queued.",
        {"plan_id": plan.plan_id, "ticket_id": plan.ticket_id, "flow": plan.flow.model_dump()},
    )
    return {
        "ok": True,
        "change": record_to_dict(store.get_change(change.id)),
        "plan": plan.model_dump(mode="json"),
        "intent_path": str(intent_path),
        "next_action": "Run manager preview and validation through the assigned runner.",
    }


@app.get("/api/cross-domain/plans/{change_id}")
def api_cross_domain_plan_get(change_id: str, http_request: Request) -> dict[str, object]:
    principal = _request_principal(http_request)
    change = PlatformStore(paths()).get_change(change_id)
    if change.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail="Unknown cross-domain change")
    result = dict(change.result or {})
    if result.get("source") != "cross_domain_plan":
        raise HTTPException(status_code=400, detail="Change is not a cross-domain plan")
    return {"ok": True, "change": record_to_dict(change), **result}


@app.post("/api/cross-domain/plans/{change_id}/manager/{action}")
def api_cross_domain_manager_action(
    change_id: str,
    action: str,
    request: ManagerActionQueueRequest,
    http_request: Request,
) -> dict[str, object]:
    allowed = {
        "probe", "snapshot", "preview", "validate", "lock", "stage", "deploy",
        "poll", "verify", "discard", "unlock", "rollback",
    }
    if action not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported manager action {action}")
    principal = _request_principal(http_request)
    if action in WRITE_ACTIONS:
        require_production_writes(org_id=principal.org_id)
    store = PlatformStore(paths())
    try:
        change = store.get_change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if change.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail="Unknown cross-domain change")
    stored = dict(change.result or {})
    try:
        plan = CrossDomainPlan.model_validate(stored.get("plan"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Change does not contain a valid cross-domain plan") from exc
    policy = plan.firewall_policy
    nat = plan.firewall_nat
    ownership = (policy or nat).ownership if (policy or nat) else None
    if ownership is None:
        raise HTTPException(status_code=400, detail="Cross-domain plan has no manager-owned firewall intent")

    approved_by: str | None = None
    approvals = [
        event for event in store.list_workflow_events(change.id)
        if event.action in {"approve", "approve_manager", "approve_rollback"}
    ]
    if action in WRITE_ACTIONS:
        approved_by = str((approvals[-1].evidence or {}).get("approved_by") or "") if approvals else None
    approval = ApprovalProof(
        approved=bool(approved_by),
        requested_by=change.requested_by,
        approved_by=approved_by,
        workflow_state="approved" if approved_by else change.workflow_state,
    )
    scope = ownership.scope
    location = (
        f"{scope.adom}/{scope.policy_package}"
        if ownership.manager_type == "fortimanager"
        else f"{scope.device_group}/{scope.rulebase}"
    )
    operation_id = request.operation_id.strip() or f"{change.id}:{action}:{uuid.uuid4().hex[:10]}"
    try:
        manager_request = ManagerJobRequest(
            action=action,
            operation_id=operation_id,
            change_id=change.id,
            manager_id=ownership.manager_id,
            ownership=ownership,
            capabilities=request.capabilities,
            policy_change=policy,
            nat_change=nat,
            flow=plan.flow,
            approval=approval,
            expected_candidate_owner=change.requested_by,
            expected_candidate_location=location,
            unrelated_candidate_changes=[],
            manager_task_id=request.manager_task_id,
            pre_change_revision=request.pre_change_revision,
        )
        return JobRunner(paths(), store=store).queue_manager_action(change.id, manager_request.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cross-domain/plans/{change_id}/approve-rollback")
def api_cross_domain_approve_rollback(
    change_id: str,
    request: ApproveRequest,
    http_request: Request,
) -> dict[str, object]:
    """Require a fresh human decision before a manager rollback write."""
    principal = _request_principal(http_request)
    store = PlatformStore(paths())
    try:
        change = store.get_change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if change.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail="Unknown cross-domain change")
    if change.workflow_state not in {"rollback_available", "failed"}:
        raise HTTPException(
            status_code=409,
            detail=f"Rollback approval requires an applied or failed change (state: {change.workflow_state}).",
        )
    approver = _approver_identity(
        principal,
        request.approved_by,
        change.requested_by,
        change.created_by_user_id,
        principal.user_id,
    )
    store.record_workflow_event(
        change.id,
        "approve_rollback",
        change.workflow_state,
        "approved",
        f"Rollback approved by {approver}.",
        {"approved_by": approver, "requested_by": change.requested_by, "rollback_only": True},
    )
    return {
        "ok": True,
        "approved_by": approver,
        "change": record_to_dict(store.get_change(change.id)),
        "message": "Rollback is approved; the manager rollback action remains runner-gated.",
    }


@app.post("/api/cross-domain/plans/{change_id}/verify")
def api_cross_domain_verify(
    change_id: str,
    request: CrossDomainVerifyRequest,
    http_request: Request,
) -> dict[str, object]:
    principal = _request_principal(http_request)
    store = PlatformStore(paths())
    try:
        change = store.get_change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if change.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail="Unknown cross-domain change")
    stored = dict(change.result or {})
    try:
        plan = CrossDomainPlan.model_validate(stored.get("plan"))
        manager_job = store.get_job(request.manager_job_id)
        if manager_job.change_id != change.id or manager_job.action != "manager_deploy":
            raise ValueError("manager_job_id is not the deploy job for this cross-domain change")
        if manager_job.status not in TERMINAL_JOB_STATUSES or not manager_job.signature:
            raise ValueError("manager deploy evidence must be a completed, signed runner result")
        manager_push_status = "success" if manager_job.status == "completed" else "failed"
        evidence: list[CheckEvidence] = []
        for job_id in list(dict.fromkeys(request.evidence_job_ids)):
            job = store.get_job(job_id)
            if job.action != "read_cross_domain_verify" or job.status not in TERMINAL_JOB_STATUSES or not job.signature:
                raise ValueError(f"evidence job {job_id} is not a completed, signed cross-domain runner result")
            job_change_id = str((job.result or {}).get("change_id") or "")
            if job_change_id != change.id:
                raise ValueError(f"evidence job {job_id} belongs to a different change")
            for item in (job.result or {}).get("service_checks") or []:
                evidence.append(CheckEvidence.model_validate(item))
        evidence.append(CheckEvidence(
            check="manager_intent",
            status="pass" if manager_push_status == "success" else "fail",
            fresh=True,
            flow_key=f"{plan.flow.source_ip}>{plan.flow.destination_ip}/{plan.flow.protocol}/{plan.flow.destination_port or '-'}",
            source=f"signed-manager-job:{manager_job.id}",
            observed=manager_push_status,
            expected="success",
            evidence_refs=[manager_job.id],
        ))
        assurance = evaluate_service_assurance(
            plan,
            manager_push_status=manager_push_status,
            evidence=evidence,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stored["service_assurance"] = assurance.model_dump(mode="json")
    if assurance.status == "verified":
        record_status, next_state = "completed", "completed"
    elif assurance.status == "failed":
        record_status, next_state = "failed", "failed"
    else:
        record_status, next_state = "blocked", change.workflow_state
    store.update_change(change.id, record_status, stored, workflow_state=next_state)
    store.record_workflow_event(
        change.id,
        "service_verify",
        change.workflow_state,
        next_state,
        f"Exact-flow service assurance: {assurance.status}.",
        assurance.model_dump(mode="json"),
    )
    handoff = None
    if assurance.status == "failed":
        handoff = attach_verification_handoff(
            store,
            change_id=change.id,
            device_id=plan.flow.source_device,
            check="cross_domain_application_flow",
            expected=f"{plan.flow.protocol}/{plan.flow.destination_port or '-'} succeeds to {plan.flow.destination_ip}",
            actual=f"Failed domain: {assurance.failed_domain}",
            verification={
                "status": "fail",
                "failed": True,
                "expected": "exact application flow succeeds",
                "actual": f"failed domain {assurance.failed_domain}",
                "assurance": assurance.model_dump(mode="json"),
            },
            intent_path=change.intent_path,
        )
    return {
        "ok": assurance.status == "verified",
        "change": record_to_dict(store.get_change(change.id)),
        "service_assurance": assurance.model_dump(mode="json"),
        "diagnostics_handoff": handoff,
    }


@app.post("/api/cross-domain/plans/{change_id}/verify/start")
def api_cross_domain_verify_start(change_id: str, http_request: Request) -> dict[str, object]:
    """Queue credential-free exact-flow collection to the source device's runner."""
    principal = _request_principal(http_request)
    store = PlatformStore(paths())
    try:
        change = store.get_change(change_id)
        plan = CrossDomainPlan.model_validate((change.result or {}).get("plan"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Unknown cross-domain plan") from exc
    if change.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail="Unknown cross-domain plan")
    source = store.resolve_device(change.org_id, plan.flow.source_device)
    if source is None:
        raise HTTPException(status_code=400, detail=f"Source device {plan.flow.source_device} is not in the runner catalog")
    firewall_id = (plan.firewall_policy or plan.firewall_nat).ownership.device_id
    payload = {
        "change_id": change.id,
        "plan_id": plan.plan_id,
        "flow": plan.flow.model_dump(mode="json"),
        "required_checks": [check for check in plan.verification.required_checks if check != "manager_intent"],
        "devices": {
            "source": plan.flow.source_device,
            "route_owner": plan.flow.expected_route_owner or plan.flow.source_device,
            "firewall": firewall_id,
        },
    }
    job = store.create_read_job(
        change.org_id,
        str(source["runner_pool"]),
        "cross_domain_verify",
        payload,
        target_runner_id=str(source["runner_id"]),
    )
    store.record_workflow_event(
        change.id,
        "service_verify_start",
        change.workflow_state,
        change.workflow_state,
        "Queued exact-flow verification through the source device's runner.",
        {"job_id": job.id, "flow": plan.flow.model_dump(mode="json")},
    )
    return {"ok": True, "job": record_to_dict(job), "change": record_to_dict(store.get_change(change.id))}


@app.post("/api/fleet/rollouts/{rollout_id}/approve")
def api_fleet_rollout_approve(rollout_id: str, request: ApproveRequest, http_request: Request) -> dict[str, object]:
    principal = _request_principal(http_request)
    rollout = _rollout_or_404(rollout_id, principal.org_id)
    approver = _approver_identity(principal, request.approved_by, str(rollout.get("requested_by") or ""),
                                  getattr(principal, "user_id", None), rollout.get("created_by_user_id"))
    try:
        store = PlatformStore(paths())
        target_change_ids = {
            str(target.get("change_id") or "")
            for wave in rollout.get("waves", [])
            for target in wave.get("targets", [])
            if isinstance(target, dict) and target.get("change_id")
        }
        for change_id in sorted(target_change_ids):
            approve_change_candidates(
                NetworkModelRepository(store),
                org_id=principal.org_id,
                change_id=change_id,
                approved_by=approver,
                git_root=paths().git_workspace,
            )
        return approve_rollout(paths(), rollout_id, approver)
    except (KeyError, NetworkModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fleet/remediate")
def api_fleet_remediate(http_request: Request) -> dict[str, object]:
    """Closed loop: turn the latest drift findings into governed remediation
    rollouts targeting only the drifted devices."""
    principal = _request_principal(http_request)
    try:
        rollouts = create_remediation_rollouts(
            paths(), principal.org_id,
            requested_by=principal.email or "netcode-user",
            created_by_user_id=principal.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "rollouts": rollouts, "count": len(rollouts)}


@app.post("/api/fleet/drift/watch")
def api_fleet_drift_watch(request: DriftWatchRequest, http_request: Request) -> dict[str, object]:
    org = _request_principal(http_request).org_id
    minutes = max(0, min(int(request.minutes), 1440))
    return set_drift_watch(paths(), org, minutes, load_intent)


@app.post("/api/fleet/drift/refresh")
def api_fleet_drift_refresh(request: Request) -> dict[str, object]:
    return start_fleet_drift(paths(), _request_principal(request).org_id, load_intent)


@app.get("/api/fleet/drift")
def api_fleet_drift(request: Request) -> dict[str, object]:
    org = _request_principal(request).org_id
    snapshot = fleet_drift_snapshot(org)
    snapshot["watch"] = drift_watch_status(org)
    return snapshot


@app.post("/api/assistant")
def api_assistant(request: AssistantRequest) -> dict[str, object]:
    return assistant_response(request.prompt, request.context)


@app.get("/api/changes")
def api_changes(
    request: Request,
    q: str = Query(default="", max_length=240),
    device_id: str = Query(default="", max_length=240),
    state: str = Query(default="", max_length=80),
    requested_by: str = Query(default="", max_length=320),
    source: str = Query(default="", max_length=80),
    site: str = Query(default="", max_length=160),
    workflow_type: str = Query(default="", max_length=120),
    created_from: str = Query(default="", max_length=64),
    created_to: str = Query(default="", max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, object]:
    store = PlatformStore(paths())
    org = _request_principal(request).org_id
    records, total = store.search_changes(
        org_id=org,
        query=q,
        device_id=device_id,
        state=state,
        requested_by=requested_by,
        source=source,
        site=site,
        workflow_type=workflow_type,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    returned = len(records)
    return {
        "changes": [change_summary_to_dict(record) for record in records],
        "total": total,
        "returned": returned,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + returned if offset + returned < total else None,
        "device_connections_opened": 0,
    }


@app.post("/api/changes/from-rca")
def api_change_from_rca(request: RcaRemediationProposalRequest, http_request: Request) -> dict[str, object]:
    """Create a Netcode draft change from a Rez RCA remediation proposal.

    This is intentionally draft-only: it writes an intent artifact and change
    record, but never queues a job or unlocks apply. Human review, dry-run, and
    approval remain the write boundary.
    """
    if request.source.strip().lower() != "rez":
        raise HTTPException(status_code=400, detail="Only Rez RCA proposals are accepted.")
    if not request.incident_id.strip():
        raise HTTPException(status_code=400, detail="incident_id is required.")
    _require_confirmed_rca_provenance(request)

    p = paths()
    principal = _request_principal(http_request)
    store = PlatformStore(p)
    intent = _intent_from_rca_proposal(request)
    try:
        load_intent_data(intent)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid remediation intent: {exc}") from exc

    incident_slug = _safe_slug(request.incident_id)
    intent_path = p.intents / "rca" / f"{incident_slug}-{uuid.uuid4().hex[:8]}.yaml"
    write_yaml(intent_path, intent)
    pipeline = run_static_pipeline(p, intent_path, org_id=principal.org_id)

    title = request.title.strip() or f"Rez RCA remediation for {request.incident_id.strip()}"
    target_device = request.target_device.strip() or None
    requested_by = request.requested_by.strip() or "rez-rca"
    target_ids = [
        str(item).strip()
        for item in ((intent.get("targets") or {}).get("device_ids") or [])
        if str(item).strip()
    ]
    environment_id = request.environment_id.strip()
    model_revision_id = request.model_revision_id.strip()
    if bool(environment_id) != bool(model_revision_id):
        raise HTTPException(
            status_code=400,
            detail="environment_id and model_revision_id must be supplied together.",
        )

    if pipeline.status == "pass" and intent.get("change_type") == "routing_redistribution" and len(target_ids) > 1:
        redistribution = dict(intent.get("redistribution") or {})
        if isinstance(intent.get("reverse_redistribution"), dict):
            redistribution["reverse_redistribution"] = dict(intent["reverse_redistribution"])
        if isinstance(intent.get("reachability_checks"), list):
            redistribution["reachability_checks"] = [
                dict(item) for item in intent["reachability_checks"] if isinstance(item, dict)
            ]
        redistribution["ticket_id"] = request.incident_id.strip()
        rollout = plan_fleet_rollout(
            p,
            change_type="routing_redistribution",
            values=redistribution,
            device_ids=target_ids,
            device_group=None,
            canary_size=1,
            batch_size=max(1, len(target_ids) - 1),
            description=title,
            requested_by=requested_by,
            org_id=principal.org_id,
            created_by_user_id=principal.user_id,
        )
        rollout_evidence = {
            "source": "rez_rca",
            "draft_only": True,
            "human_approval_required": True,
            "incident_id": request.incident_id.strip(),
            "title": title,
            "suggested_pack": request.suggested_pack,
            "change_type": intent.get("change_type"),
            "rationale": request.rationale,
            "confidence": request.confidence,
            "evidence_refs": request.evidence_refs,
            "rollout_id": rollout["id"],
        }
        target_rows = [
            target
            for wave in rollout.get("waves", [])
            for target in wave.get("targets", [])
            if isinstance(target, dict)
        ]
        candidate = None
        if environment_id:
            change_ids = [str(target.get("change_id") or "") for target in target_rows]
            try:
                candidate = create_candidate_for_change_set(
                    NetworkModelRepository(store),
                    store,
                    org_id=principal.org_id,
                    environment_id=environment_id,
                    parent_revision_id=model_revision_id,
                    revision_id=f"rollout-{str(rollout['id']).lower()}",
                    change_ids=change_ids,
                    intent=intent,
                    device_ids=target_ids,
                    created_by=principal.email or principal.user_id or requested_by,
                )
            except NetworkModelError as exc:
                raise HTTPException(status_code=409, detail=f"Network Model candidate rejected: {exc}") from exc
            rollout_evidence["network_model"] = {
                "environment_id": environment_id,
                "parent_revision_id": model_revision_id,
                "candidate_revision_id": candidate["revision_id"] if candidate else "",
            }
        for target in target_rows:
            change_id = str(target.get("change_id") or "")
            if not change_id:
                continue
            target_change = store.get_change(change_id)
            result = dict(target_change.result or {})
            result.update(rollout_evidence)
            result["pipeline"] = result.get("pipeline") or result.copy()
            store.update_change(
                change_id,
                target_change.status,
                result,
                workflow_state=target_change.workflow_state,
            )
            store.record_workflow_event(
                change_id,
                "rca_proposal",
                target_change.workflow_state,
                target_change.workflow_state,
                f"Added to Rez RCA rollout {str(rollout['id'])[:8]} for incident {request.incident_id.strip()}.",
                rollout_evidence,
            )
        canary = target_rows[0] if target_rows else {}
        canary_change = store.get_change(str(canary.get("change_id"))) if canary.get("change_id") else None
        return {
            "ok": True,
            "draft_only": True,
            "human_approval_required": True,
            "rez_change_id": rollout.get("rez_change_id"),
            "rollout_id": rollout["id"],
            "rollout": rollout,
            "change_id": canary_change.id if canary_change else None,
            "change": record_to_dict(canary_change) if canary_change else None,
            "intent_path": canary.get("intent_path") or str(intent_path),
            "intent": intent,
            "network_model": rollout_evidence.get("network_model"),
        }

    change = store.create_change(
        intent_path,
        target_device,
        requested_by=requested_by,
        org_id=principal.org_id,
        created_by_user_id=principal.user_id,
    )
    candidate = None
    if environment_id and pipeline.status == "pass":
        try:
            candidate = create_candidate_for_change_intent(
                NetworkModelRepository(store),
                store,
                org_id=principal.org_id,
                environment_id=environment_id,
                parent_revision_id=model_revision_id,
                change_id=change.id,
                intent=intent,
                device_id=target_ids[0] if target_ids else str(target_device or ""),
                created_by=principal.email or principal.user_id or requested_by,
            )
        except NetworkModelError as exc:
            raise HTTPException(status_code=409, detail=f"Network Model candidate rejected: {exc}") from exc
    evidence = {
        "source": "rez_rca",
        "draft_only": True,
        "human_approval_required": True,
        "incident_id": request.incident_id.strip(),
        "title": title,
        "target_device": target_device,
        "suggested_pack": request.suggested_pack,
        "change_type": intent.get("change_type"),
        "rationale": request.rationale,
        "confidence": request.confidence,
        "evidence_refs": request.evidence_refs,
        "network_model": {
            "environment_id": environment_id,
            "parent_revision_id": model_revision_id,
            "candidate_revision_id": candidate["revision_id"] if candidate else "",
        },
        "pipeline": pipeline.model_dump(),
        "plan": {
            "commands": pipeline.render.config,
            "rollback": rollback_config(load_intent_data(intent)),
            "validation_status": pipeline.status,
            "checks": [check.model_dump() for check in pipeline.validation.checks],
            "artifacts": pipeline.artifacts.model_dump() if pipeline.artifacts else None,
        },
    }
    workflow = state_after_static_validation(pipeline.status == "pass")
    store.update_change(
        change.id,
        "validated" if pipeline.status == "pass" else "blocked",
        evidence,
        workflow_state=workflow.state,
    )
    store.record_workflow_event(
        change.id,
        "rca_proposal",
        "draft",
        workflow.state,
        f"Created Rez RCA remediation draft and ran static validation: {title}. {workflow.message}",
        evidence,
    )
    change = store.get_change(change.id)
    serialized_change = record_to_dict(change)
    return {
        "ok": True,
        "draft_only": True,
        "human_approval_required": True,
        "rez_change_id": serialized_change["rez_change_id"],
        "change_id": change.id,
        "change": serialized_change,
        "intent_path": str(intent_path),
        "intent": intent,
        "workflow": workflow_snapshot(change.workflow_state).as_dict(),
        "network_model": evidence["network_model"],
    }


@app.get("/api/jobs")
def api_jobs(request: Request) -> dict[str, object]:
    store = PlatformStore(paths())
    org = _request_principal(request).org_id
    store.recover_expired_jobs(org_id=org)
    return {"jobs": [record_to_dict(record) for record in store.list_jobs(org_id=org)]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, request: Request) -> dict[str, object]:
    """Single job status for UI polling of runner-executed (queued) lab actions."""
    store = PlatformStore(paths())
    org = _request_principal(request).org_id
    store.recover_expired_jobs(org_id=org)
    try:
        job = store.get_job(job_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}") from exc
    if job.org_id != org:  # 404 (not 403) so job existence never leaks across tenants
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return record_to_dict(job)


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str, request: Request, payload: JobCancelRequest) -> dict[str, object]:
    principal = _request_principal(request)
    actor = principal.email or principal.user_id or "netcode-operator"
    store = PlatformStore(paths())
    try:
        job = store.cancel_job_for_org(
            job_id,
            principal.org_id,
            actor=actor,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job": record_to_dict(job), "message": job.message}


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


@app.get("/api/change/{change_id}/activity")
def api_change_activity(
    change_id: str,
    request: Request,
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict[str, object]:
    store = PlatformStore(paths())
    try:
        change = store.get_change(change_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}") from exc
    if change.org_id != _request_principal(request).org_id:
        raise HTTPException(status_code=404, detail=f"Unknown change {change_id}")
    jobs = [job for job in store.list_jobs(limit=500, org_id=change.org_id) if job.change_id == change_id]
    active_job = next((job for job in jobs if job.status in {"queued", "running"}), None)
    return {
        "ok": True,
        "change_id": change_id,
        "rez_change_id": change_audit_id(change.id, change.created_at),
        "device_id": change.device_id,
        "workflow_state": change.workflow_state,
        "active_job": record_to_dict(active_job) if active_job else None,
        "events": [record_to_dict(event) for event in store.list_execution_events(change_id, limit=limit)],
    }


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
    # A change's stored result is overwritten by later lab actions (apply/rollback store
    # their lab result, which has no plan/validation). So source the record from DURABLE
    # artifacts — recompute plan metadata from the intent, and read the persisted validation
    # report — falling back to whatever is still in result.
    intent_path_for_record = Path(str(change_dict.get("intent_path") or ""))
    durable_plan: dict = {}
    durable_intent: dict = {}
    try:
        if intent_path_for_record.exists():
            _record_intent = load_intent(intent_path_for_record)
            durable_plan = plan_metadata(_record_intent)
            durable_intent = _record_intent.model_dump()
    except Exception:
        pass
    durable_validation: dict = {}
    durable_render: dict = {}
    _report_slug = (result.get("plan") or {}).get("slug") or durable_plan.get("slug") or intent_path_for_record.stem
    _report_json = paths().reports / f"{_report_slug}.json"
    if _report_json.exists():
        try:
            _report = json.loads(_report_json.read_text(encoding="utf-8"))
            durable_validation = _report.get("validation") or {}
            durable_render = _report.get("render") or {}
            durable_intent = durable_intent or (_report.get("intent") or {})
        except Exception:
            pass
    plan = result.get("plan") or durable_plan
    validation = result.get("validation") or durable_validation
    render = result.get("render") or durable_render
    intent_info = result.get("intent") or durable_intent
    rollback_value = plan.get("rollback") if isinstance(plan, dict) else None
    rollback_plan = (
        dict(rollback_value)
        if isinstance(rollback_value, dict)
        else {"commands": str(rollback_value or "")}
    )
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

    git_root = p.git_workspace
    archived_intent = git_root / "changes" / change_id / "intent.yaml"
    try:
        evidence = (
            git_evidence(git_root, archived_intent)
            if archived_intent.exists()
            else {
                "available": False,
                "message": "No change-history checkpoint has been created for this change yet.",
            }
        )
    except Exception as exc:
        evidence = {"available": False, "message": f"Git evidence unavailable: {exc}"}
    git_status = git_workspace_status(git_root)
    git_actions = [e for e in events if str(e.get("action", "")) in ("git_commit", "git_push")]
    model_revisions = NetworkModelRepository(store).revisions_for_link(
        change.org_id,
        link_type="change",
        external_id=change.id,
    )

    return {
        "ok": True,
        "change_id": change_id,
        "rez_change_id": change_dict.get("rez_change_id"),
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
            "rollback": rollback_plan,
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
        "diagnostics_handoffs": list(result.get("diagnostics_handoffs") or []),
        "network_model": {
            "revisions": [
                {
                    "environment_id": revision["environment_id"],
                    "revision_id": revision["revision_id"],
                    "parent_revision_id": revision.get("parent_revision_id"),
                    "status": revision["status"],
                }
                for revision in model_revisions
            ]
        },
        "git": {
            "workspace": str(git_root),
            "isolated_from_application": True,
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
