"""Durable store for changes, jobs, runners, and (M5) orgs/users/sessions.

Backed by SQLite by default; Postgres-ready via DATABASE_URL. All SQL uses `?`
positional placeholders; the connection wrapper rewrites them to `%s` for the
Postgres engine so call sites stay engine-agnostic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netcode.paths import WorkspacePaths


DEFAULT_ORG_ID = "org_default"


def database_url(paths: WorkspacePaths) -> str:
    """SQLite by default (paths.database stays the source of truth); Postgres via DATABASE_URL."""
    return os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{paths.database}"


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
                    result_json TEXT,
                    FOREIGN KEY(change_id) REFERENCES changes(id)
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
            for table in ("changes", "jobs", "runners", "join_tokens"):
                self._ensure_column(conn, table, "org_id", f"TEXT DEFAULT '{DEFAULT_ORG_ID}'")
            self._ensure_column(conn, "changes", "created_by_user_id", "TEXT")
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO changes
                (id, status, workflow_state, intent_path, device_id, requested_by, created_at, updated_at, last_job_id, result_json, org_id, created_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (change_id, "draft", "draft", str(intent_path), device_id, requested_by, now, now, None, None, org_id, created_by_user_id),
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
            conn.execute(
                """
                UPDATE jobs SET status = ?, message = ?, updated_at = ?, result_json = ? WHERE id = ?
                """,
                (status, message, now, result_json, job_id),
            )
        return self.get_job(job_id)

    def update_change(self, change_id: str, status: str, result: dict[str, Any] | None = None, workflow_state: str | None = None) -> ChangeRecord:
        now = utc_now()
        result_json = json.dumps(result) if result is not None else None
        with self._connect() as conn:
            if workflow_state is None:
                conn.execute(
                    "UPDATE changes SET status = ?, updated_at = ?, result_json = ? WHERE id = ?",
                    (status, now, result_json, change_id),
                )
            else:
                conn.execute(
                    "UPDATE changes SET status = ?, workflow_state = ?, updated_at = ?, result_json = ? WHERE id = ?",
                    (status, workflow_state, now, result_json, change_id),
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

    def create_runner(self, name: str, pool: str, token_hash: str, hmac_secret: str, org_id: str = DEFAULT_ORG_ID) -> RunnerRecord:
        runner_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runners (id, name, pool, token_hash, hmac_secret, status, version, created_at, last_seen, org_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (runner_id, name, pool, token_hash, hmac_secret, "enrolled", "", now, now, org_id),
            )
        return self.get_runner(runner_id)

    def get_runner(self, runner_id: str) -> RunnerRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown runner {runner_id}")
        return self._runner(row)

    def runner_by_token_hash(self, token_hash: str) -> RunnerRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runners WHERE token_hash = ?", (token_hash,)).fetchone()
        return self._runner(row) if row else None

    def runner_hmac_secret(self, runner_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT hmac_secret FROM runners WHERE id = ?", (runner_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown runner {runner_id}")
        return row["hmac_secret"]

    def touch_runner(self, runner_id: str, status: str = "online", version: str | None = None) -> None:
        with self._connect() as conn:
            if version is None:
                conn.execute("UPDATE runners SET status = ?, last_seen = ? WHERE id = ?", (status, utc_now(), runner_id))
            else:
                conn.execute(
                    "UPDATE runners SET status = ?, version = ?, last_seen = ? WHERE id = ?",
                    (status, version, utc_now(), runner_id),
                )

    def list_runners(self, org_id: str | None = None) -> list[RunnerRecord]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute("SELECT * FROM runners ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM runners WHERE org_id = ? ORDER BY created_at DESC", (org_id,)).fetchall()
        return [self._runner(row) for row in rows]

    def queue_job(self, change_id: str, action: str, pool: str, payload: dict[str, Any]) -> JobRecord:
        job = self.create_job(change_id, action)  # inherits org_id from the parent change
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET pool = ?, payload_json = ?, message = ? WHERE id = ?",
                (pool, json.dumps(payload), f"Queued for runner pool {pool}", job.id),
            )
        return self.get_job(job.id)

    def claim_next_job(self, org_id: str, pool: str, runner_id: str) -> JobRecord | None:
        """Atomically claim the oldest queued job for a (org, pool). Concurrent- and tenant-safe:
        a runner may only claim jobs in its OWN org, so colliding pool names across tenants stay isolated."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND org_id = ? AND pool = ? ORDER BY created_at ASC LIMIT 1",
                (org_id, pool),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                "UPDATE jobs SET status = 'running', claimed_by = ?, message = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                (runner_id, f"Claimed by runner {runner_id}", utc_now(), row["id"]),
            )
            if cursor.rowcount != 1:
                return None  # another runner won the race
        return self.get_job(row["id"])

    def record_job_signature(self, job_id: str, signature: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET signature = ? WHERE id = ?", (signature, job_id))

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


def record_to_dict(record: ChangeRecord | JobRecord | WorkflowEventRecord) -> dict[str, Any]:
    return record.__dict__.copy()
