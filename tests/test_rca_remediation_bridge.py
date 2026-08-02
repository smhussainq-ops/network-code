import copy
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from netcode import api
from netcode.bootstrap import init_workspace
from netcode.network_model import NETWORK_MODEL_SCHEMA
from netcode.network_model_lifecycle import activate_verified_revision, approve_with_git
from netcode.network_model_store import NetworkModelRepository
from netcode.models import RoutingRedistributionOperation
from netcode.paths import WorkspacePaths
from netcode.store import PlatformStore
from netcode.yamlio import read_yaml


def _confirmed_proposal(payload: dict) -> dict:
    return {
        "proposal_schema": "netcode.remediation.v1",
        "proposal_source": "rez_structured_rca",
        "root_confirmed": True,
        "root_atom_id": "CONFIG_EXACT_REMEDIATION_REQUIRED",
        **payload,
    }


def _evidence_scoped_redistribution_proposal() -> dict:
    operation = {
        "op": "add_prefix_list_entry",
        "name": "ENTERPRISE-REMOTE-LOOPBACKS",
        "sequence": 20,
        "action": "permit",
        "prefix": "1.1.1.0/24",
        "le": 32,
    }
    rollback = {
        "op": "remove_prefix_list_entry",
        "name": "ENTERPRISE-REMOTE-LOOPBACKS",
        "sequence": 20,
    }
    proof = {
        "schema": "rez.redistribution-evidence.v1",
        "platform": "arista_eos",
        "site": "campus",
        "boundary_id": "campus-bgp-to-ospf",
        "vrf": "default",
        "device_id": "v2-store1",
        "environment_id": "env-campus",
        "model_revision_id": "campus-approved-v1",
        "root_atom_id": "CP_REDISTRIBUTION_GAP",
        "dependency_id": "design:campus:redistribution:campus-bgp-to-ospf:v2-store1",
        "direction": {
            "from_protocol": "bgp",
            "to_protocol": "ospf",
            "target_process": "1",
        },
        "approved_policy": {
            "route_map": "CAMPUS-BGP-TO-OSPF",
            "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
            "prefixes": ["1.1.1.0/24"],
            "route_tag": 65002,
        },
        "observed_statement": {
            "target_process": "1",
            "route_map": "CAMPUS-BGP-TO-OSPF",
            "statement_sha256": "a" * 64,
        },
        "classification": "prefix_policy_scope_gap",
        "affected_prefixes": ["1.1.1.1/32"],
        "operations": [operation],
        "rollback_operations": [rollback],
        "missing_proof": [],
        "sufficient_for_draft": True,
        "fresh": True,
        "live_root_confirmed": True,
        "approved_direction_confirmed": True,
    }
    return _confirmed_proposal({
        "root_atom_id": "CP_REDISTRIBUTION_GAP",
        "proposal_source": "site_operational_context",
        "source": "rez",
        "incident_id": "INC-CAMPUS-REDIST",
        "target_device": "v2-store1",
        "suggested_pack": "routing_redistribution",
        "rationale": "Approved BGP-to-OSPF policy omits an affected approved prefix class.",
        "evidence_refs": ["approved-design:campus-bgp-to-ospf", "live:ssh"],
        "environment_id": "env-campus",
        "model_revision_id": "campus-approved-v1",
        "evidence_contract": copy.deepcopy(proof),
        "proposed_intent": {
            "change_type": "routing_redistribution",
            "site": "campus",
            "targets": {"device_ids": ["v2-store1"]},
            "redistribution": {
                "from_protocol": "bgp",
                "to_protocol": "ospf",
                "target_process": "1",
                "route_map": "CAMPUS-BGP-TO-OSPF",
                "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
                "prefixes": ["1.1.1.0/24"],
                "route_tag": 65002,
            },
            "operations": [operation],
            "rollback_operations": [rollback],
            "evidence_contract": copy.deepcopy(proof),
        },
    })


