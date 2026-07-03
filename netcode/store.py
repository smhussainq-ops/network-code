"""Durable SQLite store for changes and jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netcode.paths import WorkspacePaths


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


@dataclass(frozen=True)
class RunnerRecord:
    id: str
    name: str
    pool: str
    status: str
    version: str
    created_at: str
    last_seen: str | None


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
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

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

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_change(self, intent_path: Path, device_id: str | None, requested_by: str = "netcode-user") -> ChangeRecord:
        now = utc_now()
        change_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO changes
                (id, status, workflow_state, intent_path, device_id, requested_by, created_at, updated_at, last_job_id, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (change_id, "draft", "draft", str(intent_path), device_id, requested_by, now, now, None, None),
            )
        return self.get_change(change_id)

    def get_or_create_change(self, intent_path: Path, device_id: str | None, requested_by: str = "netcode-user") -> ChangeRecord:
        intent = str(intent_path)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM changes
                WHERE intent_path = ? AND COALESCE(device_id, '') = COALESCE(?, '')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (intent, device_id),
            ).fetchone()
        if row:
            return self._change(row)
        return self.create_change(intent_path, device_id, requested_by=requested_by)

    def create_job(self, change_id: str, action: str) -> JobRecord:
        now = utc_now()
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                (id, change_id, action, status, message, created_at, updated_at, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, change_id, action, "queued", "Queued", now, now, None),
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

    def list_changes(self, limit: int = 50) -> list[ChangeRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM changes ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._change(row) for row in rows]

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
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
        )

    def _job(self, row: sqlite3.Row) -> JobRecord:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        keys = row.keys()
        payload = json.loads(row["payload_json"]) if "payload_json" in keys and row["payload_json"] else None
        return JobRecord(
            id=row["id"],
            change_id=row["change_id"],
            action=row["action"],
            status=row["status"],
            message=row["message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=result,
            pool=row["pool"] if "pool" in keys else None,
            payload=payload,
            claimed_by=row["claimed_by"] if "claimed_by" in keys else None,
            signature=row["signature"] if "signature" in keys else None,
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
        )

    # ── Runner registry & job queue (Phase 0 SaaS split) ──────────────────

    def create_join_token(self, token_hash: str, pool: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO join_tokens (token_hash, pool, created_at, used_at) VALUES (?, ?, ?, NULL)",
                (token_hash, pool, utc_now()),
            )

    def consume_join_token(self, token_hash: str) -> str | None:
        """Atomically mark a join token used; returns its pool or None if invalid/replayed."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE join_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (utc_now(), token_hash),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT pool FROM join_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            return row["pool"] if row else None

    def create_runner(self, name: str, pool: str, token_hash: str, hmac_secret: str) -> RunnerRecord:
        runner_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runners (id, name, pool, token_hash, hmac_secret, status, version, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (runner_id, name, pool, token_hash, hmac_secret, "enrolled", "", now, now),
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

    def list_runners(self) -> list[RunnerRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runners ORDER BY created_at DESC").fetchall()
        return [self._runner(row) for row in rows]

    def queue_job(self, change_id: str, action: str, pool: str, payload: dict[str, Any]) -> JobRecord:
        job = self.create_job(change_id, action)
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET pool = ?, payload_json = ?, message = ? WHERE id = ?",
                (pool, json.dumps(payload), f"Queued for runner pool {pool}", job.id),
            )
        return self.get_job(job.id)

    def claim_next_job(self, pool: str, runner_id: str) -> JobRecord | None:
        """Atomically claim the oldest queued job for a pool. Safe for concurrent runners."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND pool = ? ORDER BY created_at ASC LIMIT 1",
                (pool,),
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
