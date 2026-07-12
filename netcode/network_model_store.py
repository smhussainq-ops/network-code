"""Durable repository for the Rezonance operational network model."""

from __future__ import annotations

import base64
import json
from typing import Any, Iterable, Mapping

from netcode.network_model import validate_model_revision, validate_observation
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
                CREATE TABLE IF NOT EXISTS network_model_observation_heads (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id, domain, subject_id)
                )
                """
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS network_model_reconciliations (
                    org_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    reconciliation_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    site_id TEXT NOT NULL DEFAULT '',
                    device_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, environment_id, reconciliation_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_reconciliations_revision "
                "ON network_model_reconciliations (org_id, environment_id, revision_id, created_at)"
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

    def record_observation(self, value: Mapping[str, Any]) -> dict[str, Any]:
        observation = validate_observation(value)
        key = (observation["org_id"], observation["environment_id"], observation["observation_id"])
        now = utc_now()
        with self.store._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM network_model_observations "
                "WHERE org_id = ? AND environment_id = ? AND observation_id = ?",
                key,
            ).fetchone()
            if existing:
                current = self._observation(existing)
                comparable = {field: current.get(field) for field in observation}
                if comparable != observation:
                    raise ValueError(
                        f"observation {observation['observation_id']} already exists with different content"
                    )
                return {"created": False, "observation": current}
            conn.execute(
                """
                INSERT INTO network_model_observations
                (org_id, environment_id, observation_id, domain, subject_id, source, collector_id,
                 observed_at, expires_at, validation_grade, facts_json, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation["org_id"], observation["environment_id"], observation["observation_id"],
                    observation["domain"], observation["subject_id"], observation["source"],
                    observation["collector_id"], observation["observed_at"], observation.get("expires_at"),
                    observation["validation_grade"], _json(observation["facts"]),
                    _json(observation.get("metadata") or {}), now,
                ),
            )
            conn.execute(
                """
                INSERT INTO network_model_observation_heads
                (org_id, environment_id, domain, subject_id, observation_id, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (org_id, environment_id, domain, subject_id)
                DO UPDATE SET observation_id = ?, observed_at = ?
                WHERE ? > network_model_observation_heads.observed_at
                """,
                (
                    observation["org_id"], observation["environment_id"], observation["domain"],
                    observation["subject_id"], observation["observation_id"], observation["observed_at"],
                    observation["observation_id"], observation["observed_at"], observation["observed_at"],
                ),
            )
        return {"created": True, "observation": self.get_observation(*key)}

    def get_observation(self, org_id: str, environment_id: str, observation_id: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM network_model_observations "
                "WHERE org_id = ? AND environment_id = ? AND observation_id = ?",
                (org_id, environment_id, observation_id),
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown network model observation {observation_id}")
        return self._observation(row)

    def list_observations(
        self,
        org_id: str,
        environment_id: str,
        *,
        domain: str = "",
        subject_id: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        size = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses = ["org_id = ?", "environment_id = ?"]
        params: list[Any] = [org_id, environment_id]
        for column, value in (("domain", domain), ("subject_id", subject_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        parsed = _parse_cursor(cursor)
        if parsed:
            clauses.append("(observed_at < ? OR (observed_at = ? AND observation_id < ?))")
            params.extend([parsed[0], parsed[0], parsed[1]])
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM network_model_observations WHERE " + " AND ".join(clauses)
                + " ORDER BY observed_at DESC, observation_id DESC LIMIT ?",
                (*params, size + 1),
            ).fetchall()
        page = rows[:size]
        next_cursor = (
            _cursor(page[-1]["observed_at"], page[-1]["observation_id"])
            if len(rows) > size
            else None
        )
        return {
            "observations": [self._observation(row) for row in page],
            "returned": len(page),
            "next_cursor": next_cursor,
        }

    def current_observations(
        self,
        org_id: str,
        environment_id: str,
        subjects: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        unique = sorted({(str(domain), str(subject)) for domain, subject in subjects if domain and subject})
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for offset in range(0, len(unique), 200):
            chunk = unique[offset : offset + 200]
            clauses = ["(h.domain = ? AND h.subject_id = ?)" for _ in chunk]
            params: list[Any] = [org_id, environment_id]
            for domain, subject in chunk:
                params.extend([domain, subject])
            with self.store._connect() as conn:
                rows = conn.execute(
                    "SELECT o.* FROM network_model_observation_heads h "
                    "JOIN network_model_observations o ON o.org_id = h.org_id "
                    "AND o.environment_id = h.environment_id AND o.observation_id = h.observation_id "
                    "WHERE h.org_id = ? AND h.environment_id = ? AND (" + " OR ".join(clauses) + ")",
                    tuple(params),
                ).fetchall()
            for row in rows:
                item = self._observation(row)
                found[(item["domain"], item["subject_id"])] = item
        return found

    def save_reconciliation(
        self,
        *,
        org_id: str,
        environment_id: str,
        reconciliation_id: str,
        revision_id: str,
        site_id: str,
        device_id: str,
        status: str,
        summary: Mapping[str, Any],
        findings: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.store._connect() as conn:
            conn.execute(
                """
                INSERT INTO network_model_reconciliations
                (org_id, environment_id, reconciliation_id, revision_id, site_id, device_id,
                 status, summary_json, findings_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    org_id, environment_id, reconciliation_id, revision_id, site_id, device_id,
                    status, _json(dict(summary)), _json(findings), now,
                ),
            )
        return {
            "reconciliation_id": reconciliation_id,
            "revision_id": revision_id,
            "site_id": site_id,
            "device_id": device_id,
            "status": status,
            "summary": dict(summary),
            "findings": [dict(item) for item in findings],
            "created_at": now,
        }

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

    @staticmethod
    def _observation(row: Any) -> dict[str, Any]:
        return {
            "schema": "rezonance.network-observation.v1",
            "org_id": row["org_id"],
            "environment_id": row["environment_id"],
            "observation_id": row["observation_id"],
            "domain": row["domain"],
            "subject_id": row["subject_id"],
            "source": row["source"],
            "collector_id": row["collector_id"],
            "observed_at": row["observed_at"],
            "expires_at": row["expires_at"],
            "validation_grade": row["validation_grade"],
            "facts": _decode_json(row["facts_json"], {}),
            "metadata": _decode_json(row["metadata_json"], {}),
        }
