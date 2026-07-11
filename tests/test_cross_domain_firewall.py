from __future__ import annotations

import pytest
from pydantic import ValidationError

from netcode.cross_domain import (
    ChangeStep,
    CheckEvidence,
    CrossDomainPlan,
    VerificationSpec,
    build_cross_domain_plan,
    evaluate_service_assurance,
    flow_key,
)
from netcode.cross_domain_runner import collect_exact_flow_evidence
from netcode.firewall_managers import (
    ApplicationFlow,
    ApprovalProof,
    FirewallObjectRef,
    FirewallNatChange,
    FirewallPolicyChange,
    ManagerCapabilities,
    ManagerJobRequest,
    ManagerOwnership,
    ManagerScope,
    capabilities_from_probe,
    validate_unique_ownership,
)
from netcode.inventory import Inventory
from netcode.manager_execution import OperationLedger, build_manager_calls, execute_manager_job
from netcode.paths import WorkspacePaths
from netcode.bootstrap import init_workspace
from netcode.store import PlatformStore
from netcode.runner_hub import sign_result, submit_job_result


def _ownership(manager_type: str = "panorama") -> ManagerOwnership:
    if manager_type == "fortimanager":
        scope = ManagerScope(
            adom="branches",
            policy_package="branch-egress",
            vdom="root",
            install_target="branch-fw-03",
        )
    else:
        scope = ManagerScope(
            device_group="branch-firewalls",
            template_stack="branch-standard",
            vsys="vsys1",
            rulebase="pre",
        )
    return ManagerOwnership(
        device_id="branch-fw-03",
        manager_id="manager-prod-01",
        manager_type=manager_type,
        scope=scope,
        managed_serial="0123456789",
    )


def _capabilities(manager_type: str = "panorama") -> ManagerCapabilities:
    return capabilities_from_probe(
        manager_type,
        "11.1.4" if manager_type == "panorama" else "7.4.3",
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
            "partial_install": manager_type == "fortimanager",
        },
    )


def _flow() -> ApplicationFlow:
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


def _object(name: str, value: str, kind: str) -> FirewallObjectRef:
    return FirewallObjectRef(name=name, value=value, kind=kind)


def _policy(manager_type: str = "panorama") -> FirewallPolicyChange:
    ownership = _ownership(manager_type)
    return FirewallPolicyChange(
        name="allow-branch204-app",
        ownership=ownership,
        source_zones=["branch-trust"],
        destination_zones=["dc-app"],
        source_objects=[_object("BRANCH204-USERS", "10.204.20.0/24", "address")],
        destination_objects=[_object("DC-APP-25", "10.40.8.25/32", "address")],
        services=[_object("TCP-443", "tcp/443", "service")],
        applications=[_object("ssl", "ssl", "application")],
        action="allow",
        security_profiles=["strict-default"],
        insertion={"position": "before", "reference_rule": "branch-default-deny"},
        target_device_ids=[ownership.device_id],
        ticket_id="CHG-2048",
    )


def _nat(manager_type: str = "panorama") -> FirewallNatChange:
    ownership = _ownership(manager_type)
    return FirewallNatChange(
        name="branch204-snat",
        ownership=ownership,
        nat_type="snat",
        original_source="10.204.20.0/24",
        original_destination="10.40.8.25/32",
        translated_source="198.51.100.20/32",
        service="TCP-443",
        target_device_ids=[ownership.device_id],
        ticket_id="CHG-2048",
    )


def _plan() -> CrossDomainPlan:
    return build_cross_domain_plan(
        title="Enable Branch-204 application access",
        requested_by="marcus",
        ticket_id="CHG-2048",
        flow=_flow(),
        routing_owner="branch-edge-03",
        sdwan_owner="branch-edge-03",
        firewall_policy=_policy(),
    )


def _approval(*, approved: bool = True, same_user: bool = False) -> ApprovalProof:
    return ApprovalProof(
        approved=approved,
        requested_by="marcus",
        approved_by="marcus" if same_user else "syed",
        workflow_state="approved" if approved else "dry_run_passed",
    )


def _job(action: str = "deploy", **overrides) -> dict:
    policy = _policy()
    values = {
        "action": action,
        "operation_id": "op-2048-deploy",
        "change_id": "REZ-CHG-2048",
        "manager_id": policy.ownership.manager_id,
        "ownership": policy.ownership,
        "capabilities": _capabilities(),
        "policy_change": policy,
        "flow": _flow(),
        "approval": _approval(),
        "expected_candidate_owner": "marcus",
        "expected_candidate_location": "branch-firewalls/pre",
    }
    values.update(overrides)
    return values


