"""Durable runner-local replay protection for device operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_hash(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperationDecision:
    mode: str
    result: dict[str, Any] | None = None


class RunnerOperationLedger:
    """Serialize and remember operations across connector process restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    change_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def begin(
        self,
        operation_key: str,
        request: dict[str, Any],
        *,
        action: str,
        change_id: str,
        device_id: str,
    ) -> OperationDecision:
        key = str(operation_key or "").strip()
        if not key:
            raise ValueError("A durable operation key is required")
        fingerprint = _request_hash(request)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_key = ?",
                (key,),
            ).fetchone()
            if row:
                if str(row["request_hash"]) != fingerprint:
                    raise ValueError("Operation key was already used for a different runner request")
                if str(row["status"]) == "completed":
                    return OperationDecision("replay", json.loads(row["result_json"] or "{}"))
                return OperationDecision("reconcile_required")
            conn.execute(
                "INSERT INTO operations (operation_key, request_hash, action, change_id, device_id, "
                "status, result_json, started_at, completed_at) VALUES (?, ?, ?, ?, ?, 'started', NULL, ?, NULL)",
                (
                    key,
                    fingerprint,
                    str(action),
                    str(change_id),
                    str(device_id).strip().lower(),
                    _now(),
                ),
            )
        return OperationDecision("execute")

    def complete(self, operation_key: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE operations SET status = 'completed', result_json = ?, completed_at = ? "
                "WHERE operation_key = ? AND status = 'started'",
                (json.dumps(result, sort_keys=True, default=str), _now(), operation_key),
            )
            if cursor.rowcount != 1:
                raise ValueError("Runner operation is missing or already completed")

    def get(self, operation_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json") or "{}")
        return value
