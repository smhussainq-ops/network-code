"""Durable repository for the Rezonance operational network model."""

from __future__ import annotations

import base64
import json
from typing import Any, Iterable, Mapping

from netcode.network_model import validate_model_revision
from netcode.store import PlatformStore, utc_now


MAX_MODEL_BYTES = 25 * 1024 * 1024
MAX_PAGE_SIZE = 100


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _cursor(created_at: str, record_id: str) -> str:
    raw = f"{created_at}\n{record_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_cursor(value: str) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at, record_id = base64.urlsafe_b64decode(padded.encode()).decode().split("\n", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    return created_at, record_id


def _entity_rows(model: Mapping[str, Any]) -> Iterable[tuple[str, str, str, str, dict[str, Any]]]:
    """Yield bounded-query materializations while retaining the canonical JSON document."""
    for raw_type, raw_records in model.items():
        entity_type = str(raw_type).strip().lower()
        if not entity_type:
            continue
        if isinstance(raw_records, Mapping):
            records = [(str(key), value) for key, value in raw_records.items()]
        elif isinstance(raw_records, list):
            records = []
            for index, value in enumerate(raw_records):
                record = value if isinstance(value, Mapping) else {"value": value}
                entity_id = str(record.get("id") or record.get("name") or index)
                records.append((entity_id, record))
        else:
            records = [(entity_type, {"value": raw_records})]

        for raw_id, raw_record in records:
            record = dict(raw_record) if isinstance(raw_record, Mapping) else {"value": raw_record}
            entity_id = str(raw_id or record.get("id") or record.get("name") or "").strip()
            if not entity_id:
                continue
            site = str(record.get("site") or (entity_id if entity_type == "sites" else "")).strip()
            device_id = str(
                record.get("device_id")
                or record.get("device")
                or (entity_id if entity_type == "devices" else "")
            ).strip()
            yield entity_type, entity_id, site, device_id, record


