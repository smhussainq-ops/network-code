from __future__ import annotations

from typing import Any

from netcode.store import PlatformStore


class _Rows:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows


class _RecordingConnection:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def execute(self, sql: str, _params: Any = None) -> _Rows:
        self.statements.append(sql)
        if "information_schema.table_constraints" in sql:
            return _Rows(self.rows)
        return _Rows([])


def _store(engine: str) -> PlatformStore:
    store = object.__new__(PlatformStore)
    store.engine = engine
    return store


def test_postgres_drops_legacy_jobs_change_foreign_key() -> None:
    conn = _RecordingConnection([{"constraint_name": "jobs_change_id_fkey"}])

    _store("postgres")._drop_legacy_jobs_change_foreign_keys(conn)

    assert any("ccu.table_name = 'changes'" in statement for statement in conn.statements)
    assert conn.statements[-1] == 'ALTER TABLE jobs DROP CONSTRAINT "jobs_change_id_fkey"'


def test_postgres_quotes_discovered_constraint_names() -> None:
    conn = _RecordingConnection([{"constraint_name": 'legacy"constraint'}])

    _store("postgres")._drop_legacy_jobs_change_foreign_keys(conn)

    assert conn.statements[-1] == 'ALTER TABLE jobs DROP CONSTRAINT "legacy""constraint"'


def test_sqlite_skips_postgres_constraint_migration() -> None:
    conn = _RecordingConnection([])

    _store("sqlite")._drop_legacy_jobs_change_foreign_keys(conn)

    assert conn.statements == []