def _statement_gap_proposal() -> dict:
    payload = copy.deepcopy(_evidence_scoped_redistribution_proposal())
    operations = [
        {
            "op": "add_prefix_list_entry",
            "name": "ENTERPRISE-REMOTE-LOOPBACKS",
            "sequence": 20,
            "action": "permit",
            "prefix": "1.1.1.0/24",
            "le": 32,
        },
        {
            "op": "add_route_map_sequence",
            "name": "CAMPUS-BGP-TO-OSPF",
            "sequence": 20,
            "action": "permit",
            "match_prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
            "set_tag": 65002,
        },
        {
            "op": "add_redistribution_statement",
            "from_protocol": "bgp",
            "to_protocol": "ospf",
            "target_process": "1",
            "source_process": "65002",
            "address_family": "ipv4-unicast",
            "route_map": "CAMPUS-BGP-TO-OSPF",
            "subnets": False,
        },
    ]
    rollback = [
        {
            "op": "remove_redistribution_statement",
            "from_protocol": "bgp",
            "to_protocol": "ospf",
            "target_process": "1",
            "source_process": "65002",
            "address_family": "ipv4-unicast",
            "route_map": "CAMPUS-BGP-TO-OSPF",
            "subnets": False,
        },
        {
            "op": "remove_route_map_sequence",
            "name": "CAMPUS-BGP-TO-OSPF",
            "sequence": 20,
        },
        {
            "op": "remove_prefix_list_entry",
            "name": "ENTERPRISE-REMOTE-LOOPBACKS",
            "sequence": 20,
        },
    ]
    for contract in (
        payload["evidence_contract"],
        payload["proposed_intent"]["evidence_contract"],
    ):
        contract["classification"] = "statement_or_binding_gap"
        contract["operations"] = copy.deepcopy(operations)
        contract["rollback_operations"] = copy.deepcopy(rollback)
    payload["proposed_intent"]["operations"] = copy.deepcopy(operations)
    payload["proposed_intent"]["rollback_operations"] = copy.deepcopy(rollback)
    return payload


def test_statement_operation_requires_exact_source_process() -> None:
    with pytest.raises(ValidationError, match="exact source process"):
        RoutingRedistributionOperation.model_validate({
            "op": "add_redistribution_statement",
            "from_protocol": "ospf",
            "to_protocol": "bgp",
            "target_process": "65000",
            "route_map": "HQ-OSPF-TO-BGP",
        })


def _activate_evidence_model(
    tmp_path: Path,
    *,
    include_source_process: bool = True,
    routing_domain_roles: list[str] | None = None,
) -> None:
    workspace = WorkspacePaths(tmp_path.resolve())
    store = PlatformStore(workspace)
    repository = NetworkModelRepository(store)
    boundary = {
        "id": "campus-bgp-to-ospf",
        "devices": ["v2-store1"],
        "from_protocol": "bgp",
        "to_protocol": "ospf",
        "target_process": "1",
        "vrf": "default",
        "route_map": "CAMPUS-BGP-TO-OSPF",
        "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
        "prefix_classes": ["enterprise_remote_loopbacks"],
        "route_tag": 65002,
    }
    if include_source_process:
        boundary["source_process"] = "65002"
    routing_domains = []
    if routing_domain_roles is not None:
        routing_domains.append({
            "protocol": "bgp",
            "asn": 65002,
            "roles": routing_domain_roles,
        })
    repository.create_revision(
        {
            "schema": NETWORK_MODEL_SCHEMA,
            "org_id": "org_default",
            "environment_id": "env-campus",
            "revision_id": "campus-approved-v1",
            "status": "proposed",
            "source": {
                "type": "manual_review",
                "reference": "approved:campus-approved-v1",
            },
            "coverage": {
                "domains": ["identity", "sites", "routing", "route_propagation"]
            },
            "authority_bindings": {
                domain: {"source": "manual_review", "mode": "propose"}
                for domain in ("identity", "sites", "routing", "route_propagation")
            },
            "model": {
                "prefix_classes": {
                    "enterprise_remote_loopbacks": ["1.1.1.0/24"],
                },
                "sites": {
                    "campus": {
                        "devices": {
                            "v2-store1": {
                                "role": "edge",
                                "platform": "arista_eos",
                            }
                        },
                        "routing_domains": routing_domains,
                        "redistribution_boundaries": [boundary],
                    }
                },
                "devices": {
                    "v2-store1": {
                        "site": "campus",
                        "role": "edge",
                        "platform": "arista_eos",
                    },
                },
            },
        },
        created_by="evidence-reviewer",
    )
    approve_with_git(
        repository,
        org_id="org_default",
        environment_id="env-campus",
        revision_id="campus-approved-v1",
        approved_by="evidence-reviewer",
        git_root=workspace.git_workspace,
    )
    activate_verified_revision(
        repository,
        store,
        org_id="org_default",
        environment_id="env-campus",
        revision_id="campus-approved-v1",
        actor="evidence-reviewer",
        git_root=workspace.git_workspace,
        initial_baseline=True,
    )


