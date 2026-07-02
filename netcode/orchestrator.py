"""Workflow orchestration."""

from __future__ import annotations

from pathlib import Path

from netcode.bootstrap import init_workspace
from netcode.gitflow import git_evidence
from netcode.models import PipelineArtifacts, PipelineResult, load_intent
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
    stem = f"{intent.site}-{intent.change_type}-vlan-{intent.vlan.id}"
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
