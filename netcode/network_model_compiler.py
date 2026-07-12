"""Deterministic effective-intent compiler for the Rezonance Network Model."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from netcode.network_model import NetworkModelError


OPERATIONAL_MODEL_STATUSES = {"approved", "active"}


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = _dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else ([value] if value else [])
    return [str(item).strip() for item in values if str(item).strip()]


def _site_for_device(model: Mapping[str, Any], device_id: str) -> tuple[str, dict[str, Any]]:
    global_device = _dict(_dict(model.get("devices")).get(device_id))
    explicit_site = str(global_device.get("site") or "").strip()
    sites = _dict(model.get("sites"))
    nested_matches = [
        str(site_id)
        for site_id, raw_site in sites.items()
        if device_id in _dict(_dict(raw_site).get("devices"))
    ]
    assignments = sorted({*nested_matches, *([explicit_site] if explicit_site else [])})
    if len(assignments) > 1:
        raise NetworkModelError(
            f"device {device_id} is assigned to multiple approved sites: {', '.join(assignments)}"
        )
    if explicit_site and explicit_site not in sites:
        raise NetworkModelError(f"device {device_id} references unknown approved site {explicit_site}")
    return (assignments[0] if assignments else ""), global_device


def compile_effective_device(
    revision: Mapping[str, Any],
    device_id: str,
    *,
    required_domains: Sequence[str] = (),
    require_approved: bool = True,
) -> dict[str, Any]:
    status = str(revision.get("status") or "").strip().lower()
    if require_approved and status not in OPERATIONAL_MODEL_STATUSES:
        raise NetworkModelError("operational context requires an approved or active model revision")
    model = _dict(revision.get("model"))
    site_id, global_device = _site_for_device(model, device_id)
    sites = _dict(model.get("sites"))
    site = _dict(sites.get(site_id))
    site_device = _dict(_dict(site.get("devices")).get(device_id))
    if not global_device and not site_device:
        raise NetworkModelError(f"device {device_id} is not present in revision {revision.get('revision_id')}")

    role = str(global_device.get("role") or site_device.get("role") or "").strip()
    groups = sorted(
        {
            str(item).strip()
            for item in [*_strings(global_device.get("groups")), *_strings(site_device.get("groups"))]
            if str(item).strip()
        }
    )
    archetype_id = str(site.get("archetype") or "").strip()

    layers: list[tuple[str, dict[str, Any]]] = [
        ("organization_standard", _dict(model.get("organization_standard"))),
        (f"site_archetype:{archetype_id}", _dict(_dict(model.get("site_archetypes")).get(archetype_id))),
        (f"site:{site_id}", _dict(site.get("intent") or site.get("standards"))),
        (f"role:{role}", _dict(_dict(model.get("role_standards")).get(role))),
    ]
    for group in groups:
        layers.append((f"group:{group}", _dict(_dict(model.get("group_standards")).get(group))))
    layers.extend(
        [
            (f"site_device:{device_id}", _dict(site_device.get("intent") or site_device.get("overrides"))),
            (f"device:{device_id}", _dict(global_device.get("intent") or global_device.get("overrides"))),
        ]
    )

    effective: dict[str, Any] = {}
    applied_layers: list[str] = []
    for label, layer in layers:
        if not layer:
            continue
        effective = _deep_merge(effective, layer)
        applied_layers.append(label)

    coverage = {
        str(item).strip().lower()
        for item in _dict(revision.get("coverage")).get("domains", [])
        if str(item).strip()
    }
    required = {str(item).strip().lower() for item in required_domains if str(item).strip()}
    return {
        "schema": "rezonance.effective-network-model.v1",
        "org_id": revision.get("org_id"),
        "environment_id": revision.get("environment_id"),
        "revision_id": revision.get("revision_id"),
        "revision_status": status,
        "device_id": device_id,
        "site_id": site_id,
        "role": role,
        "groups": groups,
        "archetype": archetype_id,
        "coverage": sorted(coverage),
        "missing_coverage": sorted(required - coverage),
        "operationally_usable": status in OPERATIONAL_MODEL_STATUSES and not (required - coverage),
        "layers": applied_layers,
        "effective": effective,
    }


def compile_site_context(
    revision: Mapping[str, Any],
    site_id: str,
    *,
    required_domains: Sequence[str] = (),
    require_approved: bool = True,
) -> dict[str, Any]:
    status = str(revision.get("status") or "").strip().lower()
    if require_approved and status not in OPERATIONAL_MODEL_STATUSES:
        raise NetworkModelError("operational context requires an approved or active model revision")
    model = _dict(revision.get("model"))
    site = _dict(_dict(model.get("sites")).get(site_id))
    if not site:
        raise NetworkModelError(f"site {site_id} is not present in revision {revision.get('revision_id')}")
    devices = sorted(_dict(site.get("devices")))
    for device_id, raw_device in _dict(model.get("devices")).items():
        if str(_dict(raw_device).get("site") or "") == site_id and device_id not in devices:
            devices.append(str(device_id))
    coverage = {
        str(item).strip().lower()
        for item in _dict(revision.get("coverage")).get("domains", [])
        if str(item).strip()
    }
    required = {str(item).strip().lower() for item in required_domains if str(item).strip()}
    return {
        "schema": "rezonance.site-network-context.v1",
        "org_id": revision.get("org_id"),
        "environment_id": revision.get("environment_id"),
        "revision_id": revision.get("revision_id"),
        "revision_status": status,
        "site_id": site_id,
        "archetype": str(site.get("archetype") or ""),
        "coverage": sorted(coverage),
        "missing_coverage": sorted(required - coverage),
        "operationally_usable": status in OPERATIONAL_MODEL_STATUSES and not (required - coverage),
        "site": site,
        "devices": [
            compile_effective_device(
                revision,
                device_id,
                required_domains=required_domains,
                require_approved=require_approved,
            )
            for device_id in sorted(devices)
        ],
    }


def to_rez_network_design(revision: Mapping[str, Any]) -> dict[str, Any]:
    """Export approved model data using Rez's deterministic design-context contract."""
    status = str(revision.get("status") or "").strip().lower()
    if status not in OPERATIONAL_MODEL_STATUSES:
        raise NetworkModelError("Rez design export requires an approved or active model revision")
    model = _dict(revision.get("model"))
    sites = _dict(model.get("sites"))
    if not sites:
        raise NetworkModelError("Rez design export requires modeled sites")

    global_devices = _dict(model.get("devices"))
    for device_id, raw_device in global_devices.items():
        device = _dict(raw_device)
        site_id = str(device.get("site") or "").strip()
        if not site_id or site_id not in sites:
            continue
        site = _dict(sites[site_id])
        site_devices = _dict(site.get("devices"))
        public_device = {
            key: copy.deepcopy(device[key])
            for key in ("role", "platform", "transport")
            if device.get(key) not in (None, "")
        }
        site_devices[device_id] = _deep_merge(public_device, _dict(site_devices.get(device_id)))
        site["devices"] = site_devices
        sites[site_id] = site

    internal = {
        "organization_standard",
        "site_archetypes",
        "role_standards",
        "group_standards",
        "devices",
    }
    design = {
        "schema": "rez.network-design.v1",
        "namespace": revision.get("environment_id"),
        "revision": revision.get("revision_id"),
        "source": {
            "type": "rezonance_model",
            "reference": f"network-model:{revision.get('environment_id')}:{revision.get('revision_id')}",
        },
        "approval": copy.deepcopy(revision.get("approval")),
        "coverage": copy.deepcopy(revision.get("coverage")),
        **{key: copy.deepcopy(value) for key, value in model.items() if key not in internal and key != "sites"},
        "sites": sites,
    }
    return design
