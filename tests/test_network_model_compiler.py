from __future__ import annotations

import pytest

from netcode.network_model import NETWORK_MODEL_SCHEMA, NetworkModelError
from netcode.network_model_compiler import (
    compile_effective_device,
    compile_site_context,
    to_rez_network_design,
)


def _revision(status: str = "approved") -> dict:
    return {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": "org-default",
        "environment_id": "customer-a",
        "revision_id": "rev-001",
        "status": status,
        "source": {"type": "git", "reference": "model/rev-001"},
        "coverage": {"domains": ["identity", "sites", "routing", "reachability"]},
        "authority_bindings": {
            domain: {"source": "git", "mode": "authoritative"}
            for domain in ("identity", "sites", "routing", "reachability")
        },
        "approval": {
            "status": "approved",
            "approved_by": "marcus",
            "approved_at": "2026-07-12T12:00:00Z",
        },
        "model": {
            "organization_standard": {"ntp": {"servers": ["192.0.2.10"]}, "logging": {"enabled": True}},
            "site_archetypes": {
                "dual-edge": {"routing": {"ospf": {"enabled": True, "area": "0.0.0.0"}}}
            },
            "role_standards": {"edge": {"routing": {"bgp": {"enabled": True}}}},
            "group_standards": {"pci": {"logging": {"retention_days": 90}}},
            "sites": {
                "region-blue": {
                    "archetype": "dual-edge",
                    "intent": {"routing": {"ospf": {"area": "0.0.0.12"}}},
                    "devices": {
                        "border-a": {"role": "edge", "groups": ["pci"], "overrides": {"ntp": {"prefer": 1}}}
                    },
                    "operational_dependencies": [
                        {
                            "id": "wan-peer-a",
                            "device_id": "border-a",
                            "kind": "bgp",
                            "identity": {"neighbor": "198.51.100.1"},
                            "expected": {"state": "established"},
                        }
                    ],
                }
            },
            "devices": {
                "border-a": {
                    "site": "region-blue",
                    "role": "edge",
                    "groups": ["pci"],
                    "overrides": {"logging": {"enabled": False}},
                }
            },
        },
    }


def test_effective_device_uses_deterministic_precedence():
    context = compile_effective_device(_revision(), "border-a", required_domains=["routing"])

    assert context["operationally_usable"] is True
    assert context["site_id"] == "region-blue"
    assert context["effective"]["routing"]["ospf"] == {"enabled": True, "area": "0.0.0.12"}
    assert context["effective"]["routing"]["bgp"]["enabled"] is True
    assert context["effective"]["logging"] == {"enabled": False, "retention_days": 90}
    assert context["effective"]["ntp"] == {"servers": ["192.0.2.10"], "prefer": 1}
    assert context["layers"] == [
        "organization_standard",
        "site_archetype:dual-edge",
        "site:region-blue",
        "role:edge",
        "group:pci",
        "site_device:border-a",
        "device:border-a",
    ]


def test_missing_coverage_is_unknown_not_healthy():
    context = compile_site_context(
        _revision(), "region-blue", required_domains=["routing", "qos", "security_policy"]
    )
    assert context["operationally_usable"] is False
    assert context["missing_coverage"] == ["qos", "security_policy"]


def test_proposed_revision_is_preview_only():
    proposed = _revision(status="proposed")
    proposed.pop("approval")
    with pytest.raises(NetworkModelError, match="approved or active"):
        compile_effective_device(proposed, "border-a")
    preview = compile_effective_device(proposed, "border-a", require_approved=False)
    assert preview["operationally_usable"] is False


def test_device_cannot_be_silently_assigned_to_two_sites():
    revision = _revision()
    revision["model"]["sites"]["another-region"] = {"devices": {"border-a": {"role": "edge"}}}
    with pytest.raises(NetworkModelError, match="multiple approved sites"):
        compile_effective_device(revision, "border-a")


def test_string_group_is_one_group_not_individual_characters():
    revision = _revision()
    revision["model"]["devices"]["border-a"]["groups"] = "pci"
    context = compile_effective_device(revision, "border-a")
    assert context["groups"] == ["pci"]
    assert "group:p" not in context["layers"]


def test_rez_export_preserves_exact_customer_design_without_lab_defaults():
    design = to_rez_network_design(_revision())
    assert design["schema"] == "rez.network-design.v1"
    assert design["source"]["type"] == "rezonance_model"
    assert design["sites"]["region-blue"]["operational_dependencies"][0]["id"] == "wan-peer-a"
    assert design["sites"]["region-blue"]["devices"]["border-a"]["role"] == "edge"
    assert "v2-" not in str(design)
