"""Git evidence helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run_git(root: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return completed.stderr.strip()
    return completed.stdout.strip()


def git_evidence(root: Path, intent_path: Path) -> dict[str, object]:
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    rel_intent = str(intent_path.relative_to(root))
    suggested_branch = f"change/{intent_path.stem}"
    suggested_commands = [
        f"git checkout -b {suggested_branch}",
        f"git add {rel_intent}",
        f"git commit -m \"Add network intent {intent_path.stem}\"",
    ]
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {
            "available": False,
            "message": "Git evidence is unavailable because this workspace is not inside a Git repository.",
            "branch": None,
            "status_short": "",
            "intent_diff": "",
            "suggested_commands": suggested_commands,
        }

    branch = _run_git(root, ["branch", "--show-current"])
    status = _run_git(root, ["status", "--short"])
    diff = _run_git(root, ["diff", "--", str(intent_path.relative_to(root))])
    return {
        "available": True,
        "branch": branch,
        "status_short": status,
        "intent_diff": diff,
        "suggested_commands": suggested_commands,
    }


def git_workspace_status(root: Path) -> dict[str, object]:
    """Return Git repository status for workspace setup screens."""
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    setup_commands = ["git init", "git status"]
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {
            "ok": True,
            "available": False,
            "workspace": str(root),
            "message": "This workspace is not a Git repository yet.",
            "branch": None,
            "remote": "",
            "status_short": "",
            "commands": setup_commands,
        }

    branch = _run_git(root, ["branch", "--show-current"])
    remote = _run_git(root, ["remote", "get-url", "origin"])
    status = _run_git(root, ["status", "--short"])
    return {
        "ok": True,
        "available": True,
        "workspace": str(root),
        "message": "This workspace is already a Git repository.",
        "branch": branch,
        "remote": "" if "No such remote" in remote else remote,
        "status_short": status,
        "commands": ["git status", "git add <artifacts>", "git commit -m \"Describe network change\"", "git push"],
    }
