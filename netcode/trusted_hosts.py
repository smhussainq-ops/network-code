"""Strict Host validation with a narrow private readiness exception."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse


def _hostname(raw_host: str) -> str:
    value = str(raw_host or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host
    return value


def _matches(host: str, pattern: str) -> bool:
    normalized = pattern.strip().lower()
    if normalized.startswith("*."):
        return host.endswith(normalized[1:]) and host != normalized[2:]
    return host == normalized


def _private_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


class PrivateReadinessTrustedHostMiddleware:
    """Allow private-IP Host headers only for load-balancer readiness probes."""

    def __init__(
        self,
        app,
        *,
        allowed_hosts: Iterable[str],
        readiness_paths: Iterable[str] = ("/api/ready",),
    ) -> None:
        self.app = app
        self.allowed_hosts = tuple(host.strip().lower() for host in allowed_hosts if host.strip())
        self.readiness_paths = frozenset(str(path).rstrip("/") or "/" for path in readiness_paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = _hostname(Headers(scope=scope).get("host", ""))
        path = str(scope.get("path") or "/").rstrip("/") or "/"
        trusted = any(_matches(host, pattern) for pattern in self.allowed_hosts)
        private_readiness = scope["type"] == "http" and path in self.readiness_paths and _private_ip(host)
        if trusted or private_readiness:
            await self.app(scope, receive, send)
            return

        response = PlainTextResponse("Invalid host header", status_code=400)
        await response(scope, receive, send)