def _activate_route_shadow_model(tmp_path: Path) -> None:
    workspace = WorkspacePaths(tmp_path.resolve())
    store = PlatformStore(workspace)
    repository = NetworkModelRepository(store)
    repository.create_revision(
        {
            "schema": NETWORK_MODEL_SCHEMA,
            "org_id": "org_default",
            "environment_id": "env-route-shadow",
            "revision_id": "route-shadow-approved-v1",
            "status": "proposed",
            "source": {
                "type": "manual_review",
                "reference": "approved:route-shadow-approved-v1",
            },
            "coverage": {
                "domains": ["identity", "sites", "routing", "address_plan"]
            },
            "authority_bindings": {
                domain: {"source": "manual_review", "mode": "propose"}
                for domain in ("identity", "sites", "routing", "address_plan")
            },
            "model": {
                "sites": {
                    "remote4": {
                        "devices": {
                            "v2-store1": {
                                "role": "branch",
                                "platform": "arista_eos",
                            },
                        },
                        "address_plan": [{
                            "name": "remote4-users",
                            "prefix": "10.90.90.0/24",
                            "ownership": "site",
                        }],
                    },
                    "store5": {
                        "devices": {
                            "v2-store3": {
                                "role": "branch",
                                "platform": "arista_eos",
                            },
                        },
                    },
                },
                "devices": {
                    "v2-store1": {
                        "site": "remote4",
                        "role": "branch",
                        "platform": "arista_eos",
                    },
                    "v2-store3": {
                        "site": "store5",
                        "role": "branch",
                        "platform": "arista_eos",
                    },
                },
            },
        },
        created_by="route-reviewer",
    )
    approve_with_git(
        repository,
        org_id="org_default",
        environment_id="env-route-shadow",
        revision_id="route-shadow-approved-v1",
        approved_by="route-reviewer",
        git_root=workspace.git_workspace,
    )
    activate_verified_revision(
        repository,
        store,
        org_id="org_default",
        environment_id="env-route-shadow",
        revision_id="route-shadow-approved-v1",
        actor="route-reviewer",
        git_root=workspace.git_workspace,
        initial_baseline=True,
    )


def _route_shadow_proposal() -> dict:
    observed = "ip route 10.90.90.0/25 Null0"
    proof = {
        "schema": "rez.route-shadow-evidence.v1",
        "sufficient_for_draft": True,
        "fresh": True,
        "live_root_confirmed": True,
        "root_atom_id": "L3_ROUTE_SOURCE_SHADOW",
        "device_id": "v2-store3",
        "vrf": "default",
        "specific_prefix": "10.90.90.0/25",
        "broad_prefix": "10.90.90.0/24",
        "discard_next_hop": "null0",
        "approved_owner_site": "remote4",
        "approved_owner_device": "v2-store1",
        "observed_config_line": observed,
        "removal_line": f"no {observed}",
        "rollback_line": observed,
        "environment_id": "env-route-shadow",
        "model_revision_id": "route-shadow-approved-v1",
    }
    return _confirmed_proposal({
        "root_atom_id": "L3_ROUTE_SOURCE_SHADOW",
        "proposal_source": "site_operational_context",
        "source": "rez",
        "incident_id": "INC-ROUTE-SHADOW",
        "target_device": "v2-store3",
        "suggested_pack": "custom_config",
        "rationale": "Remove the proven more-specific discard route.",
        "environment_id": "env-route-shadow",
        "model_revision_id": "route-shadow-approved-v1",
        "evidence_contract": proof,
        "proposed_intent": {
            "change_type": "custom_config",
            "site": "store5",
            "targets": {"device_ids": ["v2-store3"]},
            "config_lines": f"no {observed}",
            "rollback_lines": observed,
            "verify_contains": observed,
            "verify_absent": True,
        },
    })


def test_rca_remediation_rejects_unknown_change_type_without_fallback(
    tmp_path: Path,
    monkeypatch,
):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-2048",
            "target_device": "Branch-EDGE-03",
            "suggested_pack": "firewall_policy",
            "rationale": "Rez found missing outbound NAT evidence for the scoped flow.",
            "confidence": 0.82,
            "evidence_refs": ["show firewall policy", "policy/select"],
            "proposed_intent": {
                "site": "Site-204",
                "commands": [
                    "config firewall policy",
                    "edit 2048",
                    "set nat enable",
                    "next",
                    "end",
                ],
                "rollback_lines": "config firewall policy\nedit 2048\nunset nat\nnext\nend",
                "verify_contains": "nat enable",
            },
        }),
    )

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert response.status_code == 400
    assert "Unsupported Netcode change type: firewall_policy" in response.json()["detail"]
    assert store.list_changes() == []
    assert store.list_jobs() == []


