from __future__ import annotations

import pytest

from netcode.network_model import (
    NETWORK_MODEL_SCHEMA,
    NETWORK_OBSERVATION_SCHEMA,
    NetworkModelError,
    validate_model_revision,
    validate_observation,
)


def _revision(**overrides):
    value = {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": "default",
        "environment_id": "lab-a",
        "revision_id": "rev-001",
        "status": "proposed",
        "source": {"type": "manual_review", "reference": "model/rev-001"},
        "coverage": {"domains": ["identity", "routing"]},
        "authority_bindings": {
            "identity": {"source": "rezonance", "mode": "authoritative"},
            "routing": {"source": "git-main", "mode": "authoritative"},
        },
        "model": {"devices": {"edge-1": {"role": "edge"}}},
    }
    value.update(overrides)
    return value


def test_proposed_revision_has_domain_specific_authority():
    revision = validate_model_revision(_revision())
    assert revision["coverage"]["domains"] == ["identity", "routing"]
    assert revision["authority_bindings"]["routing"] == {
        "source": "git-main",
        "mode": "authoritative",
    }


@pytest.mark.parametrize("source_type", ["discovery", "incident", "telemetry", "device"])
def test_observation_sources_cannot_authorize_approved_intent(source_type):
    value = _revision(
        status="active",
        source={"type": source_type, "reference": "job-123"},
        approval={"status": "approved", "approved_by": "marcus", "approved_at": "2026-07-12T12:00:00Z"},
    )
    with pytest.raises(NetworkModelError, match="observation-only"):
        validate_model_revision(value)


def test_approved_revision_requires_human_identity_and_timestamp():
    with pytest.raises(NetworkModelError, match="approved_by"):
        validate_model_revision(
            _revision(
                status="approved",
                approval={"status": "approved", "approved_at": "2026-07-12T12:00:00Z"},
            )
        )


def test_covered_domains_require_explicit_authority():
    value = _revision(authority_bindings={"identity": {"source": "rezonance", "mode": "authoritative"}})
    with pytest.raises(NetworkModelError, match="missing covered domains: routing"):
        validate_model_revision(value)


@pytest.mark.parametrize("secret_key", ["password", "api_token", "private_key", "snmp_community_string"])
def test_credentials_cannot_enter_any_model_path(secret_key):
    value = _revision(model={"devices": {"edge-1": {secret_key: "do-not-store"}}})
    with pytest.raises(NetworkModelError, match="credential-shaped"):
        validate_model_revision(value)


def test_custom_provider_is_allowed_without_hardcoded_catalog():
    value = _revision(
        authority_bindings={
            "identity": {"source": "customer-inventory-v3", "mode": "authoritative"},
            "routing": {"source": "customer-git", "mode": "authoritative"},
        }
    )
    revision = validate_model_revision(value)
    assert revision["authority_bindings"]["identity"]["source"] == "customer-inventory-v3"


def test_observation_source_cannot_hide_behind_manual_document_approval():
    value = _revision(
        status="approved",
        authority_bindings={
            "identity": {"source": "rezonance", "mode": "authoritative"},
            "routing": {"source": "discovery", "mode": "authoritative"},
        },
        approval={"status": "approved", "approved_by": "marcus", "approved_at": "2026-07-12T12:00:00Z"},
    )
    with pytest.raises(NetworkModelError, match="observation-only source"):
        validate_model_revision(value)


def test_approved_coverage_cannot_remain_proposal_only():
    value = _revision(
        status="active",
        authority_bindings={
            "identity": {"source": "rezonance", "mode": "authoritative"},
            "routing": {"source": "customer-git", "mode": "propose"},
        },
        approval={"status": "approved", "approved_by": "marcus", "approved_at": "2026-07-12T12:00:00Z"},
    )
    with pytest.raises(NetworkModelError, match="authoritative bindings.*routing"):
        validate_model_revision(value)


def test_observation_is_append_only_and_cannot_carry_approval():
    observation = {
        "schema": NETWORK_OBSERVATION_SCHEMA,
        "org_id": "default",
        "environment_id": "lab-a",
        "observation_id": "obs-001",
        "domain": "routing",
        "source": "discovery",
        "observed_at": "2026-07-12T12:00:00Z",
        "collector_id": "connector-1",
        "facts": {"routes": 42},
        "approval": {"status": "approved"},
    }
    with pytest.raises(NetworkModelError, match="cannot carry model approval"):
        validate_observation(observation)


def test_tenant_and_environment_are_required():
    with pytest.raises(NetworkModelError, match="org_id"):
        validate_model_revision(_revision(org_id=""))
