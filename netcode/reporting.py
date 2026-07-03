"""Report generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from netcode.models import EndToEndResult, PipelineResult
from netcode.paths import WorkspacePaths


def _fence(language: str, body: str) -> str:
    return f"```{language}\n{body.rstrip()}\n```"


def markdown_report(result: PipelineResult) -> str:
    validation_lines = []
    for check in result.validation.checks:
        marker = "PASS" if check.status == "pass" else "FAIL"
        validation_lines.append(f"- {marker}: {check.title} - {check.message}")

    commands = "\n".join(result.git.get("suggested_commands", []))
    git_diff = result.git.get("intent_diff") or "(No tracked diff yet. New files may be untracked.)"

    return "\n\n".join(
        [
            "# Netcode Change Report",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Verdict: {result.status.upper()}",
            "## Intent YAML",
            _fence("yaml", result.intent_yaml),
            "## Jinja Template",
            f"Template: `{result.render.template_path}`",
            "## Rendered Arista EOS Config",
            _fence("eos", result.render.config),
            "## Validation",
            "\n".join(validation_lines),
            "## Git Teaching View",
            _fence("bash", commands),
            "## Current Git Diff",
            _fence("diff", str(git_diff)),
        ]
    ) + "\n"


def write_reports(paths: WorkspacePaths, result: PipelineResult, stem: str) -> tuple[Path, Path]:
    paths.reports.mkdir(parents=True, exist_ok=True)
    md_path = paths.reports / f"{stem}.md"
    json_path = paths.reports / f"{stem}.json"
    md_path.write_text(markdown_report(result), encoding="utf-8")
    json_path.write_text(json.dumps(result.model_dump(), indent=2), encoding="utf-8")
    return md_path, json_path


def markdown_end_to_end_report(result: EndToEndResult) -> str:
    phase_lines = []
    for phase in result.phases:
        marker = phase.status.upper()
        phase_lines.append(f"- {marker}: {phase.title} - {phase.message}")

    lab_payload = json.dumps(result.lab, indent=2)
    base_report = markdown_report(result.pipeline)
    return "\n\n".join(
        [
            base_report.rstrip(),
            "## End-To-End Phases",
            "\n".join(phase_lines),
            "## Arista Lab Evidence",
            _fence("json", lab_payload),
        ]
    ) + "\n"


def write_end_to_end_reports(paths: WorkspacePaths, result: EndToEndResult, stem: str) -> tuple[Path, Path]:
    paths.reports.mkdir(parents=True, exist_ok=True)
    md_path = paths.reports / f"{stem}-e2e.md"
    json_path = paths.reports / f"{stem}-e2e.json"
    md_path.write_text(markdown_end_to_end_report(result), encoding="utf-8")
    json_path.write_text(json.dumps(result.model_dump(), indent=2), encoding="utf-8")
    return md_path, json_path