def _evidence(check: str, status: str = "pass", *, fresh: bool = True, key: str | None = None) -> CheckEvidence:
    return CheckEvidence(
        check=check,
        status=status,
        fresh=fresh,
        flow_key=key or flow_key(_flow()),
        source="runner-live",
        observed=status,
        expected="pass",
        evidence_refs=[f"live:{check}"],
    )


def test_manager_scope_requires_exact_owner_fields():
    with pytest.raises(ValidationError, match="missing scope field"):
        ManagerOwnership(
            device_id="branch-fw-03",
            manager_id="manager-prod-01",
            manager_type="fortimanager",
            scope={"adom": "branches"},
            managed_serial="0123456789",
        )


def test_conflicting_manager_ownership_fails_closed():
    first = _ownership()
    second = first.model_copy(update={"manager_id": "manager-prod-02"})
    with pytest.raises(ValueError, match="conflicting manager ownership"):
        validate_unique_ownership([first, second])


def test_version_never_implies_unproven_write_capability():
    capabilities = capabilities_from_probe("panorama", "99.9.9", {"read": True})
    with pytest.raises(ValueError, match="live capability probe did not prove"):
        capabilities.require("deploy")


def test_manager_write_requires_second_engineer_approval():
    with pytest.raises(ValidationError, match="approved workflow"):
        ManagerJobRequest.model_validate(_job(approval=_approval(approved=False)))
    with pytest.raises(ValidationError, match="requester cannot approve"):
        ManagerJobRequest.model_validate(_job(approval=_approval(same_user=True)))


def test_read_only_manager_probe_does_not_require_approval():
    request = ManagerJobRequest.model_validate(
        _job(
            action="probe",
            operation_id="op-2048-probe",
            approval=_approval(approved=False),
            policy_change=None,
        )
    )
    assert request.action == "probe"


def test_unrelated_manager_candidate_changes_block_deploy():
    with pytest.raises(ValidationError, match="unrelated changes"):
        ManagerJobRequest.model_validate(
            _job(unrelated_candidate_changes=[{"administrator": "other-admin", "xpath": "/shared/address"}])
        )


def test_credentials_are_forbidden_in_control_plane_job_payload():
    payload = _job()
    payload["password"] = "must-never-leave-runner"
    with pytest.raises(ValidationError, match="credential-shaped field"):
        ManagerJobRequest.model_validate(payload)


def test_firewall_policy_cannot_degrade_to_ambiguous_generic_config():
    payload = _policy().model_dump()
    payload["source_objects"] = []
    with pytest.raises(ValidationError, match="requires resolved source"):
        FirewallPolicyChange.model_validate(payload)
    payload = _policy().model_dump()
    payload["insertion"] = {}
    with pytest.raises(ValidationError, match="exact position"):
        FirewallPolicyChange.model_validate(payload)


def test_cross_domain_plan_is_ordered_and_every_write_has_compensation():
    plan = _plan()
    assert plan.execution_order == ["route-ready", "sdwan-ready", "firewall-policy", "service-verify"]
    assert plan.manager_success_is_service_success is False
    assert all(step.compensation for step in plan.steps if step.write)


def test_cross_domain_plan_rejects_dependency_cycles():
    steps = [
        ChangeStep(
            id="a", domain="routing", owner_id="r1", action="a", satisfies="a",
            depends_on=["b"], compensation={"undo": "a"}, target_ids=["r1"], write=True,
        ),
        ChangeStep(
            id="b", domain="firewall", owner_id="f1", action="b", satisfies="b",
            depends_on=["a"], compensation={"undo": "b"}, target_ids=["f1"], write=True,
        ),
    ]
    with pytest.raises(ValidationError, match="dependency cycle"):
        CrossDomainPlan(
            plan_id="XDOM-CYCLE",
            title="cycle",
            requested_by="marcus",
            ticket_id="CHG-CYCLE",
            flow=_flow(),
            steps=steps,
            verification=VerificationSpec(
                flow=_flow(),
                required_checks=[
                    "forward_route", "return_route", "sdwan_selection", "manager_intent",
                    "installed_policy_match", "nat_behavior", "application_probe",
                ],
            ),
        )


