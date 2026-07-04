"""Authentication, RBAC principals, and multi-tenancy resolution (M5).

Three token namespaces coexist, each with its own lookup:
  - njt_  join tokens   (single-use, enroll a runner)  -> store.join_tokens
  - nrt_  runner tokens (machine identity)             -> store.runners (via _require_runner)
  - nut_  user tokens   (human session)                -> store.sessions

This module handles the human/user side and the request principal. Runner auth
stays separate (runner_hub.authenticate_runner) so a user token can never act as
a runner and vice-versa. Password hashing and tokens use the stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from netcode.store import DEFAULT_ORG_ID, PlatformStore

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_HOURS = 12
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def auth_enabled() -> bool:
    return os.environ.get("NETCODE_AUTH", "").strip().lower() in ("1", "on", "true", "yes")


def admin_token() -> str:
    return os.environ.get("NETCODE_ADMIN_TOKEN", "").strip()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(hash_b64)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_session(store: PlatformStore, user_id: str, org_id: str) -> str:
    token = f"nut_{secrets.token_urlsafe(32)}"
    expires_at = (_now() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    store.create_session(token_hash(token), user_id, org_id, expires_at)
    return token


@dataclass(frozen=True)
class Principal:
    kind: str            # 'user' | 'system' | 'anon'
    org_id: str
    role: str | None     # 'admin' | 'operator' | 'viewer' | None
    user_id: str | None = None
    email: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.kind in ("user", "system")

    def has_role(self, required: str) -> bool:
        return self.role is not None and ROLE_RANK.get(self.role, 0) >= ROLE_RANK.get(required, 99)


SYSTEM_PRINCIPAL = Principal(kind="system", org_id=DEFAULT_ORG_ID, role="admin")


def resolve_principal(store: PlatformStore, authorization: str | None) -> Principal:
    """Resolve the human principal for a request.

    Auth OFF  -> everyone is a system admin on the default org (current behavior).
    Auth ON   -> a valid user session, else the break-glass admin token, else anon.
    Runner tokens are intentionally NOT resolved here (see _require_runner).
    """
    if not auth_enabled():
        return SYSTEM_PRINCIPAL

    token = (authorization or "").removeprefix("Bearer ").strip()
    if token:
        session = store.session_by_token_hash(token_hash(token))
        if session and str(session.get("expires_at", "")) > _now().isoformat():
            user = store.get_user(str(session["user_id"]))
            if user and user.status == "active":
                return Principal(kind="user", org_id=session["org_id"], role=user.role, user_id=user.id, email=user.email)
        env_admin = admin_token()
        if env_admin and hmac.compare_digest(token, env_admin):
            return Principal(kind="system", org_id=DEFAULT_ORG_ID, role="admin")
    return Principal(kind="anon", org_id=DEFAULT_ORG_ID, role=None)
