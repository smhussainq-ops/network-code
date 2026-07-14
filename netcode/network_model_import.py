"""Identity-safe importers for the Rezonance operational network model."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from netcode.network_model import NETWORK_MODEL_SCHEMA, NetworkModelError, validate_model_revision
from netcode.network_model_store import NetworkModelRepository
from netcode.paths import WorkspacePaths
from netcode.source_of_truth import LocalSourceOfTruth
from netcode.store import PlatformStore


_PUBLIC_DEVICE_FIELDS = {
    "hostname",
    "host",
    "port",
    "platform",
    "site",
    "role",
    "groups",
    "management",
    "runner_id",
    "runner_pool",
    "source",
    "updated_at",
    "building",
    "floor",
    "closet",
    "location",
}


def _slug(value: Any, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.:-]+", "-", str(value or "").strip().lower()).strip("-._:")
    return normalized[:128] or fallback


def _public_devices(devices: list[Mapping[str, Any]], *, source_name: str) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    hosts: dict[str, str] = {}
    for raw in devices:
        raw_id = raw.get("canonical_id") or raw.get("id") or raw.get("hostname")
        canonical_id = PlatformStore.normalize_device_identifier(str(raw_id or ""))
        if not canonical_id:
            raise NetworkModelError("every imported device requires a stable id or hostname")
        if canonical_id in normalized:
            raise NetworkModelError(f"duplicate imported device id {canonical_id}")
        host = str(raw.get("host") or "").strip().lower()
        if host and host in hosts and hosts[host] != canonical_id:
            raise NetworkModelError(
                f"management identity {host} is claimed by both {hosts[host]} and {canonical_id}"
            )
        if host:
            hosts[host] = canonical_id
        record = {
            key: copy.deepcopy(raw[key])
            for key in _PUBLIC_DEVICE_FIELDS
            if key in raw and raw[key] not in (None, "")
        }
        record["source_bindings"] = [{"source": source_name, "external_id": str(raw_id)}]
        normalized[canonical_id] = record
    return normalized


def _sites_from_devices(devices: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    sites: dict[str, dict[str, Any]] = {}
    for device_id, device in devices.items():
        site = str(device.get("site") or "").strip()
        if not site:
            continue
        sites.setdefault(site, {"devices": {}, "source": "imported_proposal"})
        sites[site]["devices"][device_id] = {
            "role": str(device.get("role") or "").strip(),
            "platform": str(device.get("platform") or "").strip(),
            **{
                key: copy.deepcopy(device[key])
                for key in ("building", "floor", "closet", "location", "groups")
                if device.get(key) not in (None, "", [], {})
            },
        }
    return sites


def _same_revision(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    fields = (
        "schema",
        "org_id",
        "environment_id",
        "revision_id",
        "parent_revision_id",
        "status",
        "source",
        "coverage",
        "authority_bindings",
        "approval",
        "model",
    )
    return all(left.get(field) == right.get(field) for field in fields)


def persist_import(
    repository: NetworkModelRepository,
    document: Mapping[str, Any],
    *,
    created_by: str,
) -> dict[str, Any]:
    normalized = validate_model_revision(document)
    try:
        existing = repository.get_revision(
            normalized["org_id"], normalized["environment_id"], normalized["revision_id"]
        )
    except KeyError:
        return {"created": True, "revision": repository.create_revision(normalized, created_by=created_by)}
    if not _same_revision(existing, normalized):
        raise NetworkModelError(
            f"revision {normalized['revision_id']} already exists with different content; use a new revision id"
        )
    return {"created": False, "revision": existing}


def import_catalog_candidate(
    store: PlatformStore,
    *,
    org_id: str,
    environment_id: str,
    revision_id: str,
    created_by: str,
) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    cursor = ""
    while True:
        page = store.query_devices(org_id, cursor=cursor, limit=50)
        devices.extend(page["devices"])
        cursor = str(page.get("next_cursor") or "")
        if not cursor:
            break
    repository = NetworkModelRepository(store)
    management_claims: dict[str, list[str]] = {}
    normalized_devices: list[dict[str, Any]] = []
    for raw in devices:
        record = copy.deepcopy(dict(raw))
        raw_id = record.get("canonical_id") or record.get("id") or record.get("hostname")
        canonical_id = PlatformStore.normalize_device_identifier(str(raw_id or ""))
        host = str(record.get("host") or "").strip().lower()
        if host and canonical_id:
            management_claims.setdefault(host, []).append(canonical_id)
        normalized_devices.append(record)

    ambiguous_hosts = {
        host: sorted(set(claims))
        for host, claims in management_claims.items()
        if len(set(claims)) > 1
    }
    for host, claims in ambiguous_hosts.items():
        digest = hashlib.sha256(f"identity:{host}:{','.join(claims)}".encode()).hexdigest()[:16]
        repository.record_conflict(
            org_id=org_id,
            environment_id=_slug(environment_id, "default"),
            conflict_id=f"identity-{digest}",
            domain="identity",
            subject_id=host,
            severity="high",
            details={
                "kind": "management_identity_collision",
                "revision_id": _slug(revision_id, "catalog-import"),
                "management_identity": host,
                "claimants": claims,
                "required_action": "Choose the canonical device or correct the Local Connector inventory.",
            },
        )
    for record in normalized_devices:
        host = str(record.get("host") or "").strip().lower()
        if host in ambiguous_hosts:
            record.pop("host", None)
            record.pop("management", None)

    public = _public_devices(normalized_devices, source_name="rezonance_catalog")
    if not public:
        raise NetworkModelError("the device catalog is empty; discover or import devices first")
    for device_id, device in public.items():
        if str(device.get("site") or "").strip():
            continue
        digest = hashlib.sha256(f"sites:{device_id}".encode()).hexdigest()[:16]
        repository.record_conflict(
            org_id=org_id,
            environment_id=_slug(environment_id, "default"),
            conflict_id=f"site-{digest}",
            domain="sites",
            subject_id=device_id,
            severity="medium",
            details={
                "kind": "site_assignment_missing",
                "revision_id": _slug(revision_id, "catalog-import"),
                "device_id": device_id,
                "required_action": "Assign the device to a site, then build a new proposal.",
            },
        )
    document = {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": org_id,
        "environment_id": _slug(environment_id, "default"),
        "revision_id": _slug(revision_id, "catalog-import"),
        "status": "proposed",
        "source": {"type": "discovery", "reference": f"device-catalog:{revision_id}"},
        "coverage": {"domains": ["identity", "sites"]},
        "authority_bindings": {
            "identity": {"source": "discovery", "mode": "propose"},
            "sites": {"source": "discovery", "mode": "propose"},
        },
        "model": {"devices": public, "sites": _sites_from_devices(public)},
    }
    result = persist_import(repository, document, created_by=created_by)
    result["conflicts"] = len(ambiguous_hosts) + sum(
        1 for device in public.values() if not str(device.get("site") or "").strip()
    )
    return result


def import_local_yaml_candidate(
    paths: WorkspacePaths,
    *,
    org_id: str,
    environment_id: str,
    revision_id: str,
    created_by: str,
) -> dict[str, Any]:
    snapshot = LocalSourceOfTruth(paths).snapshot()
    public = _public_devices(snapshot.get("devices") or [], source_name="local_yaml")
    if not public:
        raise NetworkModelError("local YAML inventory contains no devices")
    model: dict[str, Any] = {"devices": public, "sites": _sites_from_devices(public)}
    known_subnets = snapshot.get("known_subnets")
    coverage = ["identity", "sites"]
    bindings = {
        "identity": {"source": "local_yaml", "mode": "propose"},
        "sites": {"source": "local_yaml", "mode": "propose"},
    }
    if known_subnets:
        model["address_plan"] = copy.deepcopy(known_subnets)
        coverage.append("address_plan")
        bindings["address_plan"] = {"source": "local_yaml", "mode": "propose"}
    document = {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": org_id,
        "environment_id": _slug(environment_id, "default"),
        "revision_id": _slug(revision_id, "yaml-import"),
        "status": "proposed",
        "source": {
            "type": "local_yaml",
            "reference": f"inventory:{Path(str(snapshot['files']['inventory'])).name}",
        },
        "coverage": {"domains": coverage},
        "authority_bindings": bindings,
        "model": model,
    }
    return persist_import(
        NetworkModelRepository(PlatformStore(paths)), document, created_by=created_by
    )


def import_approved_network_design(
    repository: NetworkModelRepository,
    design: Mapping[str, Any],
    *,
    org_id: str,
    environment_id: str,
    created_by: str,
) -> dict[str, Any]:
    if str(design.get("schema") or "") != "rez.network-design.v1":
        raise NetworkModelError("approved design schema must be rez.network-design.v1")
    approval = design.get("approval") if isinstance(design.get("approval"), Mapping) else {}
    if str(approval.get("status") or "").strip().lower() != "approved":
        raise NetworkModelError("network design import requires explicit approved status")
    source = design.get("source") if isinstance(design.get("source"), Mapping) else {}
    if not str(source.get("type") or "").strip() or not str(source.get("reference") or "").strip():
        raise NetworkModelError("network design import requires exact source.type and source.reference")
    coverage = design.get("coverage") if isinstance(design.get("coverage"), Mapping) else {}
    domains = [str(item).strip().lower() for item in coverage.get("domains") or [] if str(item).strip()]
    if not domains:
        raise NetworkModelError("network design import requires declared coverage domains")
    source_type = _slug(source.get("type"), "manual_review")
    model = {
        key: copy.deepcopy(value)
        for key, value in design.items()
        if key not in {"schema", "namespace", "revision", "source", "approval", "coverage", "_loaded_from"}
    }
    document = {
        "schema": NETWORK_MODEL_SCHEMA,
        "org_id": org_id,
        "environment_id": _slug(environment_id or design.get("namespace"), "default"),
        "revision_id": _slug(design.get("revision"), "design-import"),
        "status": "approved",
        "source": {
            "type": source_type,
            "reference": str(source.get("reference")),
        },
        "coverage": {"domains": domains},
        "authority_bindings": {
            domain: {"source": source_type, "mode": "authoritative"} for domain in domains
        },
        "approval": copy.deepcopy(dict(approval)),
        "model": model,
    }
    return persist_import(repository, document, created_by=created_by)