def test_manager_success_plus_missing_route_identifies_routing_not_firewall():
    checks = [_evidence(check) for check in _plan().verification.required_checks]
    checks = [item.model_copy(update={"status": "fail"}) if item.check == "forward_route" else item for item in checks]
    result = evaluate_service_assurance(_plan(), manager_push_status="success", evidence=checks)
    assert result.status == "failed"
    assert result.failed_domain == "routing"
    assert result.manager_push_status == "success"
    assert result.rca_handoff["read_only"] is True


def test_manager_success_alone_never_marks_service_verified():
    result = evaluate_service_assurance(
        _plan(),
        manager_push_status="success",
        evidence=[_evidence("manager_intent"), _evidence("installed_policy_match")],
    )
    assert result.status == "unknown"
    assert result.service_status == "unknown"
    assert "missing exact-flow evidence: application_probe" in result.blockers


def test_stale_or_off_flow_evidence_cannot_satisfy_verification():
    checks = [_evidence(check) for check in _plan().verification.required_checks]
    checks[0] = _evidence(checks[0].check, key="10.1.1.1>10.2.2.2/tcp/443")
    checks[1] = _evidence(checks[1].check, fresh=False)
    result = evaluate_service_assurance(_plan(), manager_push_status="success", evidence=checks)
    assert result.status == "unknown"
    assert any("missing exact-flow evidence" in blocker for blocker in result.blockers)
    assert any("stale exact-flow evidence" in blocker for blocker in result.blockers)


def test_first_failed_dependency_wins_over_downstream_noise():
    checks = [_evidence(check) for check in _plan().verification.required_checks]
    failed = {"forward_route", "nat_behavior", "application_probe"}
    checks = [item.model_copy(update={"status": "fail"}) if item.check in failed else item for item in checks]
    result = evaluate_service_assurance(_plan(), manager_push_status="success", evidence=checks)
    assert result.failed_domain == "routing"


def test_all_fresh_exact_flow_checks_are_required_for_service_success():
    checks = [_evidence(check) for check in _plan().verification.required_checks]
    result = evaluate_service_assurance(_plan(), manager_push_status="success", evidence=checks)
    assert result.status == "verified"
    assert result.service_status == "success"
    assert result.failed_domain is None
    assert result.rca_handoff is None


def test_runner_inventory_separates_public_manager_ownership_from_local_credentials(tmp_path):
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        """
devices:
  - id: panorama-prod-01
    host: 192.0.2.10
    platform: panorama
    username: runner-local
    password: runner-local-password
    api:
      manager_type: panorama
      api_key: runner-local-key
      verify_ssl: true
  - id: branch-fw-03
    host: 192.0.2.33
    platform: palo_alto
    management:
      management_mode: manager
      manager_id: panorama-prod-01
      manager_type: panorama
      managed_serial: "0123456789"
      scope:
        device_group: branch-firewalls
        template_stack: branch-standard
        vsys: vsys1
        rulebase: pre
""".strip(),
        encoding="utf-8",
    )
    inventory = Inventory(inventory_path)
    manager = inventory.find_device("panorama-prod-01")
    firewall = inventory.find_device("branch-fw-03")
    assert manager.connection_options["api_key"] == "runner-local-key"
    assert firewall.management["manager_id"] == "panorama-prod-01"
    assert "api_key" not in str(firewall.management)
    assert "runner-local-password" not in repr(manager)


def test_public_catalog_persists_manager_scope_without_secrets(tmp_path):
    store = PlatformStore(WorkspacePaths(tmp_path))
    runner = store.create_runner("branch-runner", "branch", "token-hash", "hmac-secret")
    ownership = _ownership().public_dict()
    result = store.sync_runner_devices(
        runner,
        [{
            "id": "branch-fw-03",
            "hostname": "branch-fw-03",
            "host": "192.0.2.33",
            "port": 22,
            "platform": "palo_alto",
            "site": "Branch-204",
            "role": "firewall",
            "groups": ["branch"],
            "aliases": [],
            "management": ownership,
        }],
        revision="rev-1",
    )
    assert result["device_count"] == 1
    record = store.resolve_device(runner.org_id, "branch-fw-03")
    assert record["management"]["scope"]["device_group"] == "branch-firewalls"
    assert "password" not in str(record).lower()
    assert "api_key" not in str(record).lower()


