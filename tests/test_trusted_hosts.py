import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from netcode.trusted_hosts import PrivateReadinessTrustedHostMiddleware


def _client(*allowed_hosts: str) -> TestClient:
    app = FastAPI()

    @app.get("/api/ready")
    def ready():
        return {"status": "ready"}

    @app.get("/api/private")
    def private():
        return {"sensitive": True}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    app.add_middleware(
        PrivateReadinessTrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )
    return TestClient(app)


def test_configured_hostname_can_reach_all_routes():
    client = _client("netcode.example.com")

    assert client.get("/api/private", headers={"Host": "netcode.example.com"}).status_code == 200


def test_private_ip_host_can_reach_only_readiness():
    client = _client("netcode.example.com")

    assert client.get("/api/ready", headers={"Host": "10.44.1.9:8095"}).status_code == 200
    assert client.get("/api/private", headers={"Host": "10.44.1.9:8095"}).status_code == 400


def test_private_ipv6_host_can_reach_readiness_with_optional_trailing_slash():
    client = _client("netcode.example.com")

    assert client.get("/api/ready", headers={"Host": "[fd00::9]:8095"}).status_code == 200
    assert client.get("/api/ready/", headers={"Host": "10.44.1.9:8095"}).status_code == 200
    assert client.get("/api/private", headers={"Host": "[fd00::9]:8095"}).status_code == 400


def test_private_ip_websocket_does_not_receive_readiness_exception():
    client = _client("netcode.example.com")

    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/ws", headers={"Host": "10.44.1.9:8095"}):
            pass

    assert denied.value.status_code == 400


def test_public_ip_and_untrusted_hostname_cannot_use_readiness_exception():
    client = _client("netcode.example.com")

    assert client.get("/api/ready", headers={"Host": "8.8.8.8"}).status_code == 400
    assert client.get("/api/ready", headers={"Host": "attacker.example"}).status_code == 400


def test_leading_wildcard_matches_subdomains_but_not_apex():
    client = _client("*.rezonance.example")

    assert client.get("/api/private", headers={"Host": "control.rezonance.example"}).status_code == 200
    assert client.get("/api/private", headers={"Host": "rezonance.example"}).status_code == 400
