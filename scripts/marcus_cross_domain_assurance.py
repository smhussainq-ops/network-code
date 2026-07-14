#!/usr/bin/env python3
"""Run the controlled Marcus cross-domain assurance user story.

This exercises the real control-plane contracts and signed runner-result path in
an isolated workspace. It does not claim a live Panorama or FortiManager lab.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from netcode.bootstrap import init_workspace
from netcode.firewall_managers import (
    ApplicationFlow,
    FirewallObjectRef,
    FirewallPolicyChange,
    ManagerOwnership,
    ManagerScope,
    capabilities_from_probe,
)
from netcode.paths import WorkspacePaths
from netcode.runner_hub import sign_result, submit_job_result
from netcode.store import PlatformStore
from netcode.yamlio import write_yaml


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def manager_ownership() -> ManagerOwnership:
    return ManagerOwnership(
        device_id="branch-fw-03",
        manager_id="panorama-prod-01",
        manager_type="panorama",
        scope=ManagerScope(
            device_group="branch-firewalls",
            template_stack="branch-standard",
            vsys="vsys1",
            rulebase="pre",
        ),
        managed_serial="0123456789",
    )


def application_flow() -> ApplicationFlow:
    return ApplicationFlow(
        source_site="Branch-204",
        source_device="branch-edge-03",
        source_ip="10.204.20.10",
        destination_ip="10.40.8.25",
        protocol="tcp",
        destination_port=443,
        expected_route_owner="dc-core-01",
        expected_sdwan_class="business-critical",
        expected_firewall_action="allow",
        expected_nat="none",
        expected_application_result="tcp_connect",
    )


def firewall_policy() -> FirewallPolicyChange:
    ownership = manager_ownership()
    return FirewallPolicyChange(
        name="allow-branch204-app",
        ownership=ownership,
        source_zones=["branch-trust"],
        destination_zones=["dc-app"],
        source_objects=[FirewallObjectRef(name="BRANCH204-USERS", value="10.204.20.0/24", kind="address")],
        destination_objects=[FirewallObjectRef(name="DC-APP-25", value="10.40.8.25/32", kind="address")],
        services=[FirewallObjectRef(name="TCP-443", value="tcp/443", kind="service")],
        applications=[FirewallObjectRef(name="ssl", value="ssl", kind="application")],
        action="allow",
        security_profiles=["strict-default"],
        insertion={"position": "before", "reference_rule": "branch-default-deny"},
        target_device_ids=[ownership.device_id],
        ticket_id="CHG-2048",
    )


def manager_capabilities() -> dict[str, Any]:
    return capabilities_from_probe(
        "panorama",
        "11.1.4",
        {
            "read": True,
            "workspace_lock": True,
            "snapshot": True,
            "preview": True,
            "validate": True,
            "scoped_push": True,
            "task_poll": True,
            "rollback": True,
            "candidate_filter": True,
        },
    ).model_dump(mode="json")


def sign_and_submit(store: PlatformStore, runner, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    claimed = store.claim_next_job(runner.org_id, runner.pool, runner.id)
    require(claimed is not None and claimed.id == job_id, f"runner did not claim expected job {job_id}")
    accepted = submit_job_result(
        store,
        runner,
        job_id,
        result,
        sign_result("runner-hmac", result),
        claimed.lease_token,
    )
    require(accepted.get("ok") is True, f"signed runner result rejected: {accepted}")
    return accepted


def exact_flow_checks(required: list[str], *, return_route_passes: bool) -> list[dict[str, Any]]:
    flow = application_flow()
    key = f"{flow.source_ip}>{flow.destination_ip}/{flow.protocol}/{flow.destination_port}"
    rows: list[dict[str, Any]] = []
    for check in required:
        if check == "manager_intent":
            continue
        status = "pass"
        observed: Any = "pass"
        if check == "return_route" and not return_route_passes:
            status = "fail"
            observed = "10.204.20.0/24 absent from DC return RIB"
        rows.append(
            {
                "check": check,
                "status": status,
                "fresh": True,
                "flow_key": key,
                "source": "signed-runner:branch-runner",
                "observed": observed,
                "expected": "pass",
                "evidence_refs": [f"live:{check}:Branch-204"],
            }
        )
    return rows


def queue_exact_flow_result(
    store: PlatformStore,
    runner,
    change_id: str,
    checks: list[dict[str, Any]],
) -> str:
    job = store.create_read_job(
        runner.org_id,
        runner.pool,
        "cross_domain_verify",
        {"change_id": change_id},
        target_runner_id=runner.id,
    )
    result = {
        "ok": True,
        "status": "pass",
        "change_id": change_id,
        "service_checks": checks,
        "message": "Exact-flow evidence collected by the customer-side runner.",
    }
    sign_and_submit(store, runner, job.id, result)
    return job.id


def run_story(workspace: Path) -> dict[str, Any]:
    os.environ["NETCODE_WORKSPACE"] = str(workspace)
    os.environ["NETCODE_EXECUTION"] = "runner"
    os.environ["NETCODE_RUNNER_POOL"] = "branch"

    paths = WorkspacePaths(workspace)
    init_workspace(paths)
    write_yaml(
        paths.inventories / "lab.yaml",
        {
            "lab_type": "controlled_cross_domain",
            "defaults": {"platform": "arista_eos", "port": 22},
            "devices": [
                {"id": "branch-edge-03", "hostname": "branch-edge-03", "host": "192.0.2.20", "site": "Branch-204"},
                {"id": "dc-core-01", "hostname": "dc-core-01", "host": "192.0.2.40", "site": "Data-Center"},
                {"id": "branch-fw-03", "hostname": "branch-fw-03", "host": "192.0.2.33", "platform": "palo_alto", "site": "Branch-204"},
            ],
        },
    )
    from netcode import api  # Import after the workspace and static tree exist.

    store = PlatformStore(paths)
    runner = store.create_runner("branch-runner", "branch", "runner-token-hash", "runner-hmac")
    ownership = manager_ownership().public_dict()
    store.sync_runner_devices(
        runner,
        [
            {"id": "panorama-prod-01", "hostname": "panorama-prod-01", "host": "192.0.2.10", "port": 443, "platform": "panorama", "site": "control", "role": "manager"},
            {"id": "branch-fw-03", "hostname": "branch-fw-03", "host": "192.0.2.33", "port": 22, "platform": "palo_alto", "site": "Branch-204", "role": "firewall", "management": ownership},
            {"id": "branch-edge-03", "hostname": "branch-edge-03", "host": "192.0.2.20", "port": 22, "platform": "arista_eos", "site": "Branch-204", "role": "edge"},
            {"id": "dc-core-01", "hostname": "dc-core-01", "host": "192.0.2.40", "port": 22, "platform": "arista_eos", "site": "Data-Center", "role": "core"},
        ],
        revision="marcus-controlled-v1",
    )
    client = TestClient(api.app)
    timeline: list[dict[str, Any]] = []

    plan_payload = {
        "title": "Enable Branch-204 application access",
        "requested_by": "marcus",
        "ticket_id": "CHG-2048",
        "flow": application_flow().model_dump(mode="json"),
        "routing_owner": "branch-edge-03",
        "sdwan_owner": "branch-edge-03",
        "firewall_policy": firewall_policy().model_dump(mode="json"),
    }
    created = client.post("/api/cross-domain/plans", json=plan_payload)
    require(created.status_code == 200, created.text)
    change_id = created.json()["change"]["id"]
    required_checks = created.json()["plan"]["verification"]["required_checks"]
    require(created.json()["plan"]["manager_success_is_service_success"] is False, "manager/service boundary missing")
    require(store.list_jobs() == [], "planning queued an unexpected device job")
    timeline.append({"step": "plan", "status": "passed", "change_id": change_id, "jobs_queued": 0})

    preview = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/preview",
        json={"capabilities": manager_capabilities(), "operation_id": "marcus-preview-2048"},
    )
    require(preview.status_code == 200, preview.text)
    preview_job = preview.json()["job"]
    sign_and_submit(
        store,
        runner,
        preview_job["id"],
        {"status": "pass", "message": "Candidate diff and manager validation passed; no manager write performed."},
    )
    require(store.get_change(change_id).workflow_state == "dry_run_passed", "preview did not unlock approval")
    timeline.append({"step": "manager_preview", "status": "passed", "signed_runner_job": preview_job["id"]})

    self_approval = client.post(f"/api/change/{change_id}/approve", json={"approved_by": "marcus"})
    require(self_approval.status_code == 400, "requester was able to self-approve")
    approval = client.post(f"/api/change/{change_id}/approve", json={"approved_by": "syed"})
    require(approval.status_code == 200, approval.text)
    timeline.append({"step": "approval", "status": "passed", "self_approval_blocked": True, "approved_by": "syed"})

    deploy = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/deploy",
        json={"capabilities": manager_capabilities(), "operation_id": "marcus-deploy-2048"},
    )
    require(deploy.status_code == 200, deploy.text)
    deploy_job = deploy.json()["job"]
    sign_and_submit(
        store,
        runner,
        deploy_job["id"],
        {"status": "pass", "message": "Panorama job 9071 completed and running policy contains the reviewed rule.", "manager_task_id": "9071"},
    )
    require(store.get_change(change_id).workflow_state == "rollback_available", "manager deploy did not retain rollback")
    timeline.append({"step": "manager_deploy", "status": "passed", "manager_job": "9071", "signed_runner_job": deploy_job["id"]})

    browser_spoof = client.post(
        f"/api/cross-domain/plans/{change_id}/verify",
        json={"manager_push_status": "success", "evidence": []},
    )
    require(browser_spoof.status_code == 422, "browser was able to self-assert service evidence")

    failed_checks = exact_flow_checks(required_checks, return_route_passes=False)
    failed_evidence_job = queue_exact_flow_result(store, runner, change_id, failed_checks)
    failed = client.post(
        f"/api/cross-domain/plans/{change_id}/verify",
        json={"manager_job_id": deploy_job["id"], "evidence_job_ids": [failed_evidence_job]},
    )
    require(failed.status_code == 200, failed.text)
    failed_body = failed.json()
    require(failed_body["service_assurance"]["failed_domain"] == "routing", "wrong failed domain")
    require(failed_body["service_assurance"]["manager_push_status"] == "success", "manager success was lost")
    require(failed_body["diagnostics_handoff"]["context"]["read_only"] is True, "Rez handoff is not read-only")
    require(failed_body["diagnostics_handoff"]["safety"]["device_writes"] == "none", "Rez handoff can write")
    timeline.append(
        {
            "step": "service_verify_before_fix",
            "status": "failed_as_expected",
            "manager_push": "success",
            "failed_domain": "routing",
            "browser_spoof_blocked": True,
            "rez_handoff": "read_only",
        }
    )

    remediation_payload = {
        "source": "rez",
        "proposal_schema": "netcode.remediation.v1",
        "proposal_source": "rez_structured_rca",
        "root_confirmed": True,
        "root_atom_id": "CONFIG_ROUTE_REDISTRIBUTION_MISSING",
        "incident_id": "INC-2048",
        "target_device": "dc-core-01",
        "suggested_pack": "routing_redistribution",
        "requested_by": "marcus",
        "title": "Restore Branch-204 return route",
        "rationale": "Fresh exact-flow evidence confirmed that firewall policy, NAT, and SD-WAN pass while the return route is absent.",
        "confidence": 0.98,
        "evidence_refs": [failed_evidence_job, deploy_job["id"], "live:return_route:Branch-204"],
        "proposed_intent": {
            "change_type": "routing_redistribution",
            "site": "Data-Center",
            "targets": {"device_ids": ["dc-core-01"]},
            "redistribution": {
                "from_protocol": "ospf",
                "to_protocol": "bgp",
                "target_process": "65010",
                "route_map": "BR204-RETURN",
                "prefix_list": "BR204-RETURN",
                "prefixes": ["10.204.20.0/24"],
                "route_tag": 20420,
            },
            "reachability_checks": [
                {"source_device": "branch-edge-03", "source_ip": "10.204.20.10", "destination": "10.40.8.25"}
            ],
        },
    }
    remediation = client.post("/api/changes/from-rca", json=remediation_payload)
    require(remediation.status_code == 200, remediation.text)
    remediation_body = remediation.json()
    remediation_change_id = remediation_body["change_id"]
    remediation_intent = remediation_body["intent_path"]
    require(remediation_body["draft_only"] is True, "Rez remediation was not draft-only")
    require(remediation_body["human_approval_required"] is True, "remediation lost approval gate")
    require(remediation_body["change"]["workflow_state"] == "validated", remediation.text)
    require(not any(job.change_id == remediation_change_id for job in store.list_jobs()), "Rez draft auto-queued a write")
    timeline.append({"step": "rez_to_netcode_draft", "status": "passed", "change_id": remediation_change_id, "draft_only": True})

    dry_run = client.post(
        "/api/lab/dry-run",
        json={"intent_path": remediation_intent, "device_id": "dc-core-01", "change_id": remediation_change_id},
    )
    require(dry_run.status_code == 200 and dry_run.json().get("queued") is True, dry_run.text)
    dry_run_job = dry_run.json()["job"]
    sign_and_submit(
        store,
        runner,
        dry_run_job["id"],
        {
            "status": "pass",
            "message": "EOS config session accepted the exact redistribution candidate and aborted without commit.",
            "device_id": "dc-core-01",
            "device_writes": "none",
        },
    )
    require(store.get_change(remediation_change_id).workflow_state == "dry_run_passed", "remediation dry-run failed")

    remediation_self_approval = client.post(
        f"/api/change/{remediation_change_id}/approve",
        json={"approved_by": "marcus"},
    )
    require(remediation_self_approval.status_code == 400, "Marcus self-approved the remediation")
    remediation_approval = client.post(
        f"/api/change/{remediation_change_id}/approve",
        json={"approved_by": "syed"},
    )
    require(remediation_approval.status_code == 200, remediation_approval.text)

    apply_response = client.post(
        "/api/lab/apply",
        json={"intent_path": remediation_intent, "device_id": "dc-core-01", "change_id": remediation_change_id},
    )
    require(apply_response.status_code == 200 and apply_response.json().get("queued") is True, apply_response.text)
    apply_job = apply_response.json()["job"]
    sign_and_submit(
        store,
        runner,
        apply_job["id"],
        {
            "status": "pass",
            "message": "Applied scoped OSPF-to-BGP redistribution for 10.204.20.0/24.",
            "device_id": "dc-core-01",
            "commands": [
                "ip prefix-list BR204-RETURN seq 10 permit 10.204.20.0/24",
                "route-map BR204-RETURN permit 10",
                "router bgp 65010 ; redistribute ospf route-map BR204-RETURN",
            ],
            "rollback": "Remove the exact redistribution statement, route-map, and prefix list.",
            "device_touched": True,
        },
    )
    require(store.get_change(remediation_change_id).workflow_state == "rollback_available", "remediation apply not recorded")
    timeline.append(
        {
            "step": "human_approved_remediation",
            "status": "passed",
            "dry_run_job": dry_run_job["id"],
            "apply_job": apply_job["id"],
            "self_approval_blocked": True,
            "approved_by": "syed",
        }
    )

    passing_checks = exact_flow_checks(required_checks, return_route_passes=True)
    passing_evidence_job = queue_exact_flow_result(store, runner, change_id, passing_checks)
    final_verify = client.post(
        f"/api/cross-domain/plans/{change_id}/verify",
        json={"manager_job_id": deploy_job["id"], "evidence_job_ids": [passing_evidence_job]},
    )
    require(final_verify.status_code == 200, final_verify.text)
    final_body = final_verify.json()
    require(final_body["service_assurance"]["status"] == "verified", "service did not verify after remediation")
    require(store.get_change(change_id).workflow_state == "completed", "cross-domain change did not complete")
    timeline.append(
        {
            "step": "service_verify_after_fix",
            "status": "verified",
            "application_flow": "10.204.20.10 > 10.40.8.25/tcp/443",
            "signed_runner_job": passing_evidence_job,
        }
    )

    manager_events = [event for event in store.list_workflow_events(change_id)]
    remediation_events = [event for event in store.list_workflow_events(remediation_change_id)]
    return {
        "scenario": "Marcus cross-domain firewall success / application failure / routing remediation",
        "mode": "controlled contract and signed-runner execution",
        "live_manager_lab": False,
        "result": "passed",
        "cross_domain_change_id": change_id,
        "remediation_change_id": remediation_change_id,
        "timeline": timeline,
        "safety": {
            "credentials_in_control_plane_jobs": False,
            "browser_evidence_spoof_blocked": True,
            "requester_self_approval_blocked": True,
            "rez_read_only": True,
            "human_approval_before_writes": True,
            "manager_success_not_service_success": True,
        },
        "audit": {
            "cross_domain_event_actions": [event.action for event in manager_events],
            "remediation_event_actions": [event.action for event in remediation_events],
            "final_service_assurance": final_body["service_assurance"],
        },
    }


def markdown_report(result: dict[str, Any]) -> str:
    rows = "\n".join(
        f"| {index} | {item['step']} | {item['status']} |"
        for index, item in enumerate(result["timeline"], start=1)
    )
    return f"""# Marcus Cross-Domain Change Assurance Result

- Result: **{result['result'].upper()}**
- Execution mode: {result['mode']}
- Live manager lab: **No**
- Cross-domain change: `{result['cross_domain_change_id']}`
- Rez remediation change: `{result['remediation_change_id']}`

| # | User step | Result |
|---:|---|---|
{rows}

## Safety Assertions

- Browser-supplied evidence could not close the service gate.
- The requester could not approve either change.
- The manager success remained distinct from application success.
- The failed verification produced a read-only Rez handoff.
- The Rez finding created a draft only; no job was queued automatically.
- Dry-run and second-person approval preceded the remediation write.
- Final completion required fresh signed exact-flow evidence.

## Certification Boundary

This is a deterministic control-plane and signed-runner contract proof in an isolated workspace. It does not certify a real Panorama or FortiManager release. Live-manager certification remains blocked until those manager labs exist.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="marcus-cross-domain-") as tmp:
        result = run_story(Path(tmp).resolve())
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