def test_public_catalog_rejects_secret_or_mismatched_manager_ownership(tmp_path):
    store = PlatformStore(WorkspacePaths(tmp_path))
    runner = store.create_runner("branch-runner", "branch", "token-hash", "hmac-secret")
    ownership = _ownership().public_dict()
    ownership["api_token"] = "must-not-enter-saas"
    with pytest.raises(ValueError, match="credential-shaped field"):
        store.sync_runner_devices(
            runner,
            [{"id": "branch-fw-03", "host": "192.0.2.33", "management": ownership}],
            revision="rev-secret",
        )
    ownership = _ownership().public_dict()
    ownership["device_id"] = "another-firewall"
    with pytest.raises(ValueError, match="does not match catalog device"):
        store.sync_runner_devices(
            runner,
            [{"id": "branch-fw-03", "host": "192.0.2.33", "management": ownership}],
            revision="rev-mismatch",
        )


class _FakeManagerAdapter:
    def __init__(self, *, capabilities=None, fail_call: str = "", candidate_scope=None):
        self.capabilities = capabilities or _capabilities()
        self.fail_call = fail_call
        self.calls = []
        self._candidate_scope = candidate_scope or {
            "proven_isolated": True,
            "changes": [{"owner": "marcus", "location": "branch-firewalls/pre"}],
        }

    def probe(self):
        return self.capabilities, {"status": "live", "version": self.capabilities.version}

    def execute(self, call):
        self.calls.append(call)
        return {"ok": call.name != self.fail_call, "task_id": f"task-{len(self.calls)}"}

    def candidate_scope(self, request):
        return self._candidate_scope


def _manager_inventory(tmp_path, *, manager_type="panorama", ownership=None):
    ownership = ownership or _ownership(manager_type)
    manager_platform = manager_type
    scope = ownership.scope.model_dump(exclude_none=True)
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        "devices:\n"
        f"  - id: {ownership.manager_id}\n"
        "    host: 192.0.2.10\n"
        f"    platform: {manager_platform}\n"
        "    username: runner-only\n"
        "    password: runner-only-password\n"
        "    api:\n"
        f"      manager_type: {manager_type}\n"
        "      api_key: runner-only-key\n"
        "      manager_capabilities:\n"
        "        read: true\n"
        f"  - id: {ownership.device_id}\n"
        "    host: 192.0.2.33\n"
        "    platform: palo_alto\n"
        "    management:\n"
        "      management_mode: manager\n"
        f"      manager_id: {ownership.manager_id}\n"
        f"      manager_type: {manager_type}\n"
        f"      managed_serial: \"{ownership.managed_serial}\"\n"
        "      scope:\n"
        + "".join(f"        {key}: {value}\n" for key, value in scope.items()),
        encoding="utf-8",
    )
    return inventory_path


def test_manager_call_generation_is_manager_native_and_exactly_scoped():
    panorama = ManagerJobRequest.model_validate(_job(action="stage", operation_id="op-stage-pan"))
    calls = build_manager_calls(panorama)
    assert [call.name for call in calls] == ["stage-policy", "position-policy"]
    assert calls[0].write is True
    assert "branch-firewalls" in calls[0].params["xpath"]
    assert "allow-branch204-app" in calls[0].params["element"]

    policy = _policy("fortimanager")
    fortimanager = ManagerJobRequest.model_validate(
        _job(
            action="deploy",
            operation_id="op-deploy-fmg",
            manager_id=policy.ownership.manager_id,
            ownership=policy.ownership,
            capabilities=_capabilities("fortimanager"),
            policy_change=policy,
        )
    )
    calls = build_manager_calls(fortimanager)
    assert calls[0].path == "/securityconsole/install/package"
    assert calls[0].body["pkg"] == "branch-egress"
    assert calls[0].body["scope"] == [{"name": "branch-fw-03", "vdom": "root"}]


def test_manager_stage_includes_required_objects_rule_position_and_nat():
    policy_data = _policy().model_dump()
    policy_data["source_objects"][0]["create_if_missing"] = True
    policy_data["destination_objects"][0]["create_if_missing"] = True
    policy_data["services"][0]["create_if_missing"] = True
    policy = FirewallPolicyChange.model_validate(policy_data)
    request = ManagerJobRequest.model_validate(
        _job(
            action="stage",
            operation_id="op-stage-dependencies",
            policy_change=policy,
            nat_change=_nat(),
        )
    )
    names = [call.name for call in build_manager_calls(request)]
    assert names == [
        "stage-address:BRANCH204-USERS",
        "stage-address:DC-APP-25",
        "stage-service:TCP-443",
        "stage-policy",
        "position-policy",
        "stage-nat",
    ]


