from __future__ import annotations

import json

import pytest

from netcode import entitlements


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    entitlements.reset_cache_for_tests()
    monkeypatch.setenv("NETCODE_LICENSE_ENFORCEMENT", "1")
    monkeypatch.setenv("NETCODE_ENTITLEMENT_URL", "http://rez/api/license/platform-entitlements")
    monkeypatch.setenv("NETCODE_ENTITLEMENT_TOKEN", "secret")


def _payload(*, writes: bool = True) -> dict:
    return {
        "plan_id": "starter",
        "platform_available": True,
        "entitlements": {
            "max_devices": 50,
            "max_connectors": 3,
            "max_workflow_packs": 5,
            "netcode_production_writes": writes,
        },
    }


def test_fetches_authoritative_public_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entitlements.urllib.request, "urlopen", lambda request, timeout: _Response(_payload()))
    value = entitlements.get_entitlements()
    assert value.plan_id == "starter"
    assert value.max_devices == 50
    assert value.production_writes is True


def test_cache_and_authority_request_are_scoped_by_organization(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def authority(request, timeout):
        headers = {key.lower(): value for key, value in request.header_items()}
        org_id = headers["x-rezonance-org-id"]
        seen.append(org_id)
        payload = _payload()
        payload["entitlements"]["max_devices"] = 25 if org_id == "org-a" else 10000
        return _Response(payload)

    monkeypatch.setattr(entitlements.urllib.request, "urlopen", authority)
    assert entitlements.get_entitlements(org_id="org-a").max_devices == 25
    assert entitlements.get_entitlements(org_id="org-b").max_devices == 10000
    assert entitlements.get_entitlements(org_id="org-a").max_devices == 25
    assert seen == ["org-a", "org-b"]


def test_production_writes_fail_closed_for_community(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entitlements.urllib.request, "urlopen", lambda request, timeout: _Response(_payload(writes=False)))
    with pytest.raises(entitlements.EntitlementError, match="Production writes"):
        entitlements.require_production_writes()


def test_device_limit_is_enforced_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entitlements.urllib.request, "urlopen", lambda request, timeout: _Response(_payload()))
    with pytest.raises(entitlements.EntitlementError, match="allows 50 devices"):
        entitlements.enforce_capacity("devices", current=50, additional=1)


def test_authority_outage_fails_closed_without_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(entitlements.urllib.request, "urlopen", offline)
    with pytest.raises(entitlements.EntitlementError, match="fail closed"):
        entitlements.get_entitlements()


def test_explicit_development_mode_is_unmetered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETCODE_LICENSE_ENFORCEMENT", "0")
    assert entitlements.get_entitlements().source == "development_bypass"


@pytest.mark.parametrize(
    "action",
    ["lab_apply", "lab_rollback", "arista_full_run", "ansible_apply", "manager_deploy", "manager_rollback"],
)
def test_write_job_classification_is_fail_closed(action: str) -> None:
    assert entitlements.job_requires_production_writes(action) is True


@pytest.mark.parametrize("action", ["lab_dry-run", "read_rez_ssh", "ansible_check", "manager_preview"])
def test_read_and_preview_job_classification_stays_available(action: str) -> None:
    assert entitlements.job_requires_production_writes(action) is False