def test_route_shadow_remediation_is_exact_reversible_and_draft_only(
    tmp_path: Path,
    monkeypatch,
):
    init_workspace(WorkspacePaths(tmp_path))
    _activate_route_shadow_model(tmp_path)
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=_route_shadow_proposal(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True
    failed_checks = [
        check
        for check in body["change"]["result"]["pipeline"]["validation"]["checks"]
        if check["status"] == "fail"
    ]
    assert body["change"]["workflow_state"] == "validated", failed_checks
    assert body["intent"]["custom"] == {
        "config_lines": "no ip route 10.90.90.0/25 Null0",
        "rollback_lines": "ip route 10.90.90.0/25 Null0",
        "verify_contains": "ip route 10.90.90.0/25 Null0",
        "verify_absent": True,
        "description": "Remove the proven more-specific discard route.",
        "acknowledge_no_rollback": False,
    }
    assert body["change"]["result"]["plan"]["commands"] == (
        "no ip route 10.90.90.0/25 Null0\n"
    )
    assert body["change"]["result"]["plan"]["rollback"] == (
        "ip route 10.90.90.0/25 Null0\n"
    )
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


@pytest.mark.parametrize(
    ("missing_field", "expected_detail"),
    [
        ("config_lines", "forward configuration"),
        ("rollback_lines", "rollback configuration"),
        ("verify_contains", "post-change verification target"),
    ],
)
def test_rez_custom_config_requires_forward_rollback_and_verification(
    tmp_path: Path,
    monkeypatch,
    missing_field: str,
    expected_detail: str,
):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    proposed_intent = {
        "change_type": "custom_config",
        "site": "store-1842",
        "config_lines": "vlan 992\n   name RCA_DRYRUN\n",
        "rollback_lines": "no vlan 992\n",
        "verify_contains": "vlan 992",
    }
    proposed_intent.pop(missing_field)

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": f"INC-MISSING-{missing_field.upper()}",
            "target_device": "v2-store1",
            "suggested_pack": "custom_config",
            "rationale": "Rez proposed a reviewed configuration draft.",
            "proposed_intent": proposed_intent,
        }),
    )

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]
    assert store.list_changes() == []
    assert store.list_jobs() == []


@pytest.mark.parametrize(
    ("mutator", "expected_detail"),
    [
        (
            lambda payload: payload["proposed_intent"].update(
                {"config_lines": "no ip route 10.90.90.0/24 Null0"}
            ),
            "exact reversible inverse",
        ),
        (
            lambda payload: payload["evidence_contract"].update(
                {"approved_owner_device": "v2-store3"}
            ),
            "one exact owner",
        ),
        (
            lambda payload: payload["proposed_intent"].update(
                {"verify_absent": False}
            ),
            "exact reversible inverse",
        ),
    ],
)
def test_route_shadow_remediation_rejects_tampered_scope_or_commands(
    tmp_path: Path,
    monkeypatch,
    mutator,
    expected_detail: str,
):
    init_workspace(WorkspacePaths(tmp_path))
    _activate_route_shadow_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = _route_shadow_proposal()
    mutator(payload)

    response = TestClient(api.app).post("/api/changes/from-rca", json=payload)

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]