def test_runner_executes_manager_write_only_after_local_ownership_and_capability_checks(tmp_path):
    inventory_path = _manager_inventory(tmp_path)
    adapter = _FakeManagerAdapter()
    result = execute_manager_job(
        _job(action="stage", operation_id="op-stage-1"),
        inventory_path=inventory_path,
        ledger_path=tmp_path / "manager-ledger.json",
        adapter=adapter,
    )
    assert result["status"] == "pass"
    assert result["credentials_leave_runner"] is False
    assert [call.name for call in adapter.calls] == ["stage-policy", "position-policy"]


def test_runner_rejects_local_ownership_drift(tmp_path):
    local_ownership = _ownership().model_copy(update={"manager_id": "manager-prod-02"})
    inventory_path = _manager_inventory(tmp_path, ownership=local_ownership)
    with pytest.raises(ValueError, match="manager .* not in runner-local inventory"):
        execute_manager_job(
            _job(action="stage", operation_id="op-stage-owner-drift"),
            inventory_path=inventory_path,
            ledger_path=tmp_path / "manager-ledger.json",
            adapter=_FakeManagerAdapter(),
        )


def test_runner_live_capability_downgrade_blocks_write(tmp_path):
    inventory_path = _manager_inventory(tmp_path)
    adapter = _FakeManagerAdapter(capabilities=capabilities_from_probe("panorama", "11.1.4", {"read": True}))
    with pytest.raises(ValueError, match="live capability probe did not prove"):
        execute_manager_job(
            _job(action="deploy", operation_id="op-deploy-no-capability"),
            inventory_path=inventory_path,
            ledger_path=tmp_path / "manager-ledger.json",
            adapter=adapter,
        )
    assert adapter.calls == []


def test_manager_partial_failure_stops_remaining_calls_and_preserves_evidence(tmp_path):
    inventory_path = _manager_inventory(tmp_path)
    adapter = _FakeManagerAdapter(fail_call="commit-panorama")
    result = execute_manager_job(
        _job(action="deploy", operation_id="op-deploy-partial"),
        inventory_path=inventory_path,
        ledger_path=tmp_path / "manager-ledger.json",
        adapter=adapter,
    )
    assert result["status"] == "fail"
    assert [call.name for call in adapter.calls] == ["commit-panorama"]
    assert len(result["calls"]) == 1
    assert result["calls"][0]["result"]["ok"] is False


def test_runner_blocks_unproven_or_out_of_scope_candidate(tmp_path):
    inventory_path = _manager_inventory(tmp_path)
    unproven = _FakeManagerAdapter(candidate_scope={"proven_isolated": False, "message": "cannot prove isolation"})
    with pytest.raises(ValueError, match="cannot prove isolation"):
        execute_manager_job(
            _job(action="stage", operation_id="op-stage-unproven"),
            inventory_path=inventory_path,
            ledger_path=tmp_path / "manager-ledger.json",
            adapter=unproven,
        )
    wrong_owner = _FakeManagerAdapter(candidate_scope={
        "proven_isolated": True,
        "changes": [{"owner": "other-admin", "location": "branch-firewalls/pre"}],
    })
    with pytest.raises(ValueError, match="outside the reviewed"):
        execute_manager_job(
            _job(action="deploy", operation_id="op-deploy-other-admin"),
            inventory_path=inventory_path,
            ledger_path=tmp_path / "manager-ledger.json",
            adapter=wrong_owner,
        )


def test_manager_operation_is_idempotent_and_conflicting_reuse_is_rejected(tmp_path):
    inventory_path = _manager_inventory(tmp_path)
    adapter = _FakeManagerAdapter()
    ledger_path = tmp_path / "manager-ledger.json"
    payload = _job(action="stage", operation_id="op-stage-replay")
    first = execute_manager_job(payload, inventory_path=inventory_path, ledger_path=ledger_path, adapter=adapter)
    second = execute_manager_job(payload, inventory_path=inventory_path, ledger_path=ledger_path, adapter=adapter)
    assert first["status"] == "pass"
    assert second["replayed"] is True
    assert len(adapter.calls) == 2

    changed = _job(action="stage", operation_id="op-stage-replay")
    changed["change_id"] = "REZ-CHG-DIFFERENT"
    with pytest.raises(ValueError, match="already used for a different"):
        execute_manager_job(changed, inventory_path=inventory_path, ledger_path=ledger_path, adapter=adapter)


