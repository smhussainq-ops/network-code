"""Fail-closed production configuration checks for the Netcode control plane."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qs, unquote, urlparse


_PRODUCTION_ENVS = {"prod", "production", "staging"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_WEAK_PASSWORDS = {
    "admin",
    "admin123",
    "changeme",
    "change-me",
    "password",
    "replace_me",
    "replace-me",
    "netcode",
    "rezonance",
}


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _enabled(env: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    raw = _value(env, name).lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _strong_service_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return len(value) >= 32 and not any(
        marker in normalized for marker in ("replace", "change-me", "changeme", "example")
    )


def _database_url_issues(database_url: str) -> list[str]:
    """Validate a production PostgreSQL URL without returning secret material."""
    try:
        parsed = urlparse(database_url)
        port = parsed.port
    except ValueError:
        return ["DATABASE_URL must be a valid PostgreSQL URL"]

    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        return ["DATABASE_URL must use PostgreSQL"]
    if not parsed.username or not parsed.password or not parsed.path.strip("/"):
        return ["DATABASE_URL must include database credentials and name"]
    if port is not None and not 1 <= port <= 65535:
        return ["DATABASE_URL must use a valid database port"]

    password = unquote(parsed.password)
    if any(marker in password.lower() for marker in ("replace", "changeme", "change-me")):
        return ["DATABASE_URL must not contain placeholder credentials"]
    if len(password) < 16:
        return ["DATABASE_URL must contain a strong database secret"]

    sslmode = parse_qs(parsed.query).get("sslmode", [""])[-1].lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        return ["DATABASE_URL must require TLS"]
    return []


def is_production_environment(env: Mapping[str, str]) -> bool:
    return _value(env, "NETCODE_ENV").lower() in _PRODUCTION_ENVS


def collect_netcode_production_issues(
    env: Mapping[str, str],
    *,
    persisted_auth_users: bool,
) -> list[str]:
    """Return safe, secret-free reasons a production Netcode process must not start."""
    if not is_production_environment(env):
        return []

    issues: list[str] = []
    if _value(env, "NETCODE_EXECUTION").lower() != "runner":
        issues.append("NETCODE_EXECUTION must be runner")
    if not _value(env, "NETCODE_RUNNER_POOL"):
        issues.append("NETCODE_RUNNER_POOL is required")
    if not _enabled(env, "NETCODE_AUTH"):
        issues.append("NETCODE_AUTH must be enabled")
    if not _enabled(env, "NETCODE_REQUIRE_APPROVAL"):
        issues.append("NETCODE_REQUIRE_APPROVAL must be enabled")
    if not _enabled(env, "NETCODE_LICENSE_ENFORCEMENT"):
        issues.append("NETCODE_LICENSE_ENFORCEMENT must be enabled")

    database_url = _value(env, "DATABASE_URL")
    database_fields = {
        "host": _value(env, "NETCODE_DATABASE_HOST"),
        "name": _value(env, "NETCODE_DATABASE_NAME"),
        "user": _value(env, "NETCODE_DATABASE_USER"),
        "password": str(env.get("NETCODE_DATABASE_PASSWORD", "") or ""),
    }
    if database_url:
        issues.extend(_database_url_issues(database_url))
    elif not all(database_fields.values()):
        issues.append("DATABASE_URL or complete NETCODE_DATABASE_* fields are required")
    else:
        password = database_fields["password"]
        if len(password) < 16 or any(
            marker in password.lower() for marker in ("replace", "changeme", "change-me")
        ):
            issues.append("NETCODE_DATABASE_PASSWORD must be a strong secret")
        port = _value(env, "NETCODE_DATABASE_PORT") or "5432"
        try:
            valid_port = 1 <= int(port) <= 65535
        except ValueError:
            valid_port = False
        if not valid_port:
            issues.append("NETCODE_DATABASE_PORT must be between 1 and 65535")
        sslmode = (_value(env, "NETCODE_DATABASE_SSLMODE") or "").lower()
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            issues.append("NETCODE_DATABASE_SSLMODE must require TLS")
    if not _value(env, "NETCODE_WORKSPACE"):
        issues.append("NETCODE_WORKSPACE must reference durable workspace storage")

    allowed_hosts = [
        host.strip()
        for host in _value(env, "NETCODE_ALLOWED_HOSTS").split(",")
        if host.strip()
    ]
    if not allowed_hosts or any(host == "*" for host in allowed_hosts):
        issues.append("NETCODE_ALLOWED_HOSTS must contain explicit production hosts")

    bridge_token = _value(env, "NETCODE_REZ_BRIDGE_TOKEN")
    if not _strong_service_secret(bridge_token):
        issues.append("NETCODE_REZ_BRIDGE_TOKEN must contain at least 32 characters")

    entitlement_url = _value(env, "NETCODE_ENTITLEMENT_URL")
    if not _valid_http_url(entitlement_url):
        issues.append("NETCODE_ENTITLEMENT_URL must be an http(s) URL")
    if not _strong_service_secret(_value(env, "NETCODE_ENTITLEMENT_TOKEN")):
        issues.append("NETCODE_ENTITLEMENT_TOKEN must contain at least 32 characters")

    rez_trigger_url = _value(env, "NETCODE_REZ_TRIGGER_URL")
    if not _valid_http_url(rez_trigger_url):
        issues.append("NETCODE_REZ_TRIGGER_URL must be an http(s) URL")
    if not _strong_service_secret(_value(env, "NETCODE_REZ_TRIGGER_TOKEN")):
        issues.append("NETCODE_REZ_TRIGGER_TOKEN must contain at least 32 characters")
    if not _value(env, "NETCODE_REZ_ENVIRONMENT_ID"):
        issues.append("NETCODE_REZ_ENVIRONMENT_ID is required")

    for worker_env in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        worker_count = _value(env, worker_env)
        if worker_count and worker_count != "1":
            issues.append(f"{worker_env} must be 1 until runner and shell state is externalized")

    admin_token = _value(env, "NETCODE_ADMIN_TOKEN")
    if admin_token and not _strong_service_secret(admin_token):
        issues.append("NETCODE_ADMIN_TOKEN must contain at least 32 characters when configured")

    email = _value(env, "NETCODE_BOOTSTRAP_ADMIN_EMAIL")
    password = str(env.get("NETCODE_BOOTSTRAP_ADMIN_PASSWORD", "") or "")
    if bool(email) != bool(password):
        issues.append("both Netcode bootstrap admin variables must be set together")
    normalized_password = password.strip().lower()
    if password and (
        len(password) < 12
        or normalized_password in _WEAK_PASSWORDS
        or any(marker in normalized_password for marker in ("replace", "changeme", "change-me"))
    ):
        issues.append("NETCODE_BOOTSTRAP_ADMIN_PASSWORD must be a strong secret")
    if not persisted_auth_users and not (email and password):
        issues.append("an existing admin or explicit bootstrap admin is required")

    return issues
