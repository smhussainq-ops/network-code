"""Deterministic cross-domain planning and exact-flow service assurance."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from netcode.firewall_managers import ApplicationFlow, FirewallNatChange, FirewallPolicyChange


Domain = Literal["routing", "sdwan", "firewall", "nat", "service"]


class ChangeStep(BaseModel):
    id: str
    domain: Domain
    owner_id: str
    action: str
    satisfies: str
    depends_on: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    compensation: dict[str, Any]
    target_ids: list[str]
    write: bool = False


class VerificationSpec(BaseModel):
    flow: ApplicationFlow
    required_checks: list[Literal[
        "forward_route",
        "return_route",
        "sdwan_selection",
        "manager_intent",
        "installed_policy_match",
        "nat_behavior",
        "application_probe",
    ]]

    @model_validator(mode="after")
    def complete_exact_flow_checks(self) -> "VerificationSpec":
        required = {
            "forward_route",
            "return_route",
            "manager_intent",
            "installed_policy_match",
            "nat_behavior",
            "application_probe",
        }
        if self.flow.expected_sdwan_class:
            required.add("sdwan_selection")
        missing = sorted(required - set(self.required_checks))
        if missing:
            raise ValueError(f"verification is missing exact-flow check(s): {', '.join(missing)}")
        return self


class CrossDomainPlan(BaseModel):
    plan_id: str
    title: str
    requested_by: str
    ticket_id: str
    flow: ApplicationFlow
    steps: list[ChangeStep]
    verification: VerificationSpec
    firewall_policy: FirewallPolicyChange | None = None
    firewall_nat: FirewallNatChange | None = None
    human_approval_required: bool = True
    manager_success_is_service_success: Literal[False] = False

    @model_validator(mode="after")
    def valid_dag(self) -> "CrossDomainPlan":
        if not self.human_approval_required:
            raise ValueError("cross-domain writes always require human approval")
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("cross-domain step IDs must be unique")
        known = set(ids)
        for step in self.steps:
            missing = sorted(set(step.depends_on) - known)
            if missing:
                raise ValueError(f"step {step.id} depends on unknown step(s): {', '.join(missing)}")
            if step.id in step.depends_on:
                raise ValueError(f"step {step.id} cannot depend on itself")
            if step.write and not step.compensation:
                raise ValueError(f"write step {step.id} requires a compensating action")
        self._topological_order()
        if self.firewall_policy and not any(step.domain == "firewall" for step in self.steps):
            raise ValueError("firewall policy intent requires a firewall plan step")
        if self.firewall_nat and not any(step.domain == "nat" for step in self.steps):
            raise ValueError("firewall NAT intent requires a NAT plan step")
        return self

    def _topological_order(self) -> list[str]:
        dependencies = {step.id: set(step.depends_on) for step in self.steps}
        order: list[str] = []
        while dependencies:
            ready = sorted(step_id for step_id, needs in dependencies.items() if not needs)
            if not ready:
                raise ValueError("cross-domain plan contains a dependency cycle")
            order.extend(ready)
            for step_id in ready:
                dependencies.pop(step_id)
            for needs in dependencies.values():
                needs.difference_update(ready)
        return order

    @property
    def execution_order(self) -> list[str]:
        return self._topological_order()


class CheckEvidence(BaseModel):
    check: str
    status: Literal["pass", "fail", "unknown"]
    fresh: bool
    flow_key: str
    source: str
    observed: Any = None
    expected: Any = None
    evidence_refs: list[str] = Field(default_factory=list)


class ServiceAssuranceResult(BaseModel):
    status: Literal["verified", "failed", "unknown"]
    manager_push_status: Literal["success", "failed", "unknown"]
    service_status: Literal["success", "failed", "unknown"]
    failed_domain: Domain | None = None
    checks: list[CheckEvidence]
    blockers: list[str] = Field(default_factory=list)
    rca_handoff: dict[str, Any] | None = None


def flow_key(flow: ApplicationFlow) -> str:
    port = str(flow.destination_port) if flow.destination_port else "-"
    return f"{flow.source_ip}>{flow.destination_ip}/{flow.protocol}/{port}"


def build_cross_domain_plan(
    *,
    title: str,
    requested_by: str,
    ticket_id: str,
    flow: ApplicationFlow,
    routing_owner: str,
    sdwan_owner: str | None,
    firewall_policy: FirewallPolicyChange,
    firewall_nat: FirewallNatChange | None = None,
) -> CrossDomainPlan:
    material = json.dumps(
        {
            "title": title,
            "ticket_id": ticket_id,
            "flow": flow.model_dump(),
            "policy": firewall_policy.model_dump(),
            "nat": firewall_nat.model_dump() if firewall_nat else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_id = f"XDOM-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12].upper()}"
    steps = [
        ChangeStep(
            id="route-ready",
            domain="routing",
            owner_id=routing_owner,
            action="ensure exact forward and return routes",
            satisfies="The application prefixes are reachable in both directions.",
            preconditions=["approved route intent", "fresh LPM evidence"],
            stop_conditions=["unresolved next hop", "unexpected route owner"],
            compensation={"action": "restore captured routing pre-state"},
            target_ids=[routing_owner],
            write=True,
        )
    ]
    previous = "route-ready"
    if sdwan_owner:
        steps.append(
            ChangeStep(
                id="sdwan-ready",
                domain="sdwan",
                owner_id=sdwan_owner,
                action="validate or apply application-class steering",
                satisfies="The exact flow selects an eligible SD-WAN member.",
                depends_on=[previous],
                preconditions=["fresh SLA evidence", "eligible member exists"],
                stop_conditions=["all members failed", "policy ambiguity"],
                compensation={"action": "restore captured SD-WAN rule"},
                target_ids=[sdwan_owner],
                write=True,
            )
        )
        previous = "sdwan-ready"
    steps.append(
        ChangeStep(
            id="firewall-policy",
            domain="firewall",
            owner_id=firewall_policy.ownership.manager_id,
            action="manager-native policy and object transaction",
            satisfies="The exact source, destination, application, and service are permitted.",
            depends_on=[previous],
            preconditions=["manager capability probe", "isolated candidate", "preview and validation pass"],
            stop_conditions=["manager lock conflict", "unrelated candidate changes", "shadowed rule", "preview failure"],
            compensation={"action": "restore manager pre-change revision"},
            target_ids=firewall_policy.target_device_ids,
            write=True,
        )
    )
    previous = "firewall-policy"
    if firewall_nat:
        steps.append(
            ChangeStep(
                id="firewall-nat",
                domain="nat",
                owner_id=firewall_nat.ownership.manager_id,
                action="manager-native NAT transaction",
                satisfies="Translation behavior matches the application-flow design.",
                depends_on=[previous],
                preconditions=["resolved NAT objects", "return path proven"],
                stop_conditions=["translation ambiguity", "policy deny", "unknown return route"],
                compensation={"action": "restore manager pre-change revision"},
                target_ids=firewall_nat.target_device_ids,
                write=True,
            )
        )
        previous = "firewall-nat"
    steps.append(
        ChangeStep(
            id="service-verify",
            domain="service",
            owner_id="rez",
            action="independent exact-flow verification",
            satisfies="The application works from the intended source context.",
            depends_on=[previous],
            preconditions=["fresh read-only evidence"],
            stop_conditions=["any required check fails or remains unknown"],
            compensation={"action": "auto-halt and open scoped Rez investigation"},
            target_ids=[flow.source_device],
            write=False,
        )
    )
    checks = [
        "forward_route",
        "return_route",
        "manager_intent",
        "installed_policy_match",
        "nat_behavior",
        "application_probe",
    ]
    if flow.expected_sdwan_class:
        checks.append("sdwan_selection")
    return CrossDomainPlan(
        plan_id=plan_id,
        title=title,
        requested_by=requested_by,
        ticket_id=ticket_id,
        flow=flow,
        steps=steps,
        verification=VerificationSpec(flow=flow, required_checks=checks),
        firewall_policy=firewall_policy,
        firewall_nat=firewall_nat,
    )

_CHECK_DOMAIN: dict[str, Domain] = {
    "forward_route": "routing",
    "return_route": "routing",
    "sdwan_selection": "sdwan",
    "manager_intent": "firewall",
    "installed_policy_match": "firewall",
    "nat_behavior": "nat",
    "application_probe": "service",
}


def evaluate_service_assurance(
    plan: CrossDomainPlan,
    *,
    manager_push_status: Literal["success", "failed", "unknown"],
    evidence: list[CheckEvidence],
) -> ServiceAssuranceResult:
    expected_key = flow_key(plan.flow)
    by_check = {item.check: item for item in evidence if item.flow_key == expected_key}
    blockers: list[str] = []
    ordered = plan.verification.required_checks
    for check in ordered:
        item = by_check.get(check)
        if item is None:
            blockers.append(f"missing exact-flow evidence: {check}")
        elif not item.fresh:
            blockers.append(f"stale exact-flow evidence: {check}")
        elif item.status == "unknown":
            blockers.append(f"inconclusive exact-flow evidence: {check}")

    failed = [by_check[check] for check in ordered if check in by_check and by_check[check].fresh and by_check[check].status == "fail"]
    if manager_push_status == "failed":
        failed_domain: Domain | None = "firewall"
    elif failed:
        failed_domain = _CHECK_DOMAIN.get(failed[0].check, "service")
    else:
        failed_domain = None

    if failed_domain or blockers:
        service_status: Literal["success", "failed", "unknown"] = "failed" if failed_domain else "unknown"
        status: Literal["verified", "failed", "unknown"] = "failed" if failed_domain else "unknown"
    else:
        service_status = "success"
        status = "verified"

    handoff = None
    if status == "failed":
        handoff = {
            "incident_type": "cross_domain_verification_failure",
            "plan_id": plan.plan_id,
            "flow": plan.flow.model_dump(),
            "failed_domain": failed_domain,
            "manager_push_status": manager_push_status,
            "checks": [item.model_dump() for item in evidence],
            "read_only": True,
            "human_approval_required_for_remediation": True,
        }
    return ServiceAssuranceResult(
        status=status,
        manager_push_status=manager_push_status,
        service_status=service_status,
        failed_domain=failed_domain,
        checks=evidence,
        blockers=blockers,
        rca_handoff=handoff,
    )
