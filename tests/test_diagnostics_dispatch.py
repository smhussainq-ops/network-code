import json

from netcode.diagnostics_dispatch import dispatch_verification_handoff


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
    monkeypatch.delenv("NETCODE_REZ_ENVIRONMENT_ID", raising=False)
    assert dispatch_verification_handoff({})["status"] == "disabled"


def test_dispatch_sends_read_only_handoff_with_environment(monkeypatch):
    seen = {}
    monkeypatch.setenv("NETCODE_REZ_TRIGGER_URL", "https://rez.internal")
    monkeypatch.setenv("NETCODE_REZ_TRIGGER_TOKEN", "secret")
    monkeypatch.setenv("NETCODE_REZ_ENVIRONMENT_ID", "env_customer")

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["payload"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response({"ok": True, "investigation_id": "netcode_1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    handoff = {
        "context": {"failed": True, "read_only": True},
        "safety": {"device_writes": "none"},
    }
    result = dispatch_verification_handoff(handoff)

    assert result["status"] == "accepted"
    assert seen["url"].endswith("/api/integrations/netcode/verification-failure")
    assert seen["payload"]["environment_binding"] == "env_customer"
    assert seen["payload"]["handoff"] == handoff
    assert seen["headers"]["X-rez-integration-token"] == "secret"