def test_rca_remediation_preserves_known_typed_intent(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-ACL-01",
            "target_device": "Edge-FW-01",
            "suggested_pack": "acl_rule",
            "rationale": "Scoped flow needs an explicit HTTPS permit.",
            "proposed_intent": {
                "change_type": "acl_rule",
                "site": "Site-101",
                "acl": {
                    "name": "EDGE-IN",
                    "sequence": 40,
                    "action": "permit",
                    "protocol": "tcp",
                    "source": "10.10.0.0/24",
                    "destination": "203.0.113.10/32",
                    "destination_port": "443",
                },
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    intent = body["intent"]
    assert intent["change_type"] == "acl_rule"
    assert intent["targets"] == {"device_ids": ["Edge-FW-01"]}
    assert intent["metadata"]["ticket_id"] == "INC-ACL-01"
    assert body["change"]["workflow_state"] == "blocked"


def test_site_context_interface_remediation_stays_typed_and_human_gated(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "root_atom_id": "L1_INTERFACE_ADMIN_DOWN",
            "source": "rez",
            "incident_id": "INC-CAMPUS-ET2",
            "target_device": "v2-store1",
            "suggested_pack": "interface_config",
            "rationale": "Restore the exact intended interface dependency.",
            "proposed_intent": {
                "change_type": "interface_config",
                "site": "campus",
                "values": {
                    "interface": "Ethernet2",
                    "mode": "routed",
                    "enabled": True,
                    "description": "must not be applied",
                    "ip_address": "10.3.2.1/30",
                },
                "interface": {
                    "name": "Ethernet2",
                    "description": "must not be applied",
                    "enabled": True,
                    "mode": "routed",
                    "ip_address": "10.3.2.1/30",
                },
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    intent = body["intent"]
    assert intent["change_type"] == "interface_config"
    assert intent["interface"]["name"] == "Ethernet2"
    assert intent["interface"]["enabled"] is True
    assert intent["interface"]["apply_scope"] == "admin_state"
    assert intent["metadata"]["draft_only"] is True
    assert intent["metadata"]["human_approval_required"] is True
    assert body["change"]["workflow_state"] == "validated"
    assert body["change"]["result"]["plan"]["commands"] == "interface Ethernet2\n   no shutdown\n"
    assert body["change"]["result"]["plan"]["rollback"] == "interface Ethernet2\n   shutdown\n"
    interface_policy = next(
        check
        for check in body["change"]["result"]["pipeline"]["validation"]["checks"]
        if check["id"] == "interface_policy"
    )
    assert interface_policy["message"] == "The change is limited to the interface administrative state."
    assert interface_policy["evidence"] == {
        "interface": "Ethernet2",
        "apply_scope": "admin_state",
        "expected_enabled": True,
    }


def test_human_reviewed_interface_fault_creates_only_a_validated_draft(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_ADMIN_TOKEN", "rez-service-token")
    client = TestClient(api.app)
    headers = {
        "Authorization": "Bearer rez-service-token",
        "X-Rezonance-Org-ID": "org_default",
        "X-Rezonance-User": "marcus",
        "X-Rezonance-Role": "operator",
    }
    payload = {
        "proposal_schema": "netcode.remediation.v1",
        "proposal_source": "human_reviewed_rca",
        "root_confirmed": True,
        "root_atom_id": "L1_INTERFACE_ADMIN_DOWN",
        "source": "rez",
        "incident_id": "INC-HQ-ET2",
        "target_device": "v2-store1",
        "suggested_pack": "interface_config",
        "rationale": "marcus confirmed Ethernet2 is expected up.",
        "requested_by": "marcus",
        "intent_reviewed": True,
        "reviewed_by": "marcus",
        "review_candidate_id": "REZ-INTENT-123456789ABC",
        "proposed_intent": {
            "change_type": "interface_config",
            "site": "hq",
            "values": {"interface": "Ethernet2", "enabled": True, "apply_scope": "admin_state"},
        },
    }

    response = client.post("/api/changes/from-rca", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True
    assert body["change"]["workflow_state"] == "validated"
    assert body["intent"]["interface"] == {
        "name": "Ethernet2",
        "enabled": True,
        "apply_scope": "admin_state",
    }
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_human_reviewed_interface_fault_rejects_missing_review_identity(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json={
            "proposal_schema": "netcode.remediation.v1",
            "proposal_source": "human_reviewed_rca",
            "root_confirmed": True,
            "root_atom_id": "L1_INTERFACE_ADMIN_DOWN",
            "source": "rez",
            "incident_id": "INC-HQ-ET2",
            "target_device": "v2-hq-core",
            "suggested_pack": "interface_config",
            "intent_reviewed": False,
            "proposed_intent": {
                "change_type": "interface_config",
                "site": "hq",
                "values": {"interface": "Ethernet2", "enabled": True},
            },
        },
    )

    assert response.status_code == 400
    assert "review" in response.json()["detail"].lower()


def test_site_context_redistribution_remediation_is_typed_validated_and_human_gated(
    tmp_path: Path, monkeypatch
):
    init_workspace(WorkspacePaths(tmp_path))
    _activate_evidence_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_evidence_scoped_redistribution_proposal(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True
    assert body["change"]["workflow_state"] == "validated"
    assert body["intent"]["change_type"] == "routing_redistribution"
    assert body["intent"]["redistribution"]["route_map"] == "CAMPUS-BGP-TO-OSPF"
    commands = body["change"]["result"]["plan"]["commands"]
    assert commands == (
        "ip prefix-list ENTERPRISE-REMOTE-LOOPBACKS "
        "seq 20 permit 1.1.1.0/24 le 32\n"
    )
    rollback = body["change"]["result"]["plan"]["rollback"]
    assert rollback == "no ip prefix-list ENTERPRISE-REMOTE-LOOPBACKS seq 20\n"
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_exact_source_process_statement_passes_static_pipeline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_evidence_model(tmp_path)
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=_statement_gap_proposal(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    checks = body["change"]["result"]["pipeline"]["validation"]["checks"]
    assert all(check["status"] == "pass" for check in checks), checks
    commands = body["change"]["result"]["plan"]["commands"]
    assert (
        "redistribute bgp 65002 route-map CAMPUS-BGP-TO-OSPF"
        in commands
    )
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_source_process_inference_rejects_wrong_target_role(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_evidence_model(
        tmp_path,
        include_source_process=False,
        routing_domain_roles=["core"],
    )
    monkeypatch.chdir(tmp_path)

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=_statement_gap_proposal(),
    )

    assert response.status_code == 400
    assert "protocol boundary" in response.json()["detail"].lower()
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_changes() == []
    assert store.list_jobs() == []


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_proof",
        "stale",
        "wrong_device",
        "wrong_direction",
        "outside_prefix",
        "operation_outside_prefix",
        "wrong_model_boundary",
        "wrong_route_tag",
        "non_default_vrf",
        "platform_mismatch",
        "rollback_mismatch",
        "embedded_mismatch",
    ],
)
def test_site_context_redistribution_rejects_unproven_or_tampered_evidence(
    tmp_path: Path,
    monkeypatch,
    tamper: str,
):
    init_workspace(WorkspacePaths(tmp_path))
    _activate_evidence_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_evidence_scoped_redistribution_proposal())
    proof = payload["evidence_contract"]
    embedded = payload["proposed_intent"]["evidence_contract"]
    if tamper == "missing_proof":
        proof["missing_proof"] = ["routing_policy_collection"]
        embedded["missing_proof"] = ["routing_policy_collection"]
    elif tamper == "stale":
        proof["fresh"] = False
        embedded["fresh"] = False
    elif tamper == "wrong_device":
        proof["device_id"] = "another-edge"
        embedded["device_id"] = "another-edge"
    elif tamper == "wrong_direction":
        proof["direction"]["from_protocol"] = "ospf"
        proof["direction"]["to_protocol"] = "bgp"
        embedded["direction"]["from_protocol"] = "ospf"
        embedded["direction"]["to_protocol"] = "bgp"
    elif tamper == "outside_prefix":
        proof["affected_prefixes"] = ["203.0.113.1/32"]
        embedded["affected_prefixes"] = ["203.0.113.1/32"]
    elif tamper == "operation_outside_prefix":
        for contract in (proof, embedded):
            contract["operations"][0]["prefix"] = "0.0.0.0/1"
        payload["proposed_intent"]["operations"][0]["prefix"] = "0.0.0.0/1"
    elif tamper == "wrong_model_boundary":
        proof["boundary_id"] = "unapproved-boundary"
        embedded["boundary_id"] = "unapproved-boundary"
    elif tamper == "wrong_route_tag":
        for contract in (proof, embedded):
            contract["approved_policy"]["route_tag"] = 65003
        payload["proposed_intent"]["redistribution"]["route_tag"] = 65003
    elif tamper == "non_default_vrf":
        proof["vrf"] = "customer-a"
        embedded["vrf"] = "customer-a"
    elif tamper == "platform_mismatch":
        proof["platform"] = "cisco_ios"
        embedded["platform"] = "cisco_ios"
    elif tamper == "rollback_mismatch":
        proof["rollback_operations"][0]["sequence"] = 30
        embedded["rollback_operations"][0]["sequence"] = 30
        payload["proposed_intent"]["rollback_operations"][0]["sequence"] = 30
    elif tamper == "embedded_mismatch":
        embedded["classification"] = "statement_or_binding_gap"

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=payload,
    )

    assert response.status_code == 400
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    assert store.list_changes() == []
    assert store.list_jobs() == []


def test_site_context_bidirectional_exchange_is_typed_scoped_and_human_gated(
    tmp_path: Path, monkeypatch
):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "root_atom_id": "CP_REDISTRIBUTION_GAP",
            "proposal_source": "site_operational_context",
            "source": "rez",
            "incident_id": "INC-CAMPUS-BIDIRECTIONAL",
            "target_device": "v2-store1",
            "suggested_pack": "routing_redistribution",
            "rationale": "Approved bidirectional route exchange is absent and scoped reachability failed.",
            "evidence_refs": ["approved-design:campus-route-exchange", "live:ssh"],
            "proposed_intent": {
                "change_type": "routing_redistribution",
                "site": "campus",
                "targets": {"device_ids": ["v2-store1"]},
                "redistribution": {
                    "from_protocol": "bgp",
                    "to_protocol": "ospf",
                    "target_process": "1",
                    "route_map": "CAMPUS-BGP-TO-OSPF",
                    "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
                    "prefixes": ["1.1.1.0/24", "2.2.2.0/24", "4.4.4.0/24", "5.5.5.0/24"],
                    "route_tag": 65002,
                },
                "reverse_redistribution": {
                    "from_protocol": "ospf",
                    "to_protocol": "bgp",
                    "target_process": "65002",
                    "route_map": "CAMPUS-OSPF-TO-BGP",
                    "prefix_list": "CAMPUS-SITE-ROUTES",
                    "prefixes": ["3.3.3.0/24", "10.3.0.0/16"],
                    "route_tag": 65003,
                },
                "reachability_checks": [
                    {"source_device": "v2-store1", "source_ip": "3.3.3.1", "destination": "1.1.1.2"}
                ],
            },
        }),
    )

    assert response.status_code == 400
    assert "evidence" in response.json()["detail"].lower()
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_redistribution_rollback_must_follow_reverse_dependency_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_workspace(WorkspacePaths(tmp_path))
    _activate_evidence_model(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = copy.deepcopy(_evidence_scoped_redistribution_proposal())
    route_map_operation = {
        "op": "add_route_map_sequence",
        "name": "CAMPUS-BGP-TO-OSPF",
        "sequence": 20,
        "action": "permit",
        "match_prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
        "set_tag": 65002,
    }
    route_map_rollback = {
        "op": "remove_route_map_sequence",
        "name": "CAMPUS-BGP-TO-OSPF",
        "sequence": 20,
    }
    for contract in (
        payload["evidence_contract"],
        payload["proposed_intent"]["evidence_contract"],
    ):
        contract["operations"].append(copy.deepcopy(route_map_operation))
        contract["rollback_operations"] = [
            copy.deepcopy(contract["rollback_operations"][0]),
            copy.deepcopy(route_map_rollback),
        ]
    payload["proposed_intent"]["operations"].append(
        copy.deepcopy(route_map_operation)
    )
    payload["proposed_intent"]["rollback_operations"] = copy.deepcopy(
        payload["evidence_contract"]["rollback_operations"]
    )

    response = TestClient(api.app).post(
        "/api/changes/from-rca",
        json=payload,
    )

    assert response.status_code == 400
    assert "reverse-ordered" in response.json()["detail"]
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_multitarget_site_context_exchange_creates_canary_rollout_without_jobs(
    tmp_path: Path, monkeypatch
):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "root_atom_id": "CP_REDISTRIBUTION_GAP",
            "proposal_source": "site_operational_context",
            "source": "rez",
            "incident_id": "INC-CAMPUS-HA-EXCHANGE",
            "target_device": "v2-store1",
            "suggested_pack": "routing_redistribution",
            "rationale": "Both approved route-exchange boundaries are absent.",
            "evidence_refs": ["approved-design:campus-route-exchange", "live:ssh"],
            "proposed_intent": {
                "change_type": "routing_redistribution",
                "site": "campus",
                "targets": {"device_ids": ["v2-store1", "v2-store2"]},
                "redistribution": {
                    "from_protocol": "bgp",
                    "to_protocol": "ospf",
                    "target_process": "1",
                    "route_map": "CAMPUS-BGP-TO-OSPF",
                    "prefix_list": "ENTERPRISE-REMOTE-LOOPBACKS",
                    "prefixes": ["1.1.1.0/24", "4.4.4.0/24"],
                    "route_tag": 65002,
                },
                "reverse_redistribution": {
                    "from_protocol": "ospf",
                    "to_protocol": "bgp",
                    "target_process": "65002",
                    "route_map": "CAMPUS-OSPF-TO-BGP",
                    "prefix_list": "CAMPUS-SITE-ROUTES",
                    "prefixes": ["3.3.3.0/24", "10.3.0.0/16"],
                    "route_tag": 65003,
                },
                "reachability_checks": [
                    {"source_device": "v2-store1", "source_ip": "3.3.3.1", "destination": "1.1.1.2"}
                ],
            },
        }),
    )

    assert response.status_code == 400
    assert "evidence" in response.json()["detail"].lower()
    assert PlatformStore(WorkspacePaths(tmp_path.resolve())).list_jobs() == []