def test_runner_local_ledger_permissions_are_private(tmp_path):
    path = tmp_path / "ledger.json"
    OperationLedger(path).store("op-1", "key-1", {"ok": True})
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_runner_dispatches_manager_jobs_before_generic_cli_render(monkeypatch, tmp_path):
    from netcode import manager_execution, runner_agent

    captured = {}

    def fake_execute(payload, *, inventory_path, ledger_path, adapter=None):
        captured.update({"payload": payload, "inventory_path": inventory_path, "ledger_path": ledger_path})
        return {"status": "pass", "action": payload["action"]}

    monkeypatch.setattr(manager_execution, "execute_manager_job", fake_execute)
    monkeypatch.setattr(runner_agent, "INVENTORY_FILE", tmp_path / "inventory.yaml")
    monkeypatch.setattr(runner_agent, "MANAGER_LEDGER_FILE", tmp_path / "ledger.json")
    result = runner_agent._execute_job({"action": "manager_probe", "payload": {"change_id": "change-1"}})
    assert result == {"status": "pass", "action": "probe"}
    assert captured["payload"]["action"] == "probe"
    assert captured["inventory_path"] == tmp_path / "inventory.yaml"


def _sync_manager_catalog(store, runner):
    ownership = _ownership().public_dict()
    return store.sync_runner_devices(
        runner,
        [
            {
                "id": "manager-prod-01",
                "hostname": "manager-prod-01",
                "host": "192.0.2.10",
                "port": 443,
                "platform": "panorama",
                "site": "control",
                "role": "manager",
            },
            {
                "id": "branch-fw-03",
                "hostname": "branch-fw-03",
                "host": "192.0.2.33",
                "port": 22,
                "platform": "palo_alto",
                "site": "Branch-204",
                "role": "firewall",
                "management": ownership,
            },
        ],
        revision="manager-rev-1",
    )


def _api_plan_payload():
    return {
        "title": "Enable Branch-204 application access",
        "requested_by": "marcus",
        "ticket_id": "CHG-2048",
        "flow": _flow().model_dump(mode="json"),
        "routing_owner": "branch-edge-03",
        "sdwan_owner": "branch-edge-03",
        "firewall_policy": _policy().model_dump(mode="json"),
    }


def _manager_action_body(operation_id):
    return {"capabilities": _capabilities().model_dump(mode="json"), "operation_id": operation_id}


def _setup_api_workspace(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from netcode import api

    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    runner = store.create_runner("branch-runner", "branch", "runner-token-hash", "runner-hmac")
    _sync_manager_catalog(store, runner)
    return TestClient(api.app), store, runner


def _submit_signed_jobs(store, runner, change_id, checks, *, manager_ok=True):
    manager_job = store.queue_job(
        change_id,
        "manager_deploy",
        runner.pool,
        {"action": "deploy", "change_id": change_id},
        target_runner_id=runner.id,
    )
    claimed = store.claim_next_job(runner.org_id, runner.pool, runner.id)
    assert claimed.id == manager_job.id
    manager_result = {
        "status": "pass" if manager_ok else "fail",
        "message": "manager deploy finished",
    }
    submit_job_result(store, runner, manager_job.id, manager_result, sign_result("runner-hmac", manager_result))

    read_job = store.create_read_job(
        runner.org_id,
        runner.pool,
        "cross_domain_verify",
        {"change_id": change_id},
        target_runner_id=runner.id,
    )
    claimed = store.claim_next_job(runner.org_id, runner.pool, runner.id)
    assert claimed.id == read_job.id
    read_result = {
        "ok": True,
        "status": "pass",
        "change_id": change_id,
        "service_checks": checks,
        "message": "exact-flow evidence collected",
    }
    submit_job_result(store, runner, read_job.id, read_result, sign_result("runner-hmac", read_result))
    return manager_job.id, read_job.id


def test_cross_domain_api_plan_is_plan_only_and_preview_queues_to_exact_runner(tmp_path, monkeypatch):
    client, store, runner = _setup_api_workspace(tmp_path, monkeypatch)
    created = client.post("/api/cross-domain/plans", json=_api_plan_payload())
    assert created.status_code == 200, created.text
    body = created.json()
    change_id = body["change"]["id"]
    assert body["change"]["workflow_state"] == "validated"
    assert store.list_jobs() == []
    assert body["plan"]["manager_success_is_service_success"] is False

    queued = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/preview",
        json=_manager_action_body("op-preview-api"),
    )
    assert queued.status_code == 200, queued.text
    job = queued.json()["job"]
    assert job["action"] == "manager_preview"
    assert job["target_runner_id"] == runner.id
    assert "password" not in str(job["payload"]).lower()
    assert "api_key" not in str(job["payload"]).lower()


