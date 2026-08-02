import json
from types import SimpleNamespace

from netcode.diagnostics_dispatch import dispatch_verification_handoff
from netcode.diagnostics_handoff import change_environment_binding


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_dispatch_is_disabled_without_complete_deployment_config(monkeypatch):
    monkeypatch.delenv("NETCODE_REZ_TRIGGER_URL", raising=False)
    monkeypatch.delenv("NETCODE_REZ_TRIGGER_TOKEN", raising=False)
    assert dispatch_verification_handoff({})["status"] == "disabled"


def test_dispatch_requires_persisted_tenant_and_environment(monkeypatch):
    monkeypatch.setenv("NETCODE_REZ_TRIGGER_URL", "https://rez.internal")
    monkeypatch.setenv("NETCODE_REZ_TRIGGER_TOKEN", "secret")

    result = dispatch_verification_handoff(
        {"context": {"failed": True, "read_only": True}}
    )

    assert result == {
        "status": "disabled",
        "reason": "A persisted change organization and environment binding are required",
    }


def test_change_environment_binding_requires_one_unambiguous_persisted_value():
    assert (
        change_environment_binding(
            SimpleNamespace(
                result={
                    "network_model": {"environment_id": "env_customer"},
                    "pipeline": {
                        "network_model": {"environment_id": "env_customer"}
                    },
                }
            )
        )
        == "env_customer"
    )
    assert (
        change_environment_binding(
            SimpleNamespace(
                result={
                    "network_model": {"environment_id": "env_customer"},
                    "service_assurance": {"environment_id": "env_other"},
                }
            )
        )
        == ""
    )


def test_dispatch_sends_read_only_handoff_with_tenant_environment(monkeypatch):
    seen = {}
    monkeypatch.setenv("NETCODE_REZ_TRIGGER_URL", "https://rez.internal")
    monkeypatch.setenv("NETCODE_REZ_TRIGGER_TOKEN", "secret")

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["payload"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response({"ok": True, "investigation_id": "netcode_1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    handoff = {
        "context": {
            "failed": True,
            "read_only": True,
            "org_id": "org_customer",
            "environment_id": "env_customer",
        },
        "safety": {"device_writes": "none"},
    }
    result = dispatch_verification_handoff(handoff)

    assert result["status"] == "accepted"
    assert seen["url"].endswith("/api/integrations/netcode/verification-failure")
    assert seen["payload"]["organization_binding"] == "org_customer"
    assert seen["payload"]["environment_binding"] == "env_customer"
    assert seen["payload"]["handoff"] == handoff
    assert seen["headers"]["X-rez-integration-token"] == "secret"
