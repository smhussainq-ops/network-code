"""Git evidence helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


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


def _run_git_step(root: Path, args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "command": "git " + " ".join(args),
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "ok": completed.returncode == 0,
    }


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


def setup_git_workspace(root: Path, repo_url: str = "", branch: str = "main") -> dict[str, object]:
    """Initialize or connect the current workspace to the configured Git repo."""
    root.mkdir(parents=True, exist_ok=True)
    branch = branch.strip() or "main"
    repo_url = repo_url.strip()
    steps: list[dict[str, Any]] = []

    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        steps.append(_run_git_step(root, ["init", "-b", branch]))
    else:
        steps.append(_run_git_step(root, ["checkout", "-B", branch]))
    if repo_url:
        current_remote = _run_git(root, ["remote", "get-url", "origin"])
        if current_remote and "No such remote" not in current_remote:
            steps.append(_run_git_step(root, ["remote", "set-url", "origin", repo_url]))
        else:
            steps.append(_run_git_step(root, ["remote", "add", "origin", repo_url]))
    steps.append(_run_git_step(root, ["status", "--short"]))

    status = git_workspace_status(root)
    return {
        "ok": bool(status.get("available")) and all(step["ok"] for step in steps if step["command"] != "git status --short"),
        "workspace": str(root),
        "repo_url": repo_url,
        "branch": branch,
        "steps": steps,
        "status": status,
    }