def test_rez_rca_validated_draft_can_enter_dry_run_queue(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-DRYRUN",
            "target_device": "v2-store1",
            "suggested_pack": "custom_config",
            "rationale": "Rez proposed a reviewed config draft.",
            "proposed_intent": {
                "site": "store-1842",
                "config_lines": "vlan 992\n   name RCA_DRYRUN\n",
                "rollback_lines": "no vlan 992\n",
                "verify_contains": "vlan 992",
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["change"]["workflow_state"] == "validated"
    assert body["draft_only"] is True
    assert body["human_approval_required"] is True

    from netcode.jobs import JobRunner

    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    dry_run = JobRunner(WorkspacePaths(tmp_path.resolve()), store=store).run_lab_action(
        Path(body["intent_path"]),
        "dry-run",
        "v2-store1",
        body["change_id"],
    )

    assert dry_run["ok"] is True
    assert dry_run["queued"] is True
    assert dry_run["job"]["action"] == "lab_dry-run"
    assert dry_run["change"]["workflow_state"] == "validated"


def test_rca_remediation_strips_credential_shaped_fields(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-CREDS",
            "target_device": "Edge-FW-01",
            "suggested_pack": "acl_rule",
            "rationale": "Scoped flow needs an explicit HTTPS permit.",
            "proposed_intent": {
                "change_type": "acl_rule",
                "site": "Site-101",
                "password": "hunter2",
                "metadata": {"api_token": "token-should-not-persist", "operator_note": "safe"},
                "policy": {"pci_reachable": False, "private_key": "key-should-not-persist"},
                "acl": {
                    "name": "EDGE-IN",
                    "sequence": 40,
                    "action": "permit",
                    "protocol": "tcp",
                    "source": "10.10.0.0/24",
                    "destination": "203.0.113.10/32",
                    "destination_port": "443",
                    "enable_secret": "secret-should-not-persist",
                },
            },
        }),
    )

    assert response.status_code == 200
    intent_path = Path(response.json()["intent_path"])
    serialized = json.dumps(read_yaml(intent_path), sort_keys=True)
    assert "hunter2" not in serialized
    assert "token-should-not-persist" not in serialized
    assert "key-should-not-persist" not in serialized
    assert "secret-should-not-persist" not in serialized
    assert "password" not in serialized.lower()
    assert "api_token" not in serialized.lower()
    assert "private_key" not in serialized.lower()
    assert "enable_secret" not in serialized.lower()


