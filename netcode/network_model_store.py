"""Durable repository for the Rezonance operational network model."""

from __future__ import annotations

import base64
import json
from typing import Any, Iterable, Mapping

from netcode.network_model import (
    assert_no_secrets,
    prepare_reviewed_approval,
    validate_model_revision,
    validate_observation,
)
from netcode.store import PlatformStore, utc_now


MAX_MODEL_BYTES = 25 * 1024 * 1024
MAX_PAGE_SIZE = 100
_REVISION_SUMMARY_COLUMNS = (
    "org_id, environment_id, revision_id, parent_revision_id, status, source_type, "
    "source_reference, coverage_json, authority_json, approval_json, created_by, created_at, updated_at"
)


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

    # Approved Rez designs organize devices and dependencies beneath each site.
    # Materialize those nested records into the same bounded serving index used
    # by catalog-first models, while retaining the canonical document unchanged.
    sites = model.get("sites") if isinstance(model.get("sites"), Mapping) else {}
    has_top_level_devices = isinstance(model.get("devices"), Mapping)
    for raw_site_id, raw_site in sites.items():
        site_id = str(raw_site_id).strip()
        site = dict(raw_site) if isinstance(raw_site, Mapping) else {}
        if not site_id:
            continue
        if not has_top_level_devices:
            nested_devices = site.get("devices") if isinstance(site.get("devices"), Mapping) else {}
            for raw_device_id, raw_device in nested_devices.items():
                device_id = str(raw_device_id).strip()
                if not device_id:
                    continue
                record = dict(raw_device) if isinstance(raw_device, Mapping) else {}
                record["site"] = site_id
                yield "devices", device_id, site_id, device_id, record
        for nested_type in (
            "address_plan",
            "routing_domains",
            "redistribution_boundaries",
            "reachability",
            "operational_dependencies",
        ):
            values = site.get(nested_type)
            if not isinstance(values, list):
                continue
            for index, raw_value in enumerate(values):
                record = dict(raw_value) if isinstance(raw_value, Mapping) else {"value": raw_value}
                local_id = str(record.get("id") or record.get("name") or index)
                entity_id = f"{site_id}:{local_id}"
                record["site"] = site_id
                yield nested_type, entity_id, site_id, str(record.get("device_id") or ""), record


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
                "CREATE INDEX IF NOT EXISTS idx_network_model_links_external "
                "ON network_model_links (org_id, link_type, external_id, environment_id, revision_id)"
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
                f"SELECT {_REVISION_SUMMARY_COLUMNS} FROM network_model_revisions WHERE {where} "
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

    def approve_revision(
        self,
        org_id: str,
        environment_id: str,
        revision_id: str,
        *,
        approved_by: str,
        approved_at: str,
    ) -> dict[str, Any]:
        revision = self.get_revision(org_id, environment_id, revision_id)
        if revision["status"] in {"approved", "active", "superseded"}:
            return revision
        if revision["status"] not in {"proposed", "in_review"}:
            raise ValueError(f"revision {revision_id} cannot be approved from {revision['status']}")
        validated = prepare_reviewed_approval(
            revision,
            approved_by=approved_by,
            approved_at=approved_at,
        )
        with self.store._connect() as conn:
            conn.execute(
                """
                UPDATE network_model_revisions
                SET status = 'approved', source_type = ?, source_reference = ?,
                    approval_json = ?, authority_json = ?, updated_at = ?
                WHERE org_id = ? AND environment_id = ? AND revision_id = ?
                  AND status IN ('proposed', 'in_review')
                """,
                (
                    validated["source"]["type"], validated["source"]["reference"],
                    _json(validated["approval"]), _json(validated["authority_bindings"]), utc_now(),
                    org_id, environment_id, revision_id,
                ),
            )
        return self.get_revision(org_id, environment_id, revision_id)

    def link_revision(
        self,
        org_id: str,
        environment_id: str,
        revision_id: str,
        *,
        link_type: str,
        external_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_revision(org_id, environment_id, revision_id)
        now = utc_now()
        with self.store._connect() as conn:
            conn.execute(
                """
                INSERT INTO network_model_links
                (org_id, environment_id, revision_id, link_type, external_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (org_id, environment_id, revision_id, link_type, external_id)
                DO UPDATE SET metadata_json = ?
                """,
                (
                    org_id, environment_id, revision_id, link_type, external_id,
                    _json(dict(metadata or {})), now, _json(dict(metadata or {})),
                ),
            )
        return {
            "revision_id": revision_id,
            "link_type": link_type,
            "external_id": external_id,
            "metadata": dict(metadata or {}),
        }

    def list_links(self, org_id: str, environment_id: str, revision_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM network_model_links WHERE org_id = ? AND environment_id = ? AND revision_id = ? "
                "ORDER BY link_type, external_id",
                (org_id, environment_id, revision_id),
            ).fetchall()
        return [
            {
                "link_type": row["link_type"],
                "external_id": row["external_id"],
                "metadata": _decode_json(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def revisions_for_link(
        self,
        org_id: str,
        *,
        link_type: str,
        external_id: str,
        environment_id: str = "",
    ) -> list[dict[str, Any]]:
        """Return bounded revision summaries linked to one tenant-owned record."""
        clauses = ["l.org_id = ?", "l.link_type = ?", "l.external_id = ?"]
        params: list[Any] = [org_id, link_type, external_id]
        if environment_id:
            clauses.append("l.environment_id = ?")
            params.append(environment_id)
        columns = ", ".join(
            f"r.{column.strip()}" for column in _REVISION_SUMMARY_COLUMNS.split(",")
        )
        with self.store._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {columns}
                FROM network_model_links l
                JOIN network_model_revisions r
                  ON r.org_id = l.org_id
                 AND r.environment_id = l.environment_id
                 AND r.revision_id = l.revision_id
                WHERE {' AND '.join(clauses)}
                ORDER BY r.created_at, r.revision_id
                LIMIT 100
                """,
                params,
            ).fetchall()
        return [self._revision(row, include_model=False) for row in rows]

    def active_revision(self, org_id: str, environment_id: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT active_revision_id FROM network_model_heads WHERE org_id = ? AND environment_id = ?",
                (org_id, environment_id),
            ).fetchone()
        return self.get_revision(org_id, environment_id, row["active_revision_id"]) if row else None

    def active_revision_summary(self, org_id: str, environment_id: str) -> dict[str, Any] | None:
        """Return active metadata without reading or decoding the model blob."""
        with self.store._connect() as conn:
            row = conn.execute(
                """
                SELECT r.org_id, r.environment_id, r.revision_id, r.parent_revision_id,
                       r.status, r.source_type, r.source_reference, r.coverage_json,
                       r.authority_json, r.approval_json, r.created_by, r.created_at, r.updated_at
                FROM network_model_heads h
                JOIN network_model_revisions r
                  ON r.org_id = h.org_id
                 AND r.environment_id = h.environment_id
                 AND r.revision_id = h.active_revision_id
                WHERE h.org_id = ? AND h.environment_id = ?
                """,
                (org_id, environment_id),
            ).fetchone()
        return self._revision(row, include_model=False) if row else None

    def record_conflict(
        self,
        *,
        org_id: str,
        environment_id: str,
        conflict_id: str,
        domain: str,
        subject_id: str,
        severity: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        assert_no_secrets(details, "conflict.details")
        now = utc_now()
        with self.store._connect() as conn:
            conn.execute(
                """
                INSERT INTO network_model_conflicts
                (org_id, environment_id, conflict_id, domain, subject_id, status,
                 severity, details_json, created_at, resolved_at, resolved_by)
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, NULL, NULL)
                ON CONFLICT (org_id, environment_id, conflict_id)
                DO UPDATE SET details_json = ?, severity = ?, status = 'open',
                              resolved_at = NULL, resolved_by = NULL
                """,
                (
                    org_id, environment_id, conflict_id, domain, subject_id,
                    severity, _json(dict(details)), now, _json(dict(details)), severity,
                ),
            )
        return self.get_conflict(org_id, environment_id, conflict_id)

    def get_conflict(self, org_id: str, environment_id: str, conflict_id: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM network_model_conflicts "
                "WHERE org_id = ? AND environment_id = ? AND conflict_id = ?",
                (org_id, environment_id, conflict_id),
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown network model conflict {conflict_id}")
        return self._conflict(row)

    def list_conflicts(
        self,
        org_id: str,
        environment_id: str,
        *,
        status: str = "open",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        size = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses = ["org_id = ?", "environment_id = ?"]
        params: list[Any] = [org_id, environment_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        parsed = _parse_cursor(cursor)
        if parsed:
            clauses.append("(created_at < ? OR (created_at = ? AND conflict_id < ?))")
            params.extend([parsed[0], parsed[0], parsed[1]])
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM network_model_conflicts WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC, conflict_id DESC LIMIT ?",
                (*params, size + 1),
            ).fetchall()
        page = rows[:size]
        return {
            "conflicts": [self._conflict(row) for row in page],
            "returned": len(page),
            "next_cursor": (
                _cursor(page[-1]["created_at"], page[-1]["conflict_id"])
                if len(rows) > size else None
            ),
        }

    def resolve_conflict(
        self,
        org_id: str,
        environment_id: str,
        conflict_id: str,
        *,
        resolved_by: str,
        resolution: Mapping[str, Any],
    ) -> dict[str, Any]:
        conflict = self.get_conflict(org_id, environment_id, conflict_id)
        assert_no_secrets(resolution, "conflict.resolution")
        details = dict(conflict["details"])
        details["resolution"] = dict(resolution)
        now = utc_now()
        with self.store._connect() as conn:
            conn.execute(
                """
                UPDATE network_model_conflicts
                SET status = 'resolved', details_json = ?, resolved_at = ?, resolved_by = ?
                WHERE org_id = ? AND environment_id = ? AND conflict_id = ? AND status = 'open'
                """,
                (_json(details), now, str(resolved_by), org_id, environment_id, conflict_id),
            )
        return self.get_conflict(org_id, environment_id, conflict_id)

    def activate_revision(
        self,
        org_id: str,
        environment_id: str,
        revision_id: str,
        *,
        allow_superseded: bool = False,
    ) -> dict[str, Any]:
        revision = self.get_revision(org_id, environment_id, revision_id)
        allowed = {"approved", "superseded"} if allow_superseded else {"approved"}
        if revision["status"] == "active":
            return revision
        if revision["status"] not in allowed:
            raise ValueError(f"revision {revision_id} cannot become active from {revision['status']}")
        now = utc_now()
        with self.store._connect() as conn:
            head = conn.execute(
                "SELECT active_revision_id FROM network_model_heads WHERE org_id = ? AND environment_id = ?",
                (org_id, environment_id),
            ).fetchone()
            prior = str(head["active_revision_id"]) if head else ""
            if prior and prior != revision_id:
                conn.execute(
                    "UPDATE network_model_revisions SET status = 'superseded', updated_at = ? "
                    "WHERE org_id = ? AND environment_id = ? AND revision_id = ?",
                    (now, org_id, environment_id, prior),
                )
            conn.execute(
                "UPDATE network_model_revisions SET status = 'active', updated_at = ? "
                "WHERE org_id = ? AND environment_id = ? AND revision_id = ?",
                (now, org_id, environment_id, revision_id),
            )
            conn.execute(
                """
                INSERT INTO network_model_heads (org_id, environment_id, active_revision_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (org_id, environment_id)
                DO UPDATE SET active_revision_id = ?, updated_at = ?
                """,
                (org_id, environment_id, revision_id, now, revision_id, now),
            )
        return self.get_revision(org_id, environment_id, revision_id)

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

    def ensure_materialized_entities(
        self,
        org_id: str,
        environment_id: str,
        revision_id: str,
    ) -> int:
        """Idempotently rebuild derived indexes for revisions created by older code."""
        revision = self.get_revision(org_id, environment_id, revision_id)
        count = 0
        with self.store._connect() as conn:
            for entity_type, entity_id, site, device_id, record in _entity_rows(revision["model"]):
                conn.execute(
                    """
                    INSERT INTO network_model_entities
                    (org_id, environment_id, revision_id, entity_type, entity_id, site, device_id, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (org_id, environment_id, revision_id, entity_type, entity_id)
                    DO UPDATE SET site = ?, device_id = ?, data_json = ?
                    """,
                    (
                        org_id, environment_id, revision_id, entity_type, entity_id, site, device_id,
                        _json(record), site, device_id, _json(record),
                    ),
                )
                count += 1
        return count

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

    @staticmethod
    def _conflict(row: Any) -> dict[str, Any]:
        return {
            "conflict_id": row["conflict_id"],
            "domain": row["domain"],
            "subject_id": row["subject_id"],
            "status": row["status"],
            "severity": row["severity"],
            "details": _decode_json(row["details_json"], {}),
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolved_by": row["resolved_by"],
        }
