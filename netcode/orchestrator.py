"""Workflow orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netcode.bootstrap import init_workspace
from netcode.change_types import spec_for
from netcode.gitflow import git_evidence
from netcode.intent_utils import report_stem
from netcode.models import PipelineArtifacts, PipelineResult, load_intent, load_intent_data
from netcode.paths import WorkspacePaths
from netcode.rendering import render_intent, write_rendered_config
from netcode.reporting import write_reports
from netcode.validation import StaticValidator
from netcode.yamlio import dumps_yaml, read_yaml, write_yaml


def ensure_initialized(paths: WorkspacePaths) -> None:
    if not (paths.templates / "arista" / "add_vlan.j2").exists():
        init_workspace(paths)


def create_add_vlan_intent(
    paths: WorkspacePaths,
    site: str,
    device_id: str,
    vlan_id: int,
    name: str,
    subnet: str,
    purpose: str,
    pci_reachable: bool,
    requested_by: str,
) -> Path:
    ensure_initialized(paths)
    data = {
        "change_type": "add_vlan",
        "site": site,
        "targets": {"device_ids": [device_id], "device_group": "access-switches"},
        "vlan": {
            "id": vlan_id,
            "name": name,
            "subnet": subnet,
            "purpose": purpose,
            "svi": {"enabled": False},
        },
        "policy": {"pci_reachable": pci_reachable, "internet_reachable": True},
        "metadata": {"requested_by": requested_by, "learning_mode": True},
    }
    filename = f"{site}-add-vlan-{vlan_id}.yaml"
    path = paths.intents / site / filename
    write_yaml(path, data)
    return path


def create_desired_state_intent(
    paths: WorkspacePaths,
    change_type: str,
    site: str,
    device_id: str,
    requested_by: str,
    values: dict[str, Any],
) -> Path:
    ensure_initialized(paths)
    targets = {"device_ids": [device_id], "device_group": values.get("device_group", "access-switches")}
    metadata = {
        "requested_by": requested_by,
        "ticket_id": values.get("ticket_id") or None,
        "learning_mode": bool(values.get("learning_mode", True)),
    }
    common: dict[str, Any] = {
        "change_type": change_type,
        "site": site,
        "targets": targets,
        "policy": {
            "pci_reachable": bool(values.get("pci_reachable", False)),
            "internet_reachable": bool(values.get("internet_reachable", True)),
        },
        "metadata": metadata,
    }

    spec_for(change_type).build(common, values, device_id)

    validated = load_intent_from_data(common)
    filename = f"{report_stem(validated)}.yaml"
    path = paths.intents / site / filename
    write_yaml(path, common)
    return path


def load_intent_from_data(data: dict[str, Any]):
    return load_intent_data(data)


def run_static_pipeline(paths: WorkspacePaths, intent_path: Path) -> PipelineResult:
    ensure_initialized(paths)
    intent_path = intent_path.resolve()
    intent = load_intent(intent_path)
    render = render_intent(intent, paths)
    rendered_path = write_rendered_config(paths, intent, render)
    validation = StaticValidator(paths).validate(intent, render)
    intent_data = read_yaml(intent_path)
    partial = PipelineResult(
        status=validation.status,
        intent=intent_data,
        intent_yaml=dumps_yaml(intent_data),
        render=render,
        validation=validation,
        git=git_evidence(paths.root, intent_path),
        artifacts=None,
    )
    stem = report_stem(intent)
    md_path, json_path = write_reports(paths, partial, stem)
    result = partial.model_copy(
        update={
            "artifacts": PipelineArtifacts(
                intent_path=str(intent_path),
                rendered_path=str(rendered_path),
                report_markdown_path=str(md_path),
                report_json_path=str(json_path),
            )
        }
    )
    write_reports(paths, result, stem)
    return result