def test_rez_rca_draft_requires_approval_even_when_global_gate_off(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETCODE_REQUIRE_APPROVAL", "0")
    monkeypatch.setenv("NETCODE_EXECUTION", "runner")
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-APPROVAL",
            "target_device": "v2-store1",
            "suggested_pack": "custom_config",
            "rationale": "Rez proposed a reviewed config draft.",
            "proposed_intent": {
                "site": "store-1842",
                "config_lines": "vlan 991\n   name RCA_REVIEWED\n",
                "rollback_lines": "no vlan 991\n",
                "verify_contains": "vlan 991",
            },
        }),
    )

    assert response.status_code == 200
    body = response.json()
    store = PlatformStore(WorkspacePaths(tmp_path.resolve()))
    change = store.get_change(body["change_id"])
    store.record_workflow_event(change.id, "dry-run", change.workflow_state, "dry_run_passed", "dry-run proof", {})

    from netcode.jobs import JobRunner

    blocked = JobRunner(WorkspacePaths(tmp_path.resolve()), store=store).run_lab_action(
        Path(body["intent_path"]),
        "apply",
        "v2-store1",
        change.id,
    )

    assert blocked["ok"] is False
    assert blocked["result"]["approval_required"] is True
    assert blocked["result"]["workflow_state"] == "dry_run_passed"