class NetworkModelRepository:
    """Tenant-scoped model persistence over the existing SQLite/Postgres store."""

    def __init__(self, store: PlatformStore):
        self.store = store
        self._init()

    def _init(self) -> None:
        with self.store._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_revisions (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    parent_revision_id TEXT,
                    status TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_reference TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    authority_json TEXT NOT NULL,
                    model_json TEXT NOT NULL,
                    approval_json TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id, revision_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_revisions_status "
                "ON network_model_revisions (org_id, environment_id, status, created_at, revision_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_entities (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    site TEXT NOT NULL DEFAULT '',
                    device_id TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id, revision_id, entity_type, entity_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_entities_query "
                "ON network_model_entities (org_id, environment_id, revision_id, entity_type, entity_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_entities_site "
                "ON network_model_entities (org_id, environment_id, revision_id, site, entity_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_entities_device "
                "ON network_model_entities (org_id, environment_id, revision_id, device_id, entity_type)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_heads (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    active_revision_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_observations (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    subject_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    collector_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    expires_at TEXT,
                    validation_grade TEXT NOT NULL DEFAULT 'unknown',
                    facts_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id, observation_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_observations_subject "
                "ON network_model_observations (org_id, environment_id, domain, subject_id, observed_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_conflicts (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    conflict_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    subject_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    PRIMARY KEY (org_id, environment_id, conflict_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_conflicts_status "
                "ON network_model_conflicts (org_id, environment_id, status, domain, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_links (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id, revision_id, link_type, external_id)
                )
                """
            )

    def create_revision(self, value: Mapping[str, Any], *, created_by: str) -> dict[str, Any]:
        document = validate_model_revision(value)
        if document["status"] in {"active", "superseded"}:
            raise ValueError("active and superseded states are assigned only by the governed activation lifecycle")
        model_json = _json(document["model"])
        if len(model_json.encode()) > MAX_MODEL_BYTES:
            raise ValueError(f"network model exceeds {MAX_MODEL_BYTES} bytes")
        now = utc_now()
        key = (document["org_id"], document["environment_id"], document["revision_id"])
        with self.store._connect() as conn:
            if document.get("parent_revision_id"):
                parent = conn.execute(
                    "SELECT revision_id FROM network_model_revisions "
                    "WHERE org_id = ? AND environment_id = ? AND revision_id = ?",
                    (document["org_id"], document["environment_id"], document["parent_revision_id"]),
                ).fetchone()
                if not parent:
                    raise ValueError(
                        f"Parent network model revision {document['parent_revision_id']} does not exist in this environment"
                    )
            existing = conn.execute(
                "SELECT revision_id FROM network_model_revisions "
                "WHERE org_id = ? AND environment_id = ? AND revision_id = ?",
                key,
            ).fetchone()
            if existing:
                raise ValueError(f"Network model revision {document['revision_id']} already exists")
            conn.execute(
                """
                INSERT INTO network_model_revisions
                (org_id, environment_id, revision_id, parent_revision_id, status, source_type,
                 source_reference, coverage_json, authority_json, model_json, approval_json,
                 created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["org_id"],
                    document["environment_id"],
                    document["revision_id"],
                    document.get("parent_revision_id"),
                    document["status"],
                    document["source"]["type"],
                    document["source"]["reference"],
                    _json(document["coverage"]),
                    _json(document["authority_bindings"]),
                    model_json,
                    _json(document.get("approval")) if document.get("approval") else None,
                    str(created_by or "system")[:200],
                    now,
                    now,
                ),
            )
            for entity_type, entity_id, site, device_id, record in _entity_rows(document["model"]):
                conn.execute(
                    """
                    INSERT INTO network_model_entities
                    (org_id, environment_id, revision_id, entity_type, entity_id, site, device_id, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*key, entity_type, entity_id, site, device_id, _json(record)),
                )
        return self.get_revision(*key)

    def get_revision(self, org_id: str, environment_id: str, revision_id: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM network_model_revisions "
                "WHERE org_id = ? AND environment_id = ? AND revision_id = ?",
                (org_id, environment_id, revision_id),
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown network model revision {revision_id}")
        return self._revision(row, include_model=True)

    def list_revisions(
        self,
        org_id: str,
        environment_id: str,
        *,
        status: str = "",
        cursor: str = "",
        limit: int = 25,
    ) -> dict[str, Any]:
        size = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses = ["org_id = ?", "environment_id = ?"]
        params: list[Any] = [org_id, environment_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        parsed = _parse_cursor(cursor)
        if parsed:
            clauses.append("(created_at < ? OR (created_at = ? AND revision_id < ?))")
            params.extend([parsed[0], parsed[0], parsed[1]])
        where = " AND ".join(clauses)
        with self.store._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM network_model_revisions WHERE {where} "
                "ORDER BY created_at DESC, revision_id DESC LIMIT ?",
                (*params, size + 1),
            ).fetchall()
        page = rows[:size]
        next_cursor = _cursor(page[-1]["created_at"], page[-1]["revision_id"]) if len(rows) > size else None
        return {
            "revisions": [self._revision(row, include_model=False) for row in page],
            "returned": len(page),
            "next_cursor": next_cursor,
        }

    def list_entities(
        self,
        org_id: str,
        environment_id: str,
        revision_id: str,
        *,
        entity_type: str = "",
        site: str = "",
        device_id: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        size = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses = ["org_id = ?", "environment_id = ?", "revision_id = ?"]
        params: list[Any] = [org_id, environment_id, revision_id]
        for column, value in (("entity_type", entity_type), ("site", site), ("device_id", device_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if cursor:
            clauses.append("(entity_type || ':' || entity_id) > ?")
            params.append(cursor)
        where = " AND ".join(clauses)
        with self.store._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM network_model_entities WHERE {where} "
                "ORDER BY entity_type, entity_id LIMIT ?",
                (*params, size + 1),
            ).fetchall()
        page = rows[:size]
        entities = [
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "site": row["site"],
                "device_id": row["device_id"],
                "data": _decode_json(row["data_json"], {}),
            }
            for row in page
        ]
        next_cursor = (
            f"{page[-1]['entity_type']}:{page[-1]['entity_id']}" if len(rows) > size else None
        )
        return {"entities": entities, "returned": len(entities), "next_cursor": next_cursor}

    @staticmethod
    def _revision(row: Any, *, include_model: bool) -> dict[str, Any]:
        value = {
            "schema": "rezonance.network-model.v1",
            "org_id": row["org_id"],
            "environment_id": row["environment_id"],
            "revision_id": row["revision_id"],
            "parent_revision_id": row["parent_revision_id"],
            "status": row["status"],
            "source": {"type": row["source_type"], "reference": row["source_reference"]},
            "coverage": _decode_json(row["coverage_json"], {"domains": []}),
            "authority_bindings": _decode_json(row["authority_json"], {}),
            "approval": _decode_json(row["approval_json"], None),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_model:
            value["model"] = _decode_json(row["model_json"], {})
        return value
