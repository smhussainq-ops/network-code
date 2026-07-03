"""GitOps artifact planning.

This module is intentionally local-first. It does not push branches or open PRs;
it produces the deterministic branch, commit, file, and review plan needed for
promotion into a real Git service later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netcode.gitflow import git_evidence
from netcode.paths import WorkspacePaths


def gitops_plan(paths: WorkspacePaths, intent_path: Path) -> dict[str, Any]:
    evidence = git_evidence(paths.root, intent_path)
    stem = intent_path.stem
    branch = f"change/{stem}"
    rel_intent = str(intent_path.relative_to(paths.root)) if intent_path.is_absolute() and intent_path.is_relative_to(paths.root) else str(intent_path)
    repo_ready = bool(evidence.get("available"))
    setup_commands = ["git status"] if repo_ready else ["git init", "git status"]
    artifacts = [
        rel_intent,
        f"rendered/{stem.replace('-add-vlan-', '_add_vlan-vlan-')}.eos",
        f"reports/{stem.replace('-add-vlan-', '_add_vlan-vlan-')}.md",
        f"reports/{stem.replace('-add-vlan-', '_add_vlan-vlan-')}.json",
    ]
    return {
        "ok": True,
        "workspace": str(paths.root),
        "git_available": evidence.get("available"),
        "repository_setup": {
            "ready": repo_ready,
            "message": "This folder is already a Git repository." if repo_ready else "This folder is not a Git repository yet. Run git init once before using branch and commit review.",
            "commands": setup_commands,
        },
        "branch": evidence.get("branch") or branch,
        "suggested_branch": branch,
        "commit_message": f"Netcode change {stem}",
        "pull_request": {
            "title": f"Network change: {stem}",
            "body_sections": [
                "Intent summary",
                "Source-of-truth evidence",
                "Rendered candidate config",
                "Policy validation",
                "Lab dry-run evidence",
                "Rollback plan",
            ],
            "required_review_evidence": ["validation_passed", "dry_run_passed", "rollback_available"],
        },
        "artifacts": artifacts,
        "evidence": evidence,
    }