def test_rca_remediation_rejects_agent_narrative_without_confirmed_root(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json={
            "source": "rez",
            "incident_id": "INC-UNCONFIRMED",
            "target_device": "v2-store1",
            "rationale": "Agent Analysis / Unverified Hypothesis",
            "proposed_intent": {
                "change_type": "custom_config",
                "config_lines": "No configuration change is recommended from this run.",
            },
        },
    )

    assert response.status_code == 400
    assert "structured Netcode remediation proposal" in response.json()["detail"]
    assert PlatformStore(workspace).list_changes() == []


def test_rca_remediation_rejects_non_actionable_framework_root(tmp_path: Path, monkeypatch):
    workspace = WorkspacePaths(tmp_path)
    init_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "root_atom_id": "CI_ROOT_CAUSE",
            "source": "rez",
            "incident_id": "INC-FRAMEWORK",
            "target_device": "v2-store1",
            "proposed_intent": {
                "change_type": "custom_config",
                "config_lines": "description should-not-land",
            },
        }),
    )

    assert response.status_code == 400
    assert "not an actionable device condition" in response.json()["detail"]
    assert PlatformStore(workspace).list_changes() == []


def test_rca_remediation_requires_target_scope(tmp_path: Path, monkeypatch):
    init_workspace(WorkspacePaths(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)

    response = client.post(
        "/api/changes/from-rca",
        json=_confirmed_proposal({
            "source": "rez",
            "incident_id": "INC-MISSING-SCOPE",
            "rationale": "Missing target should fail closed.",
            "proposed_intent": {"change_type": "custom_config", "config_lines": "description x"},
        }),
    )

    assert response.status_code == 400
    assert "target_device" in response.json()["detail"]
