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
        self.parameters: list[Any] = []

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _Rows:
        self.statements.append(sql)
        self.parameters.append(params)
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


def test_shell_counter_update_never_sends_untyped_null_for_touch_flag() -> None:
    conn = _RecordingConnection([])
    store = _store("postgres")
    store._connect = lambda: conn  # type: ignore[method-assign]
    store.get_shell_session = lambda session_id: {"id": session_id}  # type: ignore[method-assign]

    updated = store.update_shell_session("shell-1", output_bytes_delta=12)

    assert updated == {"id": "shell-1"}
    params = conn.parameters[-1]
    assert params[4:6] == (0, 0)
    assert "CASE WHEN ? = 1 THEN ? ELSE device_touched END" in conn.statements[-1]