def test_manager_write_cannot_be_queued_before_durable_approval(tmp_path, monkeypatch):
    client, store, _ = _setup_api_workspace(tmp_path, monkeypatch)
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    blocked = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/deploy",
        json=_manager_action_body("op-deploy-before-approval"),
    )
    assert blocked.status_code == 400
    assert "approved workflow" in blocked.json()["detail"]
    assert store.list_jobs() == []


def test_preview_signed_result_then_second_engineer_approval_unlocks_manager_stage(tmp_path, monkeypatch):
    client, store, runner = _setup_api_workspace(tmp_path, monkeypatch)
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    queued = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/preview",
        json=_manager_action_body("op-preview-proof"),
    ).json()
    claimed = store.claim_next_job(runner.org_id, runner.pool, runner.id)
    assert claimed.id == queued["job"]["id"]
    preview_result = {"status": "pass", "message": "Manager preview and isolation checks passed."}
    accepted = submit_job_result(store, runner, claimed.id, preview_result, sign_result("runner-hmac", preview_result))
    assert accepted["workflow_state"] == "dry_run_passed"
    assert store.get_change(change_id).result["plan"]["plan_id"].startswith("XDOM-")

    approved = client.post(f"/api/change/{change_id}/approve", json={"approved_by": "syed"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["change"]["workflow_state"] == "approved"
    staged = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/stage",
        json=_manager_action_body("op-stage-after-approval"),
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["job"]["action"] == "manager_stage"
    assert staged.json()["job"]["payload"]["approval"]["approved_by"] == "syed"


def test_exact_flow_failure_after_manager_success_dispatches_read_only_routing_handoff(tmp_path, monkeypatch):
    client, store, runner = _setup_api_workspace(tmp_path, monkeypatch)
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    checks = [
        _evidence(check).model_dump(mode="json")
        for check in _plan().verification.required_checks
        if check != "manager_intent"
    ]
    for check in checks:
        if check["check"] == "forward_route":
            check["status"] = "fail"
    manager_job_id, evidence_job_id = _submit_signed_jobs(store, runner, change_id, checks)
    response = client.post(
        f"/api/cross-domain/plans/{change_id}/verify",
        json={"manager_job_id": manager_job_id, "evidence_job_ids": [evidence_job_id]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["service_assurance"]["failed_domain"] == "routing"
    assert body["service_assurance"]["manager_push_status"] == "success"
    assert body["diagnostics_handoff"]["context"]["read_only"] is True
    assert body["diagnostics_handoff"]["safety"]["device_writes"] == "none"
    stored = store.get_change(change_id)
    assert stored.workflow_state == "failed"
    assert stored.result["diagnostics_handoffs"][0]["context"]["check"] == "cross_domain_application_flow"


def test_exact_flow_success_is_required_before_cross_domain_change_completes(tmp_path, monkeypatch):
    client, store, runner = _setup_api_workspace(tmp_path, monkeypatch)
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    checks = [
        _evidence(check).model_dump(mode="json")
        for check in _plan().verification.required_checks
        if check != "manager_intent"
    ]
    manager_job_id, evidence_job_id = _submit_signed_jobs(store, runner, change_id, checks)
    response = client.post(
        f"/api/cross-domain/plans/{change_id}/verify",
        json={"manager_job_id": manager_job_id, "evidence_job_ids": [evidence_job_id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["service_assurance"]["status"] == "verified"
    assert store.get_change(change_id).workflow_state == "completed"


def test_browser_cannot_self_assert_cross_domain_evidence(tmp_path, monkeypatch):
    client, _, _ = _setup_api_workspace(tmp_path, monkeypatch)
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    response = client.post(
        f"/api/cross-domain/plans/{change_id}/verify",
        json={"manager_push_status": "success", "evidence": []},
    )
    assert response.status_code == 422


def test_verify_start_routes_exact_flow_collection_to_source_runner(tmp_path, monkeypatch):
    client, store, runner = _setup_api_workspace(tmp_path, monkeypatch)
    store.sync_runner_devices(
        runner,
        [{
            "id": "branch-edge-03", "hostname": "branch-edge-03", "host": "192.0.2.20",
            "port": 22, "platform": "arista_eos", "site": "Branch-204", "role": "edge",
        }],
        revision="source-rev",
        replace=False,
    )
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    response = client.post(f"/api/cross-domain/plans/{change_id}/verify/start")
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["action"] == "read_cross_domain_verify"
    assert job["target_runner_id"] == runner.id
    assert job["payload"]["flow"]["source_ip"] == "10.204.20.10"
    assert "password" not in str(job["payload"]).lower()


def test_runner_exact_flow_collector_uses_lpm_policy_nat_sdwan_and_source_probe():
    source_state = {
        "routing": {"routes": [
            {"prefix": "0.0.0.0/0", "protocol": "static", "next_hop": "10.204.0.1"},
            {"prefix": "10.40.8.0/24", "protocol": "bgp", "next_hop": "10.204.0.2"},
        ]},
        "sdwan": {"sdwan_selections": [{
            "class": "business-critical", "selected_member": "mpls-primary", "healthy": True,
        }]},
    }
    route_owner_state = {"routes": [{"prefix": "10.204.20.0/24", "protocol": "ospf", "next_hop": "10.40.0.1"}]}
    firewall_state = {
        "security": {"policy_lookup": {
            "source_ip": "10.204.20.10",
            "destination_ip": "10.40.8.25",
            "protocol": "tcp",
            "destination_port": 443,
            "action": "allow",
        }},
        "section_status": {"nat_rules": "ok"},
        "nat_rules": [],
    }
    states = {
        "branch-edge-03": source_state,
        "dc-core-01": route_owner_state,
        "branch-fw-03": firewall_state,
    }
    payload = {
        "change_id": "change-1",
        "plan_id": "XDOM-1",
        "flow": _flow().model_dump(mode="json"),
        "required_checks": [
            "forward_route", "return_route", "sdwan_selection", "installed_policy_match",
            "nat_behavior", "application_probe",
        ],
        "devices": {"source": "branch-edge-03", "route_owner": "dc-core-01", "firewall": "branch-fw-03"},
    }
    result = collect_exact_flow_evidence(
        payload,
        collect_state=lambda device_id: {"ok": True, "state": states[device_id]},
        application_probe=lambda flow: {"connected": True, "destination": flow.destination_ip},
    )
    assert result["status"] == "pass"
    assert {row["check"]: row["status"] for row in result["service_checks"]} == {
        "forward_route": "pass",
        "return_route": "pass",
        "sdwan_selection": "pass",
        "installed_policy_match": "pass",
        "nat_behavior": "pass",
        "application_probe": "pass",
    }


def test_manager_rollback_requires_fresh_second_engineer_approval(tmp_path, monkeypatch):
    client, store, runner = _setup_api_workspace(tmp_path, monkeypatch)
    change_id = client.post("/api/cross-domain/plans", json=_api_plan_payload()).json()["change"]["id"]
    checks = [
        _evidence(check).model_dump(mode="json")
        for check in _plan().verification.required_checks
        if check != "manager_intent"
    ]
    _submit_signed_jobs(store, runner, change_id, checks)
    assert store.get_change(change_id).workflow_state == "rollback_available"

    blocked = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/rollback",
        json={**_manager_action_body("op-rollback-blocked"), "pre_change_revision": "42"},
    )
    assert blocked.status_code == 400
    approved = client.post(
        f"/api/cross-domain/plans/{change_id}/approve-rollback",
        json={"approved_by": "syed"},
    )
    assert approved.status_code == 200, approved.text
    queued = client.post(
        f"/api/cross-domain/plans/{change_id}/manager/rollback",
        json={**_manager_action_body("op-rollback-approved"), "pre_change_revision": "42"},
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["job"]["action"] == "manager_rollback"
    assert queued.json()["job"]["payload"]["pre_change_revision"] == "42"
