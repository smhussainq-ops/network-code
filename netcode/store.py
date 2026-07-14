"""Durable store for changes, jobs, runners, and (M5) orgs/users/sessions.

Backed by SQLite by default; Postgres-ready via DATABASE_URL. All SQL uses `?`
positional placeholders; the connection wrapper rewrites them to `%s` for the
Postgres engine so call sites stay engine-agnostic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from netcode.paths import WorkspacePaths
from netcode.yamlio import read_yaml


DEFAULT_ORG_ID = "org_default"
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "reconcile_required"})


class JobQueueFullError(RuntimeError):
    pass


def job_queue_limit() -> int:
    try:
        configured = int(os.environ.get("NETCODE_MAX_QUEUED_JOBS", "20000") or "20000")
    except ValueError:
        configured = 20_000
    return max(100, min(configured, 100_000))


def queue_age_alert_seconds() -> int:
    try:
        configured = int(os.environ.get("NETCODE_QUEUE_AGE_ALERT_SECONDS", "300") or "300")
    except ValueError:
        configured = 300
    return max(30, min(configured, 86_400))


_RETRY_SAFE_JOB_ACTIONS = frozenset(
    {
        "lab_verify",
        "manager_probe",
        "manager_snapshot",
        "manager_preview",
        "manager_validate",
        "manager_poll",
        "manager_verify",
    }
)


def job_is_retry_safe(action: str) -> bool:
    """Return whether an expired claim may be executed again without a write.

    Read jobs are created only through ``create_read_job`` and receive the
    ``read_`` prefix there. Every other action must be explicitly listed; an
    unknown action therefore fails closed into reconciliation.
    """
    normalized = str(action or "").strip().lower()
    return normalized.startswith("read_") or normalized in _RETRY_SAFE_JOB_ACTIONS


def job_lease_seconds() -> int:
    try:
        configured = int(os.environ.get("NETCODE_JOB_LEASE_SECONDS", "90") or "90")
    except ValueError:
        configured = 90
    return max(30, min(configured, 900))


def job_max_attempts() -> int:
    try:
        configured = int(os.environ.get("NETCODE_JOB_MAX_ATTEMPTS", "3") or "3")
    except ValueError:
        configured = 3
    return max(1, min(configured, 10))


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def job_device_id(payload: dict[str, Any] | None) -> str:
    """Extract the canonical execution target used for device serialization."""
    value = payload or {}
    device = value.get("device") if isinstance(value.get("device"), dict) else {}
    ownership = value.get("ownership") if isinstance(value.get("ownership"), dict) else {}
    candidate = (
        value.get("manager_id")
        or value.get("device_id")
        or device.get("id")
        or ownership.get("device_id")
        or ""
    )
    return str(candidate).strip().lower()


def job_idempotency_key(
    *,
    org_id: str,
    change_id: str,
    action: str,
    device_id: str,
    payload: dict[str, Any],
) -> str:
    """Create a stable opaque key for one reviewed device operation."""
    canonical = json.dumps(
        {
            "org_id": str(org_id),
            "change_id": str(change_id),
            "action": str(action).strip().lower(),
            "device_id": str(device_id).strip().lower(),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"nop_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _is_active_device_conflict(exc: Exception) -> bool:
    diagnostic = getattr(exc, "diag", None)
    if str(getattr(diagnostic, "constraint_name", "")) == "idx_jobs_one_active_device":
        return True
    message = str(exc).lower()
    return (
        "idx_jobs_one_active_device" in message
        or "unique constraint failed: jobs.org_id, jobs.device_id" in message
    )


def _contains_redacted_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_redacted_secret(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted_secret(item) for item in value)
    return value == "***redacted***"


def execution_phase_for_job(action: str) -> str:
    normalized = str(action or "").strip().lower()
    if normalized.startswith("lab_"):
        return normalized.removeprefix("lab_")
    if normalized == "read_verify":
        return "verify"
    if normalized == "read_rez_discover_network":
        return "discovery"
    if normalized.startswith("manager_"):
        return normalized.removeprefix("manager_")
    return ""


def database_url(paths: WorkspacePaths) -> str:
    """Resolve SQLite, a complete DATABASE_URL, or ECS-injected RDS fields."""
    configured_url = os.environ.get("DATABASE_URL", "").strip()
    if configured_url:
        return configured_url

    components = {
        "host": os.environ.get("NETCODE_DATABASE_HOST", "").strip(),
        "port": os.environ.get("NETCODE_DATABASE_PORT", "").strip() or "5432",
        "name": os.environ.get("NETCODE_DATABASE_NAME", "").strip(),
        "user": os.environ.get("NETCODE_DATABASE_USER", "").strip(),
        "password": os.environ.get("NETCODE_DATABASE_PASSWORD", ""),
    }
    if any(value for key, value in components.items() if key != "port"):
        missing = [key for key in ("host", "name", "user", "password") if not components[key]]
        if missing:
            raise RuntimeError(
                "Incomplete NETCODE_DATABASE_* configuration: " + ", ".join(sorted(missing))
            )
        try:
            port = int(components["port"])
        except ValueError as exc:
            raise RuntimeError("NETCODE_DATABASE_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("NETCODE_DATABASE_PORT must be between 1 and 65535")
        sslmode = os.environ.get("NETCODE_DATABASE_SSLMODE", "require").strip().lower() or "require"
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise RuntimeError("NETCODE_DATABASE_SSLMODE must require TLS")
        query = urlencode({"sslmode": sslmode})
        return (
            f"postgresql://{quote(components['user'], safe='')}:{quote(components['password'], safe='')}"
            f"@{components['host']}:{port}/{quote(components['name'], safe='')}?{query}"
        )

    return f"sqlite:///{paths.database}"


def _engine_for(url: str) -> str:
    return "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"


class _EngineConn:
    """Thin wrapper giving one execute()/context-manager contract across sqlite3 and psycopg.

    - rewrites `?` -> `%s` for Postgres so every call site can use `?`
    - commits on clean exit, rolls back on error, then closes (connection-per-op)
    """

    def __init__(self, raw: Any, engine: str):
        self._raw = raw
        self._engine = engine

    def execute(self, sql: str, params: tuple | list = ()):  # noqa: ANN201
        if self._engine == "postgres":
            sql = sql.replace("?", "%s")
        return self._raw.execute(sql, params)

    def __enter__(self) -> "_EngineConn":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _change_index_metadata(intent_path: Path) -> dict[str, str]:
    """Extract non-secret search fields from a desired-change document."""
    try:
        intent = read_yaml(intent_path)
    except (OSError, ValueError):
        intent = {}
    metadata = intent.get("metadata") if isinstance(intent.get("metadata"), dict) else {}
    custom = intent.get("custom") if isinstance(intent.get("custom"), dict) else {}
    raw_source = str(metadata.get("source") or "").strip().lower()
    normalized_source = {
        "netcode_ansible": "ansible",
        "rez": "rez_rca",
    }.get(raw_source, raw_source)
    if not normalized_source:
        normalized_source = "ansible" if "ansible" in {part.lower() for part in intent_path.parts} else "netcode"
    return {
        "title": str(metadata.get("title") or custom.get("description") or intent_path.name).strip()[:240],
        "source": normalized_source[:80],
        "site": str(intent.get("site") or "").strip()[:160],
        "workflow_type": str(intent.get("change_type") or "").strip()[:120],
    }


def change_audit_id(change_id: str | None, created_at: str | None = None) -> str:
    """Return the stable customer-facing alias for a canonical change UUID."""
    canonical = "".join(ch for ch in str(change_id or "") if ch.isalnum()).upper()
    token = canonical[:12] or "UNKNOWN"
    created_date = str(created_at or "").split("T", 1)[0]
    date_token = "".join(ch for ch in created_date if ch.isdigit())
    return f"REZ-CHG-{date_token}-{token}" if date_token else f"REZ-CHG-{token}"


@dataclass(frozen=True)
class ChangeRecord:
    id: str
    status: str
    workflow_state: str
    intent_path: str
    device_id: str | None
    requested_by: str
    created_at: str
    updated_at: str
    last_job_id: str | None
    result: dict[str, Any] | None
    org_id: str = DEFAULT_ORG_ID
    created_by_user_id: str | None = None
    title: str = ""
    source: str = ""
    site: str = ""
    workflow_type: str = ""


@dataclass(frozen=True)
class JobRecord:
    id: str
    change_id: str
    action: str
    status: str
    message: str
    created_at: str
    updated_at: str
    result: dict[str, Any] | None
    pool: str | None = None
    payload: dict[str, Any] | None = None
    claimed_by: str | None = None
    signature: str | None = None
    org_id: str = DEFAULT_ORG_ID
    target_runner_id: str | None = None
    lease_expires_at: str | None = None
    lease_heartbeat_at: str | None = None
    attempt_count: int = 0
    device_id: str = ""
    idempotency_key: str | None = None
    lease_token: str | None = None


@dataclass(frozen=True)
class RunnerRecord:
    id: str
    name: str
    pool: str
    status: str
    version: str
    created_at: str
    last_seen: str | None
    org_id: str = DEFAULT_ORG_ID
    inventory_revision: str = ""
    device_count: int = 0
    drain_requested: bool = False
    draining_at: str | None = None
    token_expires_at: str | None = None
    token_rotate_after: str | None = None
    token_rotated_at: str | None = None
    previous_token_expires_at: str | None = None
    pending_token_valid_until: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True)
class UserRecord:
    id: str
    org_id: str
    email: str
    role: str
    status: str
    created_at: str


@dataclass(frozen=True)
class WorkflowEventRecord:
    id: str
    change_id: str
    action: str
    from_state: str
    to_state: str
    message: str
    created_at: str
    evidence: dict[str, Any] | None


@dataclass(frozen=True)
class ExecutionEventRecord:
    id: str
    job_id: str
    change_id: str
    org_id: str
    device_id: str
    phase: str
    stage: str
    status: str
    message: str
    sequence: int
    current_step: int | None
    total_steps: int | None
    command: str | None
    created_at: str


class PlatformStore:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self.db_path = paths.database
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.url = database_url(paths)
        self.engine = _engine_for(self.url)
        self._init()

    def _connect(self) -> _EngineConn:
        if self.engine == "postgres":
            import psycopg  # optional dep; only needed when DATABASE_URL is postgres
            from psycopg.rows import dict_row

            raw = psycopg.connect(self.url, row_factory=dict_row)
            return _EngineConn(raw, "postgres")
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA busy_timeout=5000")
        return _EngineConn(raw, "sqlite")

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS changes (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    workflow_state TEXT NOT NULL DEFAULT 'draft',
                    intent_path TEXT NOT NULL,
                    device_id TEXT,
                    requested_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_job_id TEXT,
                    result_json TEXT
                )
                """
            )
            self._ensure_column(conn, "changes", "workflow_state", "TEXT NOT NULL DEFAULT 'draft'")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    change_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id TEXT PRIMARY KEY,
                    change_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    evidence_json TEXT,
                    FOREIGN KEY(change_id) REFERENCES changes(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_events (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    change_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    device_id TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    current_step INTEGER,
                    total_steps INTEGER,
                    command TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, sequence)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_events_change "
                "ON execution_events (org_id, change_id, created_at, sequence)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_events_job "
                "ON execution_events (job_id, sequence)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runners (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pool TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    hmac_secret TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen TEXT
                )
                """
            )
            self._ensure_column(conn, "runners", "inventory_revision", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runners", "device_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runners", "drain_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runners", "draining_at", "TEXT")
            self._ensure_column(conn, "runners", "token_expires_at", "TEXT")
            self._ensure_column(conn, "runners", "token_rotate_after", "TEXT")
            self._ensure_column(conn, "runners", "token_rotated_at", "TEXT")
            self._ensure_column(conn, "runners", "previous_token_hash", "TEXT")
            self._ensure_column(conn, "runners", "previous_token_expires_at", "TEXT")
            self._ensure_column(conn, "runners", "pending_token_hash", "TEXT")
            self._ensure_column(conn, "runners", "pending_token_expires_at", "TEXT")
            self._ensure_column(conn, "runners", "pending_token_rotate_after", "TEXT")
            self._ensure_column(conn, "runners", "pending_token_valid_until", "TEXT")
            self._ensure_column(conn, "runners", "revoked_at", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runner_security_events (
                    id TEXT PRIMARY KEY,
                    runner_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runner_security_events "
                "ON runner_security_events (org_id, runner_id, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_catalog (
                    org_id TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    display_id TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 22,
                    platform TEXT NOT NULL,
                    serial TEXT NOT NULL DEFAULT '',
                    site TEXT,
                    role TEXT,
                    groups_json TEXT NOT NULL DEFAULT '[]',
                    location_json TEXT NOT NULL DEFAULT '{}',
                    management_json TEXT NOT NULL DEFAULT '{}',
                    runner_id TEXT NOT NULL,
                    runner_pool TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'runner_inventory',
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, canonical_id)
                )
                """
            )
            self._ensure_column(conn, "device_catalog", "management_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "device_catalog", "location_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "device_catalog", "serial", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_aliases (
                    org_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    PRIMARY KEY (org_id, alias)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_catalog_runner ON device_catalog (org_id, runner_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_catalog_site ON device_catalog (org_id, site, canonical_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_catalog_role ON device_catalog (org_id, role, canonical_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_catalog_platform ON device_catalog (org_id, platform, canonical_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_catalog_serial ON device_catalog (org_id, serial)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_aliases_canonical ON device_aliases (org_id, canonical_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shell_sessions (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    display_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    runner_id TEXT,
                    runner_pool TEXT,
                    status TEXT NOT NULL,
                    guard_enabled INTEGER NOT NULL DEFAULT 0,
                    change_id TEXT,
                    started_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    ended_at TEXT,
                    transcript_path TEXT NOT NULL,
                    command_count INTEGER NOT NULL DEFAULT 0,
                    output_bytes INTEGER NOT NULL DEFAULT 0,
                    device_touched INTEGER NOT NULL DEFAULT 0,
                    end_reason TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shell_sessions_org_activity "
                "ON shell_sessions (org_id, last_activity DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shell_sessions_device "
                "ON shell_sessions (org_id, device_id, last_activity DESC)"
            )
            self._ensure_column(conn, "shell_sessions", "end_reason", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS join_tokens (
                    token_hash TEXT PRIMARY KEY,
                    pool TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    used_at TEXT
                )
                """
            )
            self._ensure_column(conn, "jobs", "pool", "TEXT")
            self._ensure_column(conn, "jobs", "payload_json", "TEXT")
            self._ensure_column(conn, "jobs", "claimed_by", "TEXT")
            self._ensure_column(conn, "jobs", "signature", "TEXT")
            self._ensure_column(conn, "jobs", "target_runner_id", "TEXT")
            self._ensure_column(conn, "jobs", "lease_token_hash", "TEXT")
            self._ensure_column(conn, "jobs", "lease_expires_at", "TEXT")
            self._ensure_column(conn, "jobs", "lease_heartbeat_at", "TEXT")
            self._ensure_column(conn, "jobs", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "device_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "idempotency_key", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_runner_target "
                "ON jobs (pool, status, target_runner_id, created_at)"
            )

            # M5: tenancy + auth. Additive and default-valued so it is safe with
            # NETCODE_AUTH off (existing rows are backfilled to the default org).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orgs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    UNIQUE(org_id, email)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            # Fleet rollouts: one intent orchestrated over many devices as
            # canary -> batch waves. Each target device gets its OWN change record,
            # so the whole single-change safety spine (plan/dry-run/apply/verify,
            # evidence, state machine) applies per device; these tables only add
            # the wave structure and halt state on top.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rollouts (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    values_json TEXT,
                    status TEXT NOT NULL,
                    canary_size INTEGER NOT NULL,
                    batch_size INTEGER NOT NULL,
                    requested_by TEXT NOT NULL,
                    created_by_user_id TEXT,
                    parent_rollout_id TEXT,
                    retry_scope TEXT,
                    halt_reason TEXT,
                    current_wave INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rollout_targets (
                    rollout_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    wave_index INTEGER NOT NULL,
                    change_id TEXT,
                    intent_path TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (rollout_id, device_id),
                    FOREIGN KEY(rollout_id) REFERENCES rollouts(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rollout_targets_activity "
                "ON rollout_targets (rollout_id, status, wave_index, device_id)"
            )
            self._ensure_column(conn, "rollouts", "approved_by", "TEXT")
            self._ensure_column(conn, "rollouts", "approved_at", "TEXT")
            self._ensure_column(conn, "rollouts", "parent_rollout_id", "TEXT")
            self._ensure_column(conn, "rollouts", "retry_scope", "TEXT")
            for table in ("changes", "jobs", "runners", "join_tokens"):
                self._ensure_column(conn, table, "org_id", f"TEXT DEFAULT '{DEFAULT_ORG_ID}'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_device_claim "
                "ON jobs (org_id, device_id, status, lease_expires_at)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_org_idempotency "
                "ON jobs (org_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_device "
                "ON jobs (org_id, device_id) "
                "WHERE device_id <> '' AND status IN ('running', 'completing')"
            )
            self._ensure_column(conn, "changes", "created_by_user_id", "TEXT")
            self._ensure_column(conn, "changes", "title", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "changes", "source", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "changes", "site", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "changes", "workflow_type", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_created ON changes (org_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_state ON changes (org_id, workflow_state, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_device ON changes (org_id, device_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_requester ON changes (org_id, requested_by, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_source ON changes (org_id, source, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_site ON changes (org_id, site, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_org_workflow ON changes (org_id, workflow_type, created_at)")
            # Seed the default org idempotently so pre-flag data always has an owner.
            conn.execute(
                "INSERT INTO orgs (id, name, slug, created_at) SELECT ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM orgs WHERE id = ?)",
                (DEFAULT_ORG_ID, "Default", "default", utc_now(), DEFAULT_ORG_ID),
            )

    def _ensure_column(self, conn: _EngineConn, table: str, column: str, definition: str) -> None:
        if self.engine == "postgres":
            existing = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            ).fetchall()
            columns = {row["column_name"] for row in existing}
        else:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_change(
        self,
        intent_path: Path,
        device_id: str | None,
        requested_by: str = "netcode-user",
        org_id: str = DEFAULT_ORG_ID,
        created_by_user_id: str | None = None,
    ) -> ChangeRecord:
        now = utc_now()
        change_id = str(uuid.uuid4())
        index = _change_index_metadata(intent_path)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO changes
                (id, status, workflow_state, intent_path, device_id, requested_by, created_at, updated_at,
                 last_job_id, result_json, org_id, created_by_user_id, title, source, site, workflow_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change_id, "draft", "draft", str(intent_path), device_id, requested_by, now, now,
                    None, None, org_id, created_by_user_id, index["title"], index["source"],
                    index["site"], index["workflow_type"],
                ),
            )
        return self.get_change(change_id)

    def get_or_create_change(
        self,
        intent_path: Path,
        device_id: str | None,
        requested_by: str = "netcode-user",
        org_id: str = DEFAULT_ORG_ID,
        created_by_user_id: str | None = None,
    ) -> ChangeRecord:
        intent = str(intent_path)
        with self._connect() as conn:
            # org_id in the match prevents attaching to another tenant's change.
            row = conn.execute(
                """
                SELECT * FROM changes
                WHERE intent_path = ? AND COALESCE(device_id, '') = COALESCE(?, '') AND org_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (intent, device_id, org_id),
            ).fetchone()
        if row:
            return self._change(row)
        return self.create_change(intent_path, device_id, requested_by=requested_by, org_id=org_id, created_by_user_id=created_by_user_id)

    def create_job(self, change_id: str, action: str) -> JobRecord:
        now = utc_now()
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            org_row = conn.execute("SELECT org_id FROM changes WHERE id = ?", (change_id,)).fetchone()
            org_id = (org_row["org_id"] if org_row else None) or DEFAULT_ORG_ID
            conn.execute(
                """
                INSERT INTO jobs
                (id, change_id, action, status, message, created_at, updated_at, result_json, org_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, change_id, action, "queued", "Queued", now, now, None, org_id),
            )
            conn.execute(
                "UPDATE changes SET last_job_id = ?, updated_at = ? WHERE id = ?",
                (job_id, now, change_id),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, status: str, message: str, result: dict[str, Any] | None = None) -> JobRecord:
        now = utc_now()
        result_json = json.dumps(result) if result is not None else None
        with self._connect() as conn:
            if status in {"queued", "running", "completing"}:
                conn.execute(
                    "UPDATE jobs SET status = ?, message = ?, updated_at = ?, result_json = ? WHERE id = ?",
                    (status, message, now, result_json, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ?, message = ?, updated_at = ?, result_json = ?, "
                    "lease_token_hash = NULL, lease_expires_at = NULL, lease_heartbeat_at = NULL WHERE id = ?",
                    (status, message, now, result_json, job_id),
                )
        return self.get_job(job_id)

    def update_change(self, change_id: str, status: str, result: dict[str, Any] | None = None, workflow_state: str | None = None) -> ChangeRecord:
        now = utc_now()
        result_json = json.dumps(result) if result is not None else None
        result = result or {}
        title = str(result.get("title") or "").strip()[:240]
        raw_source = str(result.get("source") or "").strip().lower()
        source = {"netcode_ansible": "ansible", "rez": "rez_rca"}.get(raw_source, raw_source)[:80]
        workflow_type = str(result.get("change_type") or "").strip()[:120]
        with self._connect() as conn:
            if workflow_state is None:
                conn.execute(
                    "UPDATE changes SET status = ?, updated_at = ?, result_json = ?, "
                    "title = CASE WHEN ? <> '' THEN ? ELSE title END, "
                    "source = CASE WHEN ? <> '' THEN ? ELSE source END, "
                    "workflow_type = CASE WHEN ? <> '' THEN ? ELSE workflow_type END WHERE id = ?",
                    (status, now, result_json, title, title, source, source, workflow_type, workflow_type, change_id),
                )
            else:
                conn.execute(
                    "UPDATE changes SET status = ?, workflow_state = ?, updated_at = ?, result_json = ?, "
                    "title = CASE WHEN ? <> '' THEN ? ELSE title END, "
                    "source = CASE WHEN ? <> '' THEN ? ELSE source END, "
                    "workflow_type = CASE WHEN ? <> '' THEN ? ELSE workflow_type END WHERE id = ?",
                    (
                        status, workflow_state, now, result_json, title, title, source, source,
                        workflow_type, workflow_type, change_id,
                    ),
                )
        return self.get_change(change_id)

    def record_workflow_event(
        self,
        change_id: str,
        action: str,
        from_state: str,
        to_state: str,
        message: str,
        evidence: dict[str, Any] | None = None,
    ) -> WorkflowEventRecord:
        now = utc_now()
        event_id = str(uuid.uuid4())
        evidence_json = json.dumps(evidence) if evidence is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_events
                (id, change_id, action, from_state, to_state, message, created_at, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, change_id, action, from_state, to_state, message, now, evidence_json),
            )
            conn.execute(
                "UPDATE changes SET workflow_state = ?, updated_at = ? WHERE id = ?",
                (to_state, now, change_id),
            )
        return self.get_workflow_event(event_id)

    def get_change(self, change_id: str) -> ChangeRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown change {change_id}")
        return self._change(row)

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown job {job_id}")
        return self._job(row)

    def get_workflow_event(self, event_id: str) -> WorkflowEventRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflow_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown workflow event {event_id}")
        return self._workflow_event(row)

    def list_changes(self, limit: int = 50, org_id: str | None = None) -> list[ChangeRecord]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute("SELECT * FROM changes ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM changes WHERE org_id = ? ORDER BY created_at DESC LIMIT ?", (org_id, limit)
                ).fetchall()
        return [self._change(row) for row in rows]

    def search_changes(
        self,
        *,
        org_id: str,
        query: str = "",
        device_id: str = "",
        state: str = "",
        requested_by: str = "",
        source: str = "",
        site: str = "",
        workflow_type: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[ChangeRecord], int]:
        """Return one tenant-scoped, bounded page of durable change history."""
        clauses = ["org_id = ?"]
        params: list[Any] = [org_id]

        def contains(column: str, value: str) -> None:
            normalized = value.strip().lower()
            if normalized:
                clauses.append(f"LOWER(COALESCE({column}, '')) LIKE ?")
                params.append(f"%{normalized}%")

        contains("device_id", device_id)
        contains("requested_by", requested_by)
        contains("site", site)
        contains("workflow_type", workflow_type)
        if state.strip():
            clauses.append("(LOWER(status) = ? OR LOWER(workflow_state) = ?)")
            normalized_state = state.strip().lower()
            params.extend([normalized_state, normalized_state])
        if source.strip():
            normalized_source = source.strip().lower()
            if normalized_source == "rez_rca":
                clauses.append(
                    "(LOWER(COALESCE(source, '')) = ? OR "
                    "(COALESCE(source, '') = '' AND "
                    "(LOWER(COALESCE(result_json, '')) LIKE ? OR LOWER(intent_path) LIKE ?)))"
                )
                params.extend([normalized_source, "%rez_rca%", "%/rca/%"])
            elif normalized_source == "ansible":
                clauses.append(
                    "(LOWER(COALESCE(source, '')) = ? OR "
                    "(COALESCE(source, '') = '' AND "
                    "(LOWER(COALESCE(result_json, '')) LIKE ? OR LOWER(intent_path) LIKE ?)))"
                )
                params.extend([normalized_source, "%ansible%", "%/ansible/%"])
            elif normalized_source == "netcode":
                clauses.append(
                    "(LOWER(COALESCE(source, '')) = ? OR "
                    "(COALESCE(source, '') = '' AND LOWER(COALESCE(result_json, '')) NOT LIKE ? "
                    "AND LOWER(COALESCE(result_json, '')) NOT LIKE ? AND LOWER(intent_path) NOT LIKE ? "
                    "AND LOWER(intent_path) NOT LIKE ?))"
                )
                params.extend([normalized_source, "%rez_rca%", "%ansible%", "%/rca/%", "%/ansible/%"])
            else:
                clauses.append("LOWER(COALESCE(source, '')) = ?")
                params.append(normalized_source)
        if created_from.strip():
            clauses.append("created_at >= ?")
            params.append(created_from.strip())
        if created_to.strip():
            clauses.append("created_at <= ?")
            params.append(created_to.strip())
        if query.strip():
            needle = f"%{query.strip().lower()}%"
            clauses.append(
                "(LOWER(id) LIKE ? OR LOWER(COALESCE(title, '')) LIKE ? OR "
                "LOWER(COALESCE(device_id, '')) LIKE ? OR LOWER(requested_by) LIKE ? OR "
                "LOWER(intent_path) LIKE ? OR LOWER(COALESCE(result_json, '')) LIKE ? OR "
                "LOWER(COALESCE(source, '')) LIKE ? OR LOWER(COALESCE(site, '')) LIKE ? OR "
                "LOWER(COALESCE(workflow_type, '')) LIKE ?)"
            )
            params.extend([needle] * 9)

        where = " AND ".join(clauses)
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, min(int(offset), 1_000_000))
        with self._connect() as conn:
            count_row = conn.execute(f"SELECT COUNT(*) AS total FROM changes WHERE {where}", params).fetchone()
            rows = conn.execute(
                f"SELECT * FROM changes WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, bounded_limit, bounded_offset],
            ).fetchall()
        total = int(count_row["total"]) if count_row else 0
        return [self._change(row) for row in rows], total

    def list_jobs(self, limit: int = 50, org_id: str | None = None) -> list[JobRecord]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE org_id = ? ORDER BY created_at DESC LIMIT ?", (org_id, limit)
                ).fetchall()
        return [self._job(row) for row in rows]

    def list_workflow_events(self, change_id: str, limit: int = 100) -> list[WorkflowEventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_events WHERE change_id = ? ORDER BY created_at ASC LIMIT ?",
                (change_id, limit),
            ).fetchall()
        return [self._workflow_event(row) for row in rows]

    def record_execution_event(
        self,
        *,
        event_id: str,
        job_id: str,
        change_id: str,
        org_id: str,
        device_id: str,
        phase: str,
        stage: str,
        status: str,
        message: str,
        sequence: int,
        current_step: int | None = None,
        total_steps: int | None = None,
        command: str | None = None,
    ) -> ExecutionEventRecord:
        """Persist one idempotent runner milestone without changing workflow state."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO execution_events
                (id, job_id, change_id, org_id, device_id, phase, stage, status,
                 message, sequence, current_step, total_steps, command, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (job_id, sequence) DO NOTHING
                """,
                (
                    event_id,
                    job_id,
                    change_id,
                    org_id,
                    device_id,
                    phase,
                    stage,
                    status,
                    message,
                    int(sequence),
                    current_step,
                    total_steps,
                    command,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM execution_events WHERE job_id = ? AND sequence = ?",
                (job_id, int(sequence)),
            ).fetchone()
        if not row:
            raise RuntimeError(f"Execution event {job_id}:{sequence} was not persisted")
        return self._execution_event(row)

    def next_execution_sequence(self, job_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(sequence) AS max_sequence FROM execution_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        maximum = int(row["max_sequence"]) if row and row["max_sequence"] is not None else -1
        return maximum + 1

    def last_execution_event(self, job_id: str) -> ExecutionEventRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_events WHERE job_id = ? ORDER BY sequence DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return self._execution_event(row) if row else None

    def list_execution_events(self, change_id: str, limit: int = 500) -> list[ExecutionEventRecord]:
        bounded = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_events WHERE change_id = ? "
                "ORDER BY created_at ASC, job_id ASC, sequence ASC LIMIT ?",
                (change_id, bounded),
            ).fetchall()
        return [self._execution_event(row) for row in rows]

    def list_execution_events_for_changes(
        self,
        change_ids: list[str],
        *,
        per_change: int = 30,
    ) -> dict[str, list[ExecutionEventRecord]]:
        ids = list(dict.fromkeys(str(item) for item in change_ids if str(item)))
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM execution_events WHERE change_id IN ({marks}) "
                "ORDER BY created_at ASC, job_id ASC, sequence ASC",
                ids,
            ).fetchall()
        grouped: dict[str, list[ExecutionEventRecord]] = {change_id: [] for change_id in ids}
        for row in rows:
            grouped.setdefault(str(row["change_id"]), []).append(self._execution_event(row))
        bounded = max(1, min(int(per_change), 100))
        return {change_id: events[-bounded:] for change_id, events in grouped.items()}

    def _change(self, row: sqlite3.Row) -> ChangeRecord:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return ChangeRecord(
            id=row["id"],
            status=row["status"],
            workflow_state=row["workflow_state"],
            intent_path=row["intent_path"],
            device_id=row["device_id"],
            requested_by=row["requested_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_job_id=row["last_job_id"],
            result=result,
            org_id=self._col(row, "org_id") or DEFAULT_ORG_ID,
            created_by_user_id=self._col(row, "created_by_user_id"),
            title=str(self._col(row, "title") or ""),
            source=str(self._col(row, "source") or ""),
            site=str(self._col(row, "site") or ""),
            workflow_type=str(self._col(row, "workflow_type") or ""),
        )

    @staticmethod
    def _col(row: Any, name: str, default: Any = None) -> Any:
        """Read an optional column across sqlite3.Row and psycopg dict rows."""
        try:
            keys = row.keys()
        except AttributeError:
            keys = row
        if name in keys:
            value = row[name]
            return value if value is not None else default
        return default

    def _job(self, row: sqlite3.Row) -> JobRecord:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        payload_raw = self._col(row, "payload_json")
        payload = json.loads(payload_raw) if payload_raw else None
        return JobRecord(
            id=row["id"],
            change_id=row["change_id"],
            action=row["action"],
            status=row["status"],
            message=row["message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=result,
            pool=self._col(row, "pool"),
            payload=payload,
            claimed_by=self._col(row, "claimed_by"),
            signature=self._col(row, "signature"),
            org_id=self._col(row, "org_id") or DEFAULT_ORG_ID,
            target_runner_id=self._col(row, "target_runner_id"),
            lease_expires_at=self._col(row, "lease_expires_at"),
            lease_heartbeat_at=self._col(row, "lease_heartbeat_at"),
            attempt_count=int(self._col(row, "attempt_count") or 0),
            device_id=str(self._col(row, "device_id") or ""),
            idempotency_key=self._col(row, "idempotency_key"),
        )

    def _runner(self, row: sqlite3.Row) -> RunnerRecord:
        return RunnerRecord(
            id=row["id"],
            name=row["name"],
            pool=row["pool"],
            status=row["status"],
            version=row["version"],
            created_at=row["created_at"],
            last_seen=row["last_seen"],
            org_id=self._col(row, "org_id") or DEFAULT_ORG_ID,
            inventory_revision=str(self._col(row, "inventory_revision") or ""),
            device_count=int(self._col(row, "device_count") or 0),
            drain_requested=bool(self._col(row, "drain_requested") or False),
            draining_at=self._col(row, "draining_at"),
            token_expires_at=self._col(row, "token_expires_at"),
            token_rotate_after=self._col(row, "token_rotate_after"),
            token_rotated_at=self._col(row, "token_rotated_at"),
            previous_token_expires_at=self._col(row, "previous_token_expires_at"),
            pending_token_valid_until=self._col(row, "pending_token_valid_until"),
            revoked_at=self._col(row, "revoked_at"),
        )

    # ── Runner registry & job queue (Phase 0 SaaS split) ──────────────────

    def create_join_token(self, token_hash: str, pool: str, org_id: str = DEFAULT_ORG_ID) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO join_tokens (token_hash, pool, created_at, used_at, org_id) VALUES (?, ?, ?, NULL, ?)",
                (token_hash, pool, utc_now(), org_id),
            )

    def consume_join_token(self, token_hash: str) -> dict[str, str] | None:
        """Atomically mark a join token used; returns {pool, org_id} or None if invalid/replayed."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE join_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (utc_now(), token_hash),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT pool, org_id FROM join_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if not row:
                return None
            return {"pool": row["pool"], "org_id": self._col(row, "org_id") or DEFAULT_ORG_ID}

    def create_runner(
        self,
        name: str,
        pool: str,
        token_hash: str,
        hmac_secret: str,
        org_id: str = DEFAULT_ORG_ID,
        *,
        token_expires_at: str | None = None,
        token_rotate_after: str | None = None,
    ) -> RunnerRecord:
        runner_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runners (id, name, pool, token_hash, hmac_secret, status, version, created_at, "
                "last_seen, org_id, token_expires_at, token_rotate_after) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    runner_id,
                    name,
                    pool,
                    token_hash,
                    hmac_secret,
                    "enrolled",
                    "",
                    now,
                    now,
                    org_id,
                    token_expires_at,
                    token_rotate_after,
                ),
            )
        return self.get_runner(runner_id)

    def get_runner(self, runner_id: str) -> RunnerRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown runner {runner_id}")
        return self._runner(row)

    def runner_by_token_hash(self, token_hash: str) -> RunnerRecord | None:
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runners WHERE revoked_at IS NULL AND ("
                "(token_hash = ? AND (token_expires_at IS NULL OR token_expires_at > ?)) OR "
                "(previous_token_hash = ? AND previous_token_expires_at > ?) OR "
                "(pending_token_hash = ? AND pending_token_valid_until > ?))",
                (token_hash, now, token_hash, now, token_hash, now),
            ).fetchone()
        return self._runner(row) if row else None

    def prepare_runner_token_rotation(
        self,
        runner_id: str,
        org_id: str,
        *,
        presented_token_hash: str,
        pending_token_hash: str,
        pending_token_expires_at: str,
        pending_token_rotate_after: str,
        pending_token_valid_until: str,
    ) -> RunnerRecord:
        """Stage a new credential without invalidating the connector's saved token."""
        now = utc_now()
        with self._connect() as conn:
            if self.engine == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runners WHERE id = ? AND org_id = ?" +
                (" FOR UPDATE" if self.engine == "postgres" else ""),
                (runner_id, org_id),
            ).fetchone()
            if not row or self._col(row, "revoked_at"):
                raise ValueError(f"Unknown or revoked runner {runner_id}")
            if not hmac.compare_digest(str(row["token_hash"]), presented_token_hash):
                raise PermissionError("Only the current connector token can begin rotation")
            current_expiry = str(self._col(row, "token_expires_at") or "")
            if current_expiry and current_expiry <= now:
                raise PermissionError("Connector token has expired; re-enrollment is required")
            pending_hash = str(self._col(row, "pending_token_hash") or "")
            pending_until = str(self._col(row, "pending_token_valid_until") or "")
            if pending_hash and pending_until > now:
                raise RuntimeError("A connector token rotation is already pending confirmation")
            conn.execute(
                "UPDATE runners SET pending_token_hash = ?, pending_token_expires_at = ?, "
                "pending_token_rotate_after = ?, pending_token_valid_until = ? WHERE id = ? AND org_id = ?",
                (
                    pending_token_hash,
                    pending_token_expires_at,
                    pending_token_rotate_after,
                    pending_token_valid_until,
                    runner_id,
                    org_id,
                ),
            )
        return self.get_runner(runner_id)

    def confirm_runner_token_rotation(
        self,
        runner_id: str,
        org_id: str,
        *,
        presented_token_hash: str,
        previous_token_expires_at: str,
    ) -> tuple[RunnerRecord, bool]:
        """Promote a locally saved pending token; confirmation is idempotent."""
        now = utc_now()
        already_confirmed = False
        with self._connect() as conn:
            if self.engine == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runners WHERE id = ? AND org_id = ?" +
                (" FOR UPDATE" if self.engine == "postgres" else ""),
                (runner_id, org_id),
            ).fetchone()
            if not row or self._col(row, "revoked_at"):
                raise ValueError(f"Unknown or revoked runner {runner_id}")
            pending_hash = str(self._col(row, "pending_token_hash") or "")
            if not pending_hash and hmac.compare_digest(str(row["token_hash"]), presented_token_hash):
                already_confirmed = True
            else:
                pending_until = str(self._col(row, "pending_token_valid_until") or "")
                if not pending_hash or not hmac.compare_digest(pending_hash, presented_token_hash):
                    raise PermissionError("The presented connector token is not pending confirmation")
                if not pending_until or pending_until <= now:
                    raise PermissionError("The pending connector token has expired")
                conn.execute(
                    "UPDATE runners SET previous_token_hash = token_hash, previous_token_expires_at = ?, "
                    "token_hash = pending_token_hash, token_expires_at = pending_token_expires_at, "
                    "token_rotate_after = pending_token_rotate_after, token_rotated_at = ?, "
                    "pending_token_hash = NULL, pending_token_expires_at = NULL, "
                    "pending_token_rotate_after = NULL, pending_token_valid_until = NULL "
                    "WHERE id = ? AND org_id = ?",
                    (previous_token_expires_at, now, runner_id, org_id),
                )
        return self.get_runner(runner_id), already_confirmed

    def revoke_runner(self, runner_id: str, org_id: str) -> RunnerRecord:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runners SET revoked_at = ?, status = 'revoked', drain_requested = 1, draining_at = ? "
                "WHERE id = ? AND org_id = ? AND revoked_at IS NULL",
                (now, now, runner_id, org_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown or already revoked runner {runner_id}")
        return self.get_runner(runner_id)

    def record_runner_security_event(
        self,
        runner_id: str,
        org_id: str,
        event: str,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runner_security_events "
                "(id, runner_id, org_id, event, actor, created_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    runner_id,
                    org_id,
                    str(event),
                    str(actor),
                    utc_now(),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def list_runner_security_events(self, runner_id: str, org_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runner_security_events WHERE runner_id = ? AND org_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (runner_id, org_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "runner_id": str(row["runner_id"]),
                "org_id": str(row["org_id"]),
                "event": str(row["event"]),
                "actor": str(row["actor"]),
                "created_at": str(row["created_at"]),
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in rows
        ]

    def runner_hmac_secret(self, runner_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT hmac_secret FROM runners WHERE id = ?", (runner_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown runner {runner_id}")
        return row["hmac_secret"]

    def touch_runner(self, runner_id: str, status: str = "online", version: str | None = None) -> None:
        """Update liveness without overriding an administrator-requested drain."""
        with self._connect() as conn:
            if version is None:
                conn.execute(
                    "UPDATE runners SET status = CASE WHEN drain_requested = 1 THEN 'draining' ELSE ? END, "
                    "last_seen = ? WHERE id = ? AND revoked_at IS NULL",
                    (status, utc_now(), runner_id),
                )
            else:
                conn.execute(
                    "UPDATE runners SET status = CASE WHEN drain_requested = 1 THEN 'draining' ELSE ? END, "
                    "version = ?, last_seen = ? WHERE id = ? AND revoked_at IS NULL",
                    (status, version, utc_now(), runner_id),
                )

    def heartbeat_runner(self, runner_id: str, *, version: str = "", state: str = "online") -> RunnerRecord:
        normalized = str(state or "online").strip().lower()
        if normalized not in {"online", "draining"}:
            raise ValueError("Runner heartbeat state must be online or draining")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE runners SET status = CASE WHEN drain_requested = 1 THEN 'draining' ELSE ? END, "
                "version = ?, last_seen = ?, draining_at = CASE "
                "WHEN drain_requested = 1 OR ? = 'draining' THEN COALESCE(draining_at, ?) ELSE NULL END "
                "WHERE id = ? AND revoked_at IS NULL",
                (normalized, version, now, normalized, now, runner_id),
            )
        return self.get_runner(runner_id)

    def set_runner_drain(self, runner_id: str, org_id: str, *, requested: bool) -> RunnerRecord:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runners SET drain_requested = ?, status = ?, draining_at = ?, last_seen = last_seen "
                "WHERE id = ? AND org_id = ? AND revoked_at IS NULL",
                (
                    int(requested),
                    "draining" if requested else "enrolled",
                    now if requested else None,
                    runner_id,
                    org_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown runner {runner_id}")
        return self.get_runner(runner_id)

    def list_runners(self, org_id: str | None = None) -> list[RunnerRecord]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute("SELECT * FROM runners ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM runners WHERE org_id = ? ORDER BY created_at DESC", (org_id,)).fetchall()
        return [self._runner(row) for row in rows]

    def queue_metrics(self, org_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS queued, MIN(created_at) AS oldest FROM jobs "
                "WHERE org_id = ? AND status = 'queued'",
                (org_id,),
            ).fetchone()
        queued = int(row["queued"] if row else 0)
        oldest = str(row["oldest"] or "") if row else ""
        age_seconds = 0
        if oldest:
            try:
                parsed = datetime.fromisoformat(oldest)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age_seconds = max(
                    0,
                    int((datetime.now(timezone.utc) - parsed).total_seconds()),
                )
            except (TypeError, ValueError):
                age_seconds = 0
        threshold = queue_age_alert_seconds()
        return {
            "queued": queued,
            "limit": job_queue_limit(),
            "oldest_created_at": oldest or None,
            "oldest_age_seconds": age_seconds,
            "age_alert_seconds": threshold,
            "age_alert": bool(queued and age_seconds >= threshold),
        }

    def catalog_device_count(self, org_id: str, *, runner_id: str | None = None) -> int:
        with self._connect() as conn:
            if runner_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM device_catalog WHERE org_id = ?",
                    (org_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM device_catalog WHERE org_id = ? AND runner_id = ?",
                    (org_id, runner_id),
                ).fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    def normalize_device_identifier(value: Any) -> str:
        return str(value or "").strip().lower()

    def sync_runner_devices(
        self,
        runner: RunnerRecord,
        devices: list[dict[str, Any]],
        *,
        revision: str,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Persist public runner inventory metadata without accepting credentials."""
        normalized: dict[str, dict[str, Any]] = {}
        for raw in devices:
            canonical_id = self.normalize_device_identifier(raw.get("id"))
            if not canonical_id:
                continue
            display_id = str(raw.get("id") or canonical_id).strip()
            hostname = str(raw.get("hostname") or display_id).strip()
            host = str(raw.get("host") or "").strip()
            port = int(raw.get("port") or 22)
            platform = str(raw.get("platform") or "unknown").strip().lower()
            serial = str(raw.get("serial") or "").strip()
            groups = sorted({str(item).strip() for item in (raw.get("groups") or []) if str(item).strip()})
            raw_location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
            location = {
                key: str(raw_location[key]).strip()[:256]
                for key in ("campus", "building", "floor", "closet", "room", "rack", "zone")
                if isinstance(raw_location.get(key), (str, int, float)) and str(raw_location[key]).strip()
            }
            for key in ("building", "floor", "closet"):
                value = raw.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    location[key] = str(value).strip()[:256]
            management: dict[str, Any] = {}
            if raw.get("management"):
                from netcode.firewall_managers import ManagerOwnership, assert_no_secrets

                assert_no_secrets(raw["management"], f"runner_inventory.{display_id}.management")
                ownership = ManagerOwnership.model_validate(raw["management"])
                if self.normalize_device_identifier(ownership.device_id) != canonical_id:
                    raise ValueError(f"manager ownership device_id does not match catalog device {display_id}")
                management = ownership.public_dict()
            aliases = {
                canonical_id,
                self.normalize_device_identifier(display_id),
                self.normalize_device_identifier(hostname),
                self.normalize_device_identifier(host),
                self.normalize_device_identifier(f"{host}:{port}" if host else ""),
                *(self.normalize_device_identifier(item) for item in (raw.get("aliases") or [])),
            }
            normalized[canonical_id] = {
                "canonical_id": canonical_id,
                "display_id": display_id,
                "hostname": hostname,
                "host": host,
                "port": port,
                "platform": platform,
                "serial": serial,
                "site": str(raw.get("site") or "").strip() or None,
                "role": str(raw.get("role") or "").strip() or None,
                "groups": groups,
                "location": location,
                "aliases": sorted(alias for alias in aliases if alias),
                "management": management,
            }

        now = utc_now()
        conflicts: list[dict[str, str]] = []
        with self._connect() as conn:
            if replace:
                conn.execute(
                    "DELETE FROM device_aliases WHERE org_id = ? AND canonical_id IN "
                    "(SELECT canonical_id FROM device_catalog WHERE org_id = ? AND runner_id = ?)",
                    (runner.org_id, runner.org_id, runner.id),
                )
                conn.execute(
                    "DELETE FROM device_catalog WHERE org_id = ? AND runner_id = ?",
                    (runner.org_id, runner.id),
                )
            for device in normalized.values():
                if device["serial"]:
                    serial_owner = conn.execute(
                        "SELECT canonical_id, runner_id FROM device_catalog WHERE org_id = ? AND LOWER(serial) = LOWER(?) LIMIT 1",
                        (runner.org_id, device["serial"]),
                    ).fetchone()
                    if serial_owner and str(serial_owner["canonical_id"]) != device["canonical_id"]:
                        conflicts.append({
                            "type": "serial_identity_conflict",
                            "canonical_id": str(device["canonical_id"]),
                            "existing_canonical_id": str(serial_owner["canonical_id"]),
                            "serial": str(device["serial"]),
                            "claiming_runner_id": runner.id,
                        })
                        if str(serial_owner["runner_id"]) != runner.id:
                            continue
                endpoint_owner = conn.execute(
                    "SELECT canonical_id, runner_id FROM device_catalog WHERE org_id = ? AND LOWER(host) = LOWER(?) AND port = ? LIMIT 1",
                    (runner.org_id, device["host"], device["port"]),
                ).fetchone()
                if endpoint_owner and str(endpoint_owner["canonical_id"]) != device["canonical_id"]:
                    conflicts.append({
                        "type": "endpoint_identity_conflict",
                        "canonical_id": str(device["canonical_id"]),
                        "existing_canonical_id": str(endpoint_owner["canonical_id"]),
                        "endpoint": f"{device['host']}:{device['port']}",
                        "claiming_runner_id": runner.id,
                    })
                    if str(endpoint_owner["runner_id"]) != runner.id:
                        continue
                alias_conflict = None
                for alias in device["aliases"]:
                    alias_owner = conn.execute(
                        "SELECT a.canonical_id, d.runner_id FROM device_aliases a "
                        "JOIN device_catalog d ON d.org_id = a.org_id AND d.canonical_id = a.canonical_id "
                        "WHERE a.org_id = ? AND a.alias = ? LIMIT 1",
                        (runner.org_id, alias),
                    ).fetchone()
                    if alias_owner and str(alias_owner["canonical_id"]) != device["canonical_id"]:
                        alias_conflict = (alias, str(alias_owner["canonical_id"]))
                        break
                if alias_conflict:
                    conflicts.append({
                        "type": "alias_identity_conflict",
                        "canonical_id": str(device["canonical_id"]),
                        "existing_canonical_id": alias_conflict[1],
                        "alias": alias_conflict[0],
                        "claiming_runner_id": runner.id,
                    })
                    alias_owner = conn.execute(
                        "SELECT d.runner_id FROM device_aliases a "
                        "JOIN device_catalog d ON d.org_id = a.org_id AND d.canonical_id = a.canonical_id "
                        "WHERE a.org_id = ? AND a.alias = ? LIMIT 1",
                        (runner.org_id, alias_conflict[0]),
                    ).fetchone()
                    if alias_owner and str(alias_owner["runner_id"]) != runner.id:
                        continue
                existing = conn.execute(
                    "SELECT runner_id FROM device_catalog WHERE org_id = ? AND canonical_id = ?",
                    (runner.org_id, device["canonical_id"]),
                ).fetchone()
                if existing and str(existing["runner_id"]) != runner.id:
                    conflicts.append({
                        "canonical_id": str(device["canonical_id"]),
                        "existing_runner_id": str(existing["runner_id"]),
                        "claiming_runner_id": runner.id,
                    })
                    continue
                conn.execute(
                    """
                    INSERT INTO device_catalog
                    (org_id, canonical_id, display_id, hostname, host, port, platform, serial, site, role,
                     groups_json, location_json, management_json, runner_id, runner_pool, source, last_seen, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'runner_inventory', ?, ?)
                    ON CONFLICT (org_id, canonical_id) DO UPDATE SET
                      display_id = excluded.display_id,
                      hostname = excluded.hostname,
                      host = excluded.host,
                      port = excluded.port,
                      platform = excluded.platform,
                      serial = excluded.serial,
                      site = excluded.site,
                      role = excluded.role,
                      groups_json = excluded.groups_json,
                      location_json = excluded.location_json,
                      management_json = excluded.management_json,
                      runner_id = excluded.runner_id,
                      runner_pool = excluded.runner_pool,
                      source = excluded.source,
                      last_seen = excluded.last_seen,
                      updated_at = excluded.updated_at
                    """,
                    (
                        runner.org_id,
                        device["canonical_id"],
                        device["display_id"],
                        device["hostname"],
                        device["host"],
                        device["port"],
                        device["platform"],
                        device["serial"],
                        device["site"],
                        device["role"],
                        json.dumps(device["groups"]),
                        json.dumps(device["location"]),
                        json.dumps(device["management"]),
                        runner.id,
                        runner.pool,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "DELETE FROM device_aliases WHERE org_id = ? AND canonical_id = ?",
                    (runner.org_id, device["canonical_id"]),
                )
                for alias in device["aliases"]:
                    conn.execute(
                        """
                        INSERT INTO device_aliases (org_id, alias, canonical_id) VALUES (?, ?, ?)
                        ON CONFLICT (org_id, alias) DO NOTHING
                        """,
                        (runner.org_id, alias, device["canonical_id"]),
                    )
            if replace:
                conn.execute(
                    "UPDATE runners SET inventory_revision = ?, device_count = ?, last_seen = ?, "
                    "status = CASE WHEN drain_requested = 1 THEN 'draining' ELSE 'online' END "
                    "WHERE id = ? AND revoked_at IS NULL",
                    (revision, len(normalized), now, runner.id),
                )
            else:
                conn.execute(
                    "UPDATE runners SET last_seen = ?, "
                    "status = CASE WHEN drain_requested = 1 THEN 'draining' ELSE 'online' END "
                    "WHERE id = ? AND revoked_at IS NULL",
                    (now, runner.id),
                )
            count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM device_catalog WHERE org_id = ? AND runner_id = ?",
                (runner.org_id, runner.id),
            ).fetchone()
        return {
            "revision": revision,
            "device_count": int(count_row["count"] if count_row else 0),
            "conflicts": conflicts,
        }

    @staticmethod
    def _catalog_row(row: Any) -> dict[str, Any]:
        raw_groups = row["groups_json"] or "[]"
        raw_location = row["location_json"] or "{}"
        raw_management = row["management_json"] or "{}"
        location = json.loads(raw_location)
        return {
            "canonical_id": row["canonical_id"],
            "id": row["display_id"],
            "hostname": row["hostname"],
            "host": row["host"],
            "port": int(row["port"] or 22),
            "platform": row["platform"],
            "serial": row["serial"],
            "site": row["site"],
            "role": row["role"],
            "groups": json.loads(raw_groups),
            "building": location.get("building"),
            "floor": location.get("floor"),
            "closet": location.get("closet"),
            "location": location,
            "management": json.loads(raw_management),
            "runner_id": row["runner_id"],
            "runner_pool": row["runner_pool"],
            "runner_status": row["runner_status"] or "offline",
            "runner_last_seen": row["runner_last_seen"],
            "source": row["source"],
            "updated_at": row["updated_at"],
        }

    def resolve_device(self, org_id: str, identifier: str) -> dict[str, Any] | None:
        alias = self.normalize_device_identifier(identifier)
        if not alias:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT d.*, r.status AS runner_status, r.last_seen AS runner_last_seen
                FROM device_aliases a
                JOIN device_catalog d ON d.org_id = a.org_id AND d.canonical_id = a.canonical_id
                LEFT JOIN runners r ON r.id = d.runner_id
                WHERE a.org_id = ? AND a.alias = ?
                """,
                (org_id, alias),
            ).fetchone()
        return self._catalog_row(row) if row else None

    def devices_by_identifiers(self, org_id: str, identifiers: list[str]) -> list[dict[str, Any]]:
        aliases = list(dict.fromkeys(
            self.normalize_device_identifier(value) for value in identifiers if self.normalize_device_identifier(value)
        ))[:50]
        if not aliases:
            return []
        placeholders = ", ".join("?" for _ in aliases)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.alias AS matched_alias, d.*, r.status AS runner_status, r.last_seen AS runner_last_seen
                FROM device_aliases a
                JOIN device_catalog d ON d.org_id = a.org_id AND d.canonical_id = a.canonical_id
                LEFT JOIN runners r ON r.id = d.runner_id
                WHERE a.org_id = ? AND a.alias IN ({placeholders})
                """,
                (org_id, *aliases),
            ).fetchall()
        by_alias = {str(row["matched_alias"]): self._catalog_row(row) for row in rows}
        devices: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alias in aliases:
            device = by_alias.get(alias)
            if not device or device["canonical_id"] in seen:
                continue
            seen.add(device["canonical_id"])
            devices.append(device)
        return devices

    def query_devices(
        self,
        org_id: str,
        *,
        query: str = "",
        site: str = "",
        role: str = "",
        platform: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        clauses = ["d.org_id = ?"]
        params: list[Any] = [org_id]
        if query.strip():
            term = f"%{query.strip().lower()}%"
            clauses.append(
                "(LOWER(d.display_id) LIKE ? OR LOWER(d.hostname) LIKE ? OR LOWER(d.host) LIKE ? "
                "OR EXISTS (SELECT 1 FROM device_aliases a WHERE a.org_id = d.org_id "
                "AND a.canonical_id = d.canonical_id AND a.alias LIKE ?))"
            )
            params.extend([term, term, term, term])
        for column, value in (("site", site), ("role", role), ("platform", platform)):
            if value.strip():
                clauses.append(f"LOWER(COALESCE(d.{column}, '')) = ?")
                params.append(value.strip().lower())
        count_where = " AND ".join(clauses)
        count_params = tuple(params)
        if cursor.strip():
            clauses.append("d.canonical_id > ?")
            params.append(self.normalize_device_identifier(cursor))
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.*, r.status AS runner_status, r.last_seen AS runner_last_seen
                FROM device_catalog d
                LEFT JOIN runners r ON r.id = d.runner_id
                WHERE {where}
                ORDER BY d.canonical_id ASC
                LIMIT ?
                """,
                (*params, limit + 1),
            ).fetchall()
            count_row = conn.execute(
                f"SELECT COUNT(*) AS count FROM device_catalog d WHERE {count_where}",
                count_params,
            ).fetchone()
            facets: dict[str, list[str]] = {}
            for column in ("site", "role", "platform"):
                values = conn.execute(
                    f"SELECT DISTINCT {column} AS value FROM device_catalog "
                    "WHERE org_id = ? AND COALESCE(" + column + ", '') <> '' ORDER BY value ASC LIMIT 200",
                    (org_id,),
                ).fetchall()
                facets[column + "s"] = [str(row["value"]) for row in values]
        has_more = len(rows) > limit
        page = rows[:limit]
        devices = [self._catalog_row(row) for row in page]
        return {
            "devices": devices,
            "returned": len(devices),
            "total": int(count_row["count"] if count_row else 0),
            "next_cursor": devices[-1]["canonical_id"] if has_more and devices else None,
            "facets": facets,
        }

    @staticmethod
    def _shell_session_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "org_id": str(row["org_id"]),
            "device_id": str(row["device_id"]),
            "display_id": str(row["display_id"]),
            "platform": str(row["platform"]),
            "runner_id": str(row["runner_id"] or ""),
            "runner_pool": str(row["runner_pool"] or ""),
            "status": str(row["status"]),
            "guard_enabled": bool(row["guard_enabled"]),
            "change_id": str(row["change_id"] or ""),
            "started_at": str(row["started_at"]),
            "last_activity": str(row["last_activity"]),
            "ended_at": str(row["ended_at"] or ""),
            "transcript_path": str(row["transcript_path"]),
            "command_count": int(row["command_count"] or 0),
            "output_bytes": int(row["output_bytes"] or 0),
            "device_touched": bool(row["device_touched"]),
            "end_reason": str(PlatformStore._col(row, "end_reason") or ""),
        }

    def create_shell_session(
        self,
        *,
        session_id: str,
        org_id: str,
        device_id: str,
        display_id: str,
        platform: str,
        transcript_path: str,
        runner_id: str = "",
        runner_pool: str = "",
        status: str = "opened",
        guard_enabled: bool = False,
        change_id: str = "",
        started_at: str | None = None,
        last_activity: str | None = None,
        ended_at: str = "",
        command_count: int = 0,
        output_bytes: int = 0,
        device_touched: bool = False,
    ) -> dict[str, Any]:
        started = started_at or utc_now()
        activity = last_activity or started
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO shell_sessions (
                    id, org_id, device_id, display_id, platform, runner_id, runner_pool,
                    status, guard_enabled, change_id, started_at, last_activity, ended_at,
                    transcript_path, command_count, output_bytes, device_touched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    session_id, org_id, device_id, display_id or device_id, platform,
                    runner_id or None, runner_pool or None, status, int(guard_enabled),
                    change_id or None, started, activity, ended_at or None, transcript_path,
                    max(0, int(command_count)), max(0, int(output_bytes)), int(device_touched),
                ),
            )
        session = self.get_shell_session(session_id)
        if session is None:
            raise RuntimeError(f"Failed to persist shell session {session_id}")
        return session

    def update_shell_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        change_id: str | None = None,
        command_delta: int = 0,
        output_bytes_delta: int = 0,
        device_touched: bool | None = None,
        ended: bool = False,
        end_reason: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        touched_value = None if device_touched is None else int(device_touched)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE shell_sessions
                SET status = COALESCE(?, status),
                    change_id = COALESCE(?, change_id),
                    command_count = command_count + ?,
                    output_bytes = output_bytes + ?,
                    device_touched = CASE WHEN ? IS NULL THEN device_touched ELSE ? END,
                    last_activity = ?,
                    ended_at = CASE WHEN ? = 1 THEN ? ELSE ended_at END,
                    end_reason = COALESCE(?, end_reason)
                WHERE id = ?
                """,
                (
                    status, change_id, max(0, int(command_delta)),
                    max(0, int(output_bytes_delta)), touched_value, touched_value,
                    now, int(ended), now, end_reason, session_id,
                ),
            )
        return self.get_shell_session(session_id)

    def terminate_active_shell_sessions(
        self,
        *,
        reason: str,
        runner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["status IN ('opened', 'active')"]
        params: list[Any] = []
        if runner_id:
            clauses.append("runner_id = ?")
            params.append(runner_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shell_sessions WHERE " + " AND ".join(clauses),
                tuple(params),
            ).fetchall()
            if rows:
                now = utc_now()
                conn.execute(
                    "UPDATE shell_sessions SET status = 'terminated', ended_at = ?, last_activity = ?, "
                    "end_reason = ? WHERE " + " AND ".join(clauses),
                    (now, now, reason, *params),
                )
        terminated: list[dict[str, Any]] = []
        for row in rows:
            session = self.get_shell_session(str(row["id"]))
            if session is not None:
                terminated.append(session)
        return terminated

    def get_shell_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shell_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._shell_session_row(row) if row else None

    def list_shell_sessions(
        self,
        org_id: str,
        *,
        limit: int = 50,
        device_id: str = "",
        before: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["org_id = ?"]
        params: list[Any] = [org_id]
        if device_id.strip():
            clauses.append("LOWER(device_id) = ?")
            params.append(device_id.strip().lower())
        if before.strip():
            cursor_time, separator, cursor_id = before.strip().rpartition("|")
            if separator and cursor_time and cursor_id:
                clauses.append("(last_activity < ? OR (last_activity = ? AND id < ?))")
                params.extend([cursor_time, cursor_time, cursor_id])
            else:
                clauses.append("last_activity < ?")
                params.append(before.strip())
        limit = max(1, min(int(limit), 101))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shell_sessions WHERE " + " AND ".join(clauses)
                + " ORDER BY last_activity DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._shell_session_row(row) for row in rows]

    def queue_job(
        self,
        change_id: str,
        action: str,
        pool: str,
        payload: dict[str, Any],
        *,
        target_runner_id: str | None = None,
        device_id: str | None = None,
        idempotency_key: str | None = None,
        retry_terminal: bool = False,
    ) -> JobRecord:
        """Queue one change operation with a durable idempotency boundary.

        The key is durable and unique within an organization. Concurrent API
        requests for the same reviewed operation therefore receive the same job
        instead of creating a second device write. Callers may opt safe,
        read-only actions into a new audited attempt after the prior attempt is
        terminal; write actions must retain the default exactly-once behavior.
        """
        now = utc_now()
        job_id = str(uuid.uuid4())
        canonical_device = str(device_id or job_device_id(payload)).strip().lower()
        with self._connect() as conn:
            if self.engine == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            org_row = conn.execute("SELECT org_id FROM changes WHERE id = ?", (change_id,)).fetchone()
            if not org_row:
                raise ValueError(f"Unknown change {change_id}")
            org_id = str(org_row["org_id"] or DEFAULT_ORG_ID)
            caller_key = str(idempotency_key or "").strip()
            base_operation_key = caller_key or job_idempotency_key(
                org_id=org_id,
                change_id=change_id,
                action=action,
                device_id=canonical_device,
                payload=payload,
            )
            if self.engine == "postgres":
                conn.execute("SELECT id FROM orgs WHERE id = ? FOR UPDATE", (org_id,)).fetchone()
            operation_key = base_operation_key
            if retry_terminal and not caller_key:
                attempts = conn.execute(
                    "SELECT id, status, idempotency_key FROM jobs "
                    "WHERE org_id = ? AND (idempotency_key = ? OR idempotency_key LIKE ?) "
                    "ORDER BY created_at DESC, id DESC",
                    (org_id, base_operation_key, f"{base_operation_key}:retry:%"),
                ).fetchall()
                if attempts:
                    latest = attempts[0]
                    if str(latest["status"]) in TERMINAL_JOB_STATUSES:
                        operation_key = f"{base_operation_key}:retry:{len(attempts)}"
                    else:
                        operation_key = str(latest["idempotency_key"])
            existing = conn.execute(
                "SELECT id FROM jobs WHERE org_id = ? AND idempotency_key = ?",
                (org_id, operation_key),
            ).fetchone()
            if not existing:
                self._enforce_queue_capacity(conn, org_id)
                conn.execute(
                    "INSERT INTO jobs (id, change_id, action, status, message, created_at, updated_at, "
                    "result_json, org_id, pool, payload_json, target_runner_id, device_id, idempotency_key) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (
                        job_id,
                        change_id,
                        action,
                        f"Queued for runner pool {pool}",
                        now,
                        now,
                        org_id,
                        pool,
                        json.dumps(payload),
                        target_runner_id,
                        canonical_device,
                        operation_key,
                    ),
                )
            row = conn.execute(
                "SELECT id, change_id, action, device_id, payload_json FROM jobs "
                "WHERE org_id = ? AND idempotency_key = ?",
                (org_id, operation_key),
            ).fetchone()
            if not row:
                raise RuntimeError("Failed to queue or resolve the idempotent operation")
            existing_payload = json.loads(row["payload_json"] or "{}")
            if (
                str(row["change_id"]) != change_id
                or str(row["action"]) != action
                or str(row["device_id"] or "") != canonical_device
                or json.dumps(existing_payload, sort_keys=True, separators=(",", ":"), default=str)
                != json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            ):
                raise ValueError("Idempotency key is already bound to a different device operation")
            actual_job_id = str(row["id"])
            conn.execute(
                "UPDATE changes SET last_job_id = ?, updated_at = ? WHERE id = ?",
                (actual_job_id, now, change_id),
            )
        return self.get_job(actual_job_id)

    def _enforce_queue_capacity(self, conn: _EngineConn, org_id: str) -> None:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE org_id = ? AND status = 'queued'",
            (org_id,),
        ).fetchone()
        queued = int(row["count"] if row else 0)
        limit = job_queue_limit()
        if queued >= limit:
            raise JobQueueFullError(
                f"Organization job queue is full ({queued}/{limit}); wait for work to drain before submitting more."
            )

    # ── Fleet rollouts ────────────────────────────────────────────────────

    def create_rollout(
        self,
        *,
        description: str,
        change_type: str,
        values: dict[str, Any],
        canary_size: int,
        batch_size: int,
        requested_by: str = "netcode-user",
        org_id: str = DEFAULT_ORG_ID,
        created_by_user_id: str | None = None,
        parent_rollout_id: str | None = None,
        retry_scope: str | None = None,
    ) -> dict[str, Any]:
        rollout_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rollouts (id, org_id, description, change_type, values_json, status,"
                " canary_size, batch_size, requested_by, created_by_user_id, parent_rollout_id, retry_scope,"
                " halt_reason, current_wave, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rollout_id, org_id, description, change_type, json.dumps(values), "planned",
                 canary_size, batch_size, requested_by, created_by_user_id, parent_rollout_id,
                 retry_scope, None, 0, now, now),
            )
        return self.get_rollout(rollout_id)

    def get_rollout(self, rollout_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rollouts WHERE id = ?", (rollout_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown rollout {rollout_id}")
        return self._rollout(row)

    def list_rollouts(self, org_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute("SELECT * FROM rollouts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rollouts WHERE org_id = ? ORDER BY created_at DESC LIMIT ?", (org_id, limit)
                ).fetchall()
        return [self._rollout(row) for row in rows]

    def update_rollout(
        self,
        rollout_id: str,
        *,
        status: str | None = None,
        halt_reason: str | None = None,
        current_wave: int | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """Update a rollout. With expected_status the write is conditional
        (compare-and-set), so a halt request can never clobber a terminal state
        and a 'completed' write can never overwrite a pending halt."""
        sets, params = ["updated_at = ?"], [utc_now()]
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if halt_reason is not None:
            sets.append("halt_reason = ?"); params.append(halt_reason)
        if current_wave is not None:
            sets.append("current_wave = ?"); params.append(current_wave)
        params.append(rollout_id)
        where = "id = ?"
        # (approved_by/approved_at are set only via approve_rollout)
        if expected_status is not None:
            where += " AND status = ?"
            params.append(expected_status)
        with self._connect() as conn:
            conn.execute(f"UPDATE rollouts SET {', '.join(sets)} WHERE {where}", tuple(params))
        return self.get_rollout(rollout_id)

    def approve_rollout(self, rollout_id: str, approved_by: str) -> dict[str, Any]:
        """Compare-and-set approval: only a planned, not-yet-approved rollout."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE rollouts SET approved_by = ?, approved_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'planned' AND approved_by IS NULL",
                (approved_by, now, now, rollout_id),
            )
        return self.get_rollout(rollout_id)

    def cancel_queued_jobs_for_change(self, change_id: str, reason: str) -> int:
        """Fail-close any still-queued jobs for a change so an offline runner can
        never claim and execute them later (zombie apply). Returns cancelled count."""
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'failed', message = ?, updated_at = ? "
                "WHERE change_id = ? AND status = 'queued'",
                (f"Cancelled: {reason}", now, change_id),
            )
            count = cursor.rowcount if cursor.rowcount is not None else 0
        return count

    def cancel_job_if_queued(self, job_id: str, reason: str) -> bool:
        """Atomically cancel ONE job if (and only if) it is still queued."""
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'failed', message = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (f"Cancelled: {reason}", now, job_id),
            )
            return bool(cursor.rowcount)

    def cancel_job_for_org(self, job_id: str, org_id: str, *, actor: str, reason: str) -> JobRecord:
        """Cancel only work that has not crossed the connector claim boundary."""
        now = utc_now()
        message = f"Cancelled by {actor}: {str(reason or 'operator request').strip()[:300]}"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND org_id = ?",
                (job_id, org_id),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown job {job_id}")
            if str(row["status"]) != "queued":
                raise RuntimeError(
                    f"Job {job_id} is {row['status']}; claimed or terminal work cannot be cancelled blindly."
                )
            cursor = conn.execute(
                "UPDATE jobs SET status = 'cancelled', message = ?, updated_at = ? "
                "WHERE id = ? AND org_id = ? AND status = 'queued'",
                (message, now, job_id, org_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Job {job_id} crossed the claim boundary before cancellation completed.")
        job = self.get_job(job_id)
        if job.change_id != "__read__":
            try:
                change = self.get_change(job.change_id)
            except ValueError:
                return job
            self.record_workflow_event(
                change.id,
                "cancel_queued_job",
                change.workflow_state,
                change.workflow_state,
                message,
                {"job_id": job.id, "actor": actor, "reason": reason, "device_id": job.device_id},
            )
        return job

    def list_rollouts_in_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM rollouts WHERE status IN ({marks})", statuses).fetchall()
        return [self._rollout(row) for row in rows]

    def add_rollout_target(
        self, rollout_id: str, device_id: str, wave_index: int,
        change_id: str | None = None, intent_path: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rollout_targets (rollout_id, device_id, wave_index, change_id, intent_path,"
                " status, stage, message, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rollout_id, device_id, wave_index, change_id, intent_path, "pending", "", "", utc_now()),
            )

    def list_rollout_targets(self, rollout_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rollout_targets WHERE rollout_id = ? ORDER BY wave_index ASC, device_id ASC",
                (rollout_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rollout_target_counts(self, rollout_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT LOWER(status) AS status, COUNT(*) AS count "
                "FROM rollout_targets WHERE rollout_id = ? GROUP BY LOWER(status)",
                (rollout_id,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def rollout_wave_counts(self, rollout_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT wave_index, LOWER(status) AS status, COUNT(*) AS count "
                "FROM rollout_targets WHERE rollout_id = ? "
                "GROUP BY wave_index, LOWER(status) ORDER BY wave_index ASC",
                (rollout_id,),
            ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            index = int(row["wave_index"])
            wave = grouped.setdefault(index, {
                "index": index,
                "label": "Canary" if index == 0 else f"Batch {index}",
                "target_counts": {},
                "total": 0,
            })
            count = int(row["count"])
            wave["target_counts"][str(row["status"])] = count
            wave["total"] += count
        return [grouped[index] for index in sorted(grouped)]

    def list_rollout_targets_page(
        self,
        rollout_id: str,
        *,
        query: str = "",
        category: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        categories = {
            "all": (),
            "running": ("running", "in_progress", "in-progress"),
            "passed": ("passed", "completed", "verified", "success"),
            "failed": ("failed", "blocked", "error"),
            "untouched": ("pending", "planned", "queued", "skipped", "cancelled"),
        }
        normalized_category = str(category or "all").strip().lower()
        if normalized_category not in categories:
            raise ValueError(f"Unknown rollout target category {category!r}")
        clauses = ["rollout_id = ?"]
        params: list[Any] = [rollout_id]
        search = str(query or "").strip().lower()
        if search:
            clauses.append("(LOWER(device_id) LIKE ? OR LOWER(message) LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        statuses = categories[normalized_category]
        if statuses:
            marks = ", ".join("?" for _ in statuses)
            clauses.append(f"LOWER(status) IN ({marks})")
            params.extend(statuses)
        where = " AND ".join(clauses)
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        with self._connect() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) AS count FROM rollout_targets WHERE {where}",
                params,
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM rollout_targets WHERE {where} "
                "ORDER BY wave_index ASC, device_id ASC LIMIT ? OFFSET ?",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
        return [dict(row) for row in rows], int(count_row["count"] if count_row else 0)

    def update_rollout_target(
        self, rollout_id: str, device_id: str, *,
        status: str | None = None, stage: str | None = None,
        message: str | None = None, change_id: str | None = None,
    ) -> None:
        sets, params = ["updated_at = ?"], [utc_now()]
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if stage is not None:
            sets.append("stage = ?"); params.append(stage)
        if message is not None:
            sets.append("message = ?"); params.append(message)
        if change_id is not None:
            sets.append("change_id = ?"); params.append(change_id)
        params.extend([rollout_id, device_id])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE rollout_targets SET {', '.join(sets)} WHERE rollout_id = ? AND device_id = ?",
                tuple(params),
            )

    def _rollout(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["values"] = json.loads(data.pop("values_json") or "{}")
        except Exception:  # noqa: BLE001
            data["values"] = {}
        return data

    def create_read_job(
        self,
        org_id: str,
        pool: str,
        action: str,
        payload: dict[str, Any],
        *,
        target_runner_id: str | None = None,
        change_id: str = "__read__",
        idempotency_key: str | None = None,
    ) -> JobRecord:
        """Queue a device-READ job for a runner. Not tied to a change (uses the '__read__'
        sentinel), so submitting its result never advances a change workflow."""
        job_id = str(uuid.uuid4())
        now = utc_now()
        canonical_device = job_device_id(payload)
        operation_key = str(idempotency_key or "").strip() or None
        with self._connect() as conn:
            if self.engine == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("SELECT id FROM orgs WHERE id = ? FOR UPDATE", (org_id,)).fetchone()
            existing = None
            if operation_key:
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE org_id = ? AND idempotency_key = ?",
                    (org_id, operation_key),
                ).fetchone()
            if not existing:
                self._enforce_queue_capacity(conn, org_id)
                conn.execute(
                    "INSERT INTO jobs (id, change_id, action, status, message, created_at, updated_at, result_json, "
                    "org_id, pool, payload_json, target_runner_id, device_id, idempotency_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (
                        job_id,
                        change_id,
                        f"read_{action}",
                        "queued",
                        f"Queued read '{action}' for pool {pool}",
                        now,
                        now,
                        None,
                        org_id,
                        pool,
                        json.dumps(payload),
                        target_runner_id,
                        canonical_device,
                        operation_key,
                    ),
                )
            if operation_key:
                row = conn.execute(
                    "SELECT id, change_id, action, device_id, payload_json FROM jobs "
                    "WHERE org_id = ? AND idempotency_key = ?",
                    (org_id, operation_key),
                ).fetchone()
                if not row:
                    raise RuntimeError("Failed to queue or resolve the idempotent read operation")
                existing_payload = json.loads(row["payload_json"] or "{}")
                if (
                    str(row["change_id"]) != change_id
                    or str(row["action"]) != f"read_{action}"
                    or str(row["device_id"] or "") != canonical_device
                    or json.dumps(existing_payload, sort_keys=True, separators=(",", ":"), default=str)
                    != json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
                ):
                    raise ValueError("Idempotency key is already bound to a different read operation")
                job_id = str(row["id"])
        return self.get_job(job_id)

    def queue_reconciliation_read(self, job: JobRecord) -> JobRecord | None:
        """Queue one read-only live-state check for an uncertain direct device write."""
        if job.action not in {"lab_apply", "lab_rollback"}:
            return None
        payload = job.payload or {}
        intent_yaml = str(payload.get("intent_yaml") or "")
        device_id = str(job.device_id or job_device_id(payload)).strip().lower()
        if not intent_yaml or not device_id or not job.pool:
            return None
        verification_payload = {
            "device_id": device_id,
            "intent_yaml": intent_yaml,
            "present": job.action == "lab_apply",
            "reconciliation_for_job_id": job.id,
            "uncertain_action": job.action,
        }
        return self.create_read_job(
            job.org_id,
            str(job.pool),
            "verify",
            verification_payload,
            target_runner_id=job.target_runner_id or job.claimed_by,
            change_id=job.change_id,
            idempotency_key=f"reconcile_{job.id}",
        )

    def recover_expired_jobs(self, *, org_id: str | None = None, pool: str | None = None) -> dict[str, int]:
        """Recover orphaned connector work without ever replaying an uncertain write."""
        now = utc_now()
        clauses = ["status IN ('running', 'completing')", "(lease_expires_at IS NULL OR lease_expires_at <= ?)"]
        params: list[Any] = [now]
        if org_id:
            clauses.append("org_id = ?")
            params.append(org_id)
        if pool:
            clauses.append("pool = ?")
            params.append(pool)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC",
                tuple(params),
            ).fetchall()

        recovered = {"requeued": 0, "failed": 0, "reconcile_required": 0}
        for row in rows:
            job = self._job(row)
            retry_safe = job_is_retry_safe(job.action)
            payload_has_scrubbed_secret = _contains_redacted_secret(job.payload or {})
            if retry_safe and job.attempt_count < job_max_attempts() and not payload_has_scrubbed_secret:
                status = "queued"
                message = "Connector lease expired; safe read-only work requeued."
                bucket = "requeued"
            elif retry_safe:
                status = "failed"
                message = (
                    "Connector lease expired after its retry limit."
                    if not payload_has_scrubbed_secret
                    else "Connector lease expired after one-time discovery credentials were scrubbed; start a new scan."
                )
                bucket = "failed"
            else:
                status = "reconcile_required"
                message = (
                    "Connector lease expired after a potentially mutating action. "
                    "Reconcile live device state before any retry."
                )
                bucket = "reconcile_required"
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE jobs SET status = ?, message = ?, updated_at = ?, claimed_by = NULL, "
                    "lease_token_hash = NULL, lease_expires_at = NULL, lease_heartbeat_at = NULL "
                    "WHERE id = ? AND status IN ('running', 'completing') "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                    (status, message, now, job.id, now),
                )
            if cursor.rowcount == 1:
                recovered[bucket] += 1
                if bucket == "reconcile_required" and job.change_id != "__read__":
                    try:
                        change = self.get_change(job.change_id)
                    except ValueError:
                        continue
                    evidence = {
                        "job_id": job.id,
                        "runner_id": job.claimed_by,
                        "action": job.action,
                        "attempt_count": job.attempt_count,
                        "reason": "connector_lease_expired",
                    }
                    reconciliation_job = self.queue_reconciliation_read(job)
                    if reconciliation_job is not None:
                        evidence["verification_job_id"] = reconciliation_job.id
                        evidence["verification_status"] = reconciliation_job.status
                    else:
                        evidence["verification_status"] = "not_available_for_action"
                    phase = execution_phase_for_job(job.action)
                    if phase:
                        self.record_execution_event(
                            event_id=str(uuid.uuid4()),
                            job_id=job.id,
                            change_id=job.change_id,
                            org_id=job.org_id,
                            device_id=change.device_id or "",
                            phase=phase,
                            stage="reconcile_required",
                            status="failed",
                            message=message,
                            sequence=self.next_execution_sequence(job.id),
                        )
                    combined_result = dict(change.result or {})
                    combined_result["connector_reconciliation"] = evidence
                    self.update_change(change.id, "blocked", combined_result, workflow_state="blocked")
                    self.record_workflow_event(
                        change.id,
                        "connector_lease_expired",
                        change.workflow_state,
                        "blocked",
                        message,
                        evidence,
                    )
        return recovered

    def claim_next_job(self, org_id: str, pool: str, runner_id: str) -> JobRecord | None:
        """Atomically claim the oldest queued job for a (org, pool). Concurrent- and tenant-safe:
        a runner may only claim jobs in its OWN org, and catalog-targeted jobs may
        only be claimed by the runner that advertised the target device."""
        self.recover_expired_jobs(org_id=org_id, pool=pool)
        lease_token = f"jlt_{secrets.token_urlsafe(32)}"
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_expires_at = (now + timedelta(seconds=job_lease_seconds())).isoformat()
        with self._connect() as conn:
            if self.engine == "postgres":
                conn.execute("SELECT id FROM runners WHERE id = ? FOR UPDATE", (runner_id,)).fetchone()
            else:
                conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT id FROM jobs WHERE claimed_by = ? AND status IN ('running', 'completing') "
                "AND lease_expires_at > ? LIMIT 1",
                (runner_id, now_text),
            ).fetchone()
            if active:
                return None
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND org_id = ? AND pool = ? "
                "AND (target_runner_id IS NULL OR target_runner_id = ?) "
                "AND (COALESCE(device_id, '') = '' OR NOT EXISTS ("
                "SELECT 1 FROM jobs device_active WHERE device_active.org_id = jobs.org_id "
                "AND LOWER(device_active.device_id) = LOWER(jobs.device_id) "
                "AND device_active.status IN ('running', 'completing') "
                "AND device_active.lease_expires_at > ?)) "
                "ORDER BY created_at ASC LIMIT 1",
                (org_id, pool, runner_id, now_text),
            ).fetchone()
            if not row:
                return None
            conn.execute("SAVEPOINT device_claim")
            try:
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'running', claimed_by = ?, message = ?, updated_at = ?, "
                    "lease_token_hash = ?, lease_expires_at = ?, lease_heartbeat_at = ?, "
                    "attempt_count = attempt_count + 1 "
                    "WHERE id = ? AND status = 'queued' "
                    "AND (target_runner_id IS NULL OR target_runner_id = ?) "
                    "AND NOT EXISTS (SELECT 1 FROM jobs active WHERE active.claimed_by = ? "
                    "AND active.status IN ('running', 'completing') AND active.lease_expires_at > ?) "
                    "AND (COALESCE(device_id, '') = '' OR NOT EXISTS ("
                    "SELECT 1 FROM jobs device_active WHERE device_active.org_id = jobs.org_id "
                    "AND LOWER(device_active.device_id) = LOWER(jobs.device_id) "
                    "AND device_active.id <> jobs.id "
                    "AND device_active.status IN ('running', 'completing') "
                    "AND device_active.lease_expires_at > ?))",
                    (
                        runner_id,
                        f"Claimed by runner {runner_id}",
                        now_text,
                        _token_hash(lease_token),
                        lease_expires_at,
                        now_text,
                        row["id"],
                        runner_id,
                        runner_id,
                        now_text,
                        now_text,
                    ),
                )
            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT device_claim")
                conn.execute("RELEASE SAVEPOINT device_claim")
                if _is_active_device_conflict(exc):
                    return None
                raise
            else:
                conn.execute("RELEASE SAVEPOINT device_claim")
            if cursor.rowcount != 1:
                return None  # another runner won the race
        # Fetch the job WITH its real payload to hand back to the runner (it
        # needs any discovery credentials to reach the not-yet-trusted device),
        # then scrub the STORED copy immediately. A runner that dies mid-read
        # therefore never leaves the credential at rest in the control plane —
        # the previous scrub-on-successful-result missed exactly that case.
        claimed = self.get_job(row["id"])
        claimed = JobRecord(**{**claimed.__dict__, "lease_token": lease_token})
        phase = execution_phase_for_job(claimed.action)
        if claimed.change_id != "__read__" and phase:
            payload = claimed.payload or {}
            device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
            device_id = str(payload.get("device_id") or device.get("id") or "")
            self.record_execution_event(
                event_id=str(uuid.uuid4()),
                job_id=claimed.id,
                change_id=claimed.change_id,
                org_id=claimed.org_id,
                device_id=device_id,
                phase=phase,
                stage="claimed",
                status="running",
                message=f"Runner {runner_id} claimed the job.",
                sequence=1,
            )
        self.scrub_job_payload_secrets(claimed.id)
        return claimed

    def renew_job_lease(self, job_id: str, runner_id: str, lease_token: str) -> str | None:
        """Renew only the current claim; stale connector processes are rejected."""
        token = str(lease_token or "").strip()
        if not token:
            return None
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=job_lease_seconds())).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET lease_expires_at = ?, lease_heartbeat_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND claimed_by = ? "
                "AND lease_token_hash = ? AND lease_expires_at > ?",
                (expires, now_text, now_text, job_id, runner_id, _token_hash(token), now_text),
            )
        return expires if cursor.rowcount == 1 else None

    def job_lease_matches(self, job_id: str, runner_id: str, lease_token: str) -> bool:
        token = str(lease_token or "").strip()
        if not token:
            return False
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lease_token_hash FROM jobs WHERE id = ? AND status = 'running' "
                "AND claimed_by = ? AND lease_expires_at > ?",
                (job_id, runner_id, now),
            ).fetchone()
        return bool(row and hmac.compare_digest(str(row["lease_token_hash"] or ""), _token_hash(token)))

    def begin_job_completion(self, job_id: str, runner_id: str, lease_token: str) -> bool:
        """Atomically reserve result processing so one job advances workflow once."""
        token = str(lease_token or "").strip()
        if not token:
            return False
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=job_lease_seconds())).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'completing', lease_expires_at = ?, "
                "lease_heartbeat_at = ?, updated_at = ? WHERE id = ? AND status = 'running' "
                "AND claimed_by = ? AND lease_token_hash = ? AND lease_expires_at > ?",
                (expires, now_text, now_text, job_id, runner_id, _token_hash(token), now_text),
            )
        return cursor.rowcount == 1

    def record_job_signature(self, job_id: str, signature: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET signature = ? WHERE id = ?", (signature, job_id))

    def scrub_job_payload_secrets(self, job_id: str) -> None:
        """Purge credentials from a job's stored payload once the runner has used
        them. Discovery must ship creds to the runner (the device isn't trusted
        yet), but they must not sit at rest in the control-plane DB afterward."""
        with self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or not self._col(row, "payload_json"):
                return
            try:
                payload = json.loads(self._col(row, "payload_json"))
            except Exception:  # noqa: BLE001
                return
            conn.execute("UPDATE jobs SET payload_json = ? WHERE id = ?",
                         (json.dumps(redact_secrets(payload)), job_id))

    # ── Orgs / users / sessions (M5 auth + multi-tenancy) ─────────────────

    def ensure_org(self, org_id: str, name: str, slug: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO orgs (id, name, slug, created_at) SELECT ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM orgs WHERE id = ?)",
                (org_id, name, slug, utc_now(), org_id),
            )

    def get_user_by_email(self, org_id: str, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE org_id = ? AND email = ? AND status = 'active'",
                (org_id, email.strip().lower()),
            ).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return UserRecord(id=row["id"], org_id=row["org_id"], email=row["email"], role=row["role"], status=row["status"], created_at=row["created_at"])

    def create_user(self, org_id: str, email: str, password_hash: str, role: str = "viewer") -> UserRecord:
        user_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, org_id, email, password_hash, role, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (user_id, org_id, email.strip().lower(), password_hash, role, utc_now()),
            )
        return self.get_user(user_id)  # type: ignore[return-value]

    def user_exists(self, org_id: str, email: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE org_id = ? AND email = ?", (org_id, email.strip().lower())).fetchone()
        return row is not None

    def active_user_count(self, org_id: str | None = None) -> int:
        with self._connect() as conn:
            if org_id is None:
                row = conn.execute("SELECT COUNT(*) AS total FROM users WHERE status = 'active'").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS total FROM users WHERE org_id = ? AND status = 'active'",
                    (org_id,),
                ).fetchone()
        return int(row["total"]) if row else 0

    def create_session(self, token_hash: str, user_id: str, org_id: str, expires_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, org_id, created_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (token_hash, user_id, org_id, utc_now(), expires_at),
            )

    def session_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        return dict(row) if row else None

    def revoke_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET revoked_at = ? WHERE token_hash = ?", (utc_now(), token_hash))

    def _workflow_event(self, row: sqlite3.Row) -> WorkflowEventRecord:
        evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else None
        return WorkflowEventRecord(
            id=row["id"],
            change_id=row["change_id"],
            action=row["action"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            message=row["message"],
            created_at=row["created_at"],
            evidence=evidence,
        )

    def _execution_event(self, row: sqlite3.Row) -> ExecutionEventRecord:
        return ExecutionEventRecord(
            id=row["id"],
            job_id=row["job_id"],
            change_id=row["change_id"],
            org_id=row["org_id"],
            device_id=row["device_id"],
            phase=row["phase"],
            stage=row["stage"],
            status=row["status"],
            message=row["message"],
            sequence=int(row["sequence"]),
            current_step=int(row["current_step"]) if row["current_step"] is not None else None,
            total_steps=int(row["total_steps"]) if row["total_steps"] is not None else None,
            command=row["command"],
            created_at=row["created_at"],
        )


_SENSITIVE_PAYLOAD_KEYS = (
    "password", "passwd", "pwd", "secret", "token", "credential", "enable_secret",
    "passphrase", "api_key", "apikey", "private_key", "privatekey",
    "username", "login",  # the account name to reach an untrusted device is recon-sensitive
)


def redact_secrets(value: Any) -> Any:
    """Recursively replace sensitive values so device credentials are never
    surfaced through the API. Discovery of an untrusted device is the one moment
    creds transit; they must never be read back out of a job payload."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SENSITIVE_PAYLOAD_KEYS) and item not in (None, ""):
                redacted[key] = "***redacted***"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def record_to_dict(
    record: ChangeRecord | JobRecord | WorkflowEventRecord | ExecutionEventRecord,
) -> dict[str, Any]:
    data = record.__dict__.copy()
    data.pop("lease_token", None)
    if isinstance(record, ChangeRecord):
        data["rez_change_id"] = change_audit_id(record.id, record.created_at)
    if "payload" in data and data["payload"]:
        data["payload"] = redact_secrets(data["payload"])
    return data


def change_summary_to_dict(record: ChangeRecord) -> dict[str, Any]:
    """Serialize a bounded list row; full evidence stays on the record endpoint."""
    result = record.result if isinstance(record.result, dict) else {}
    source = record.source or str(result.get("source") or "").strip().lower()
    path_parts = {part.lower() for part in Path(record.intent_path).parts}
    if not source:
        source = "rez_rca" if "rca" in path_parts else "ansible" if "ansible" in path_parts else "netcode"
    source = {"netcode_ansible": "ansible", "rez": "rez_rca"}.get(source, source)
    title = record.title or str(result.get("title") or "").strip() or Path(record.intent_path).name
    workflow_type = record.workflow_type or str(result.get("change_type") or "").strip()
    return {
        "id": record.id,
        "rez_change_id": change_audit_id(record.id, record.created_at),
        "status": record.status,
        "workflow_state": record.workflow_state,
        "intent_name": Path(record.intent_path).name,
        "device_id": record.device_id,
        "requested_by": record.requested_by,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_job_id": record.last_job_id,
        "org_id": record.org_id,
        "title": title[:240],
        "source": source[:80],
        "site": record.site,
        "workflow_type": workflow_type[:120],
        "result": {
            "source": source[:80],
            "title": title[:240],
            "change_type": workflow_type[:120],
        },
    }
