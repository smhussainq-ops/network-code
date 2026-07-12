"""Git evidence helpers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


_SAFE_PATH_PART = re.compile(r"[^A-Za-z0-9._-]+")
_CHANGE_HISTORY_GITIGNORE = """# Netcode change-history repository
# Only reviewed change records and their copied artifacts belong here.
*
!.gitignore
!README.md
!changes/
!changes/**
"""


def _safe_path_part(value: object, fallback: str) -> str:
    cleaned = _SAFE_PATH_PART.sub("-", str(value or "").strip()).strip("-.")
    return cleaned or fallback


def materialize_change_artifacts(
    git_root: Path,
    *,
    workspace_root: Path,
    change_id: str,
    record: dict[str, Any],
) -> dict[str, object]:
    """Copy one change's durable artifacts into the isolated history repo."""
    safe_change_id = _safe_path_part(change_id, "unknown-change")
    change_dir = git_root / "changes" / safe_change_id
    change_dir.mkdir(parents=True, exist_ok=True)
    workspace = workspace_root.resolve()
    copied: list[str] = []
    for item in record.get("manifest") or []:
        if not isinstance(item, dict) or not item.get("exists"):
            continue
        source = Path(str(item.get("path") or "")).resolve()
        if source != workspace and workspace not in source.parents:
            continue
        artifact_name = _safe_path_part(item.get("artifact"), source.name)
        suffix = source.suffix if not artifact_name.endswith(source.suffix) else ""
        destination = change_dir / f"{artifact_name}{suffix}"
        shutil.copy2(source, destination)
        copied.append(str(destination.relative_to(git_root)))

    audit_record = {
        "schema": "netcode.change-history.v1",
        "change_id": record.get("change_id") or change_id,
        "rez_change_id": record.get("rez_change_id"),
        "workflow_state": record.get("workflow_state"),
        "status": record.get("status"),
        "request": record.get("request") or {},
        "plan": record.get("plan") or {},
        "safety": record.get("safety") or {},
        "lab_proof": record.get("lab_proof") or {},
        "apply_proof": record.get("apply_proof") or {},
        "verify_proof": record.get("verify_proof") or {},
        "rollback_record": record.get("rollback_record") or {},
    }
    record_path = change_dir / "change-record.json"
    record_path.write_text(
        json.dumps(audit_record, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    copied.append(str(record_path.relative_to(git_root)))
    return {
        "ok": True,
        "change_id": change_id,
        "change_directory": str(change_dir),
        "paths": sorted(set(copied)),
    }


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
    upstream_probe = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    upstream = upstream_probe.stdout.strip() if upstream_probe.returncode == 0 else None
    ahead = None
    if upstream:
        ahead_raw = _run_git(root, ["rev-list", "--count", "@{upstream}..HEAD"])
        ahead = int(ahead_raw) if ahead_raw.isdigit() else None
    return {
        "ok": True,
        "available": True,
        "workspace": str(root),
        "message": "This workspace is already a Git repository.",
        "branch": branch,
        "remote": "" if "No such remote" in remote else remote,
        "status_short": status,
        "dirty": bool(status.strip()),
        "upstream": upstream,
        "ahead": ahead,
        "commands": ["git status", "git add <artifacts>", "git commit -m \"Describe network change\"", "git push"],
    }


def _inside_work_tree(root: Path) -> bool:
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return inside.returncode == 0 and inside.stdout.strip() == "true"


def list_git_branches(root: Path) -> dict[str, object]:
    """Return local branches and the current branch for workspace screens."""
    if not _inside_work_tree(root):
        return {
            "ok": True,
            "available": False,
            "current": None,
            "branches": [],
            "message": "This workspace is not a Git repository yet. Connect Git first.",
        }
    current = _run_git(root, ["branch", "--show-current"])
    raw = _run_git(root, ["for-each-ref", "refs/heads", "--format=%(refname:short)"])
    branches = [line.strip() for line in raw.splitlines() if line.strip()]
    if current and current not in branches:
        branches.insert(0, current)  # unborn branch (no commits yet) still counts as the working branch
    return {
        "ok": True,
        "available": True,
        "current": current or None,
        "branches": branches,
        "message": f"{len(branches)} local branch{'es' if len(branches) != 1 else ''}. Currently on {current or 'no branch'}.",
    }


def create_change_branch(root: Path, name: str, base: str = "") -> dict[str, object]:
    """Create or switch to a change branch so each network change is reviewable on its own branch."""
    name = name.strip()
    base = base.strip()
    if not _inside_work_tree(root):
        return {
            "ok": False,
            "action": "blocked",
            "branch": name,
            "base": base,
            "message": "This workspace is not a Git repository yet. Connect Git before creating a change branch.",
            "steps": [],
            "current": None,
            "branches": [],
        }
    if not name:
        return {
            "ok": False,
            "action": "blocked",
            "branch": "",
            "base": base,
            "message": "Provide a branch name such as change/store-1842-add-vlan-90.",
            "steps": [],
            "current": _run_git(root, ["branch", "--show-current"]) or None,
            "branches": list_git_branches(root).get("branches", []),
        }

    steps: list[dict[str, Any]] = []
    check = _run_git_step(root, ["check-ref-format", "--branch", name])
    steps.append(check)
    if not check["ok"]:
        return {
            "ok": False,
            "action": "blocked",
            "branch": name,
            "base": base,
            "message": f"'{name}' is not a valid Git branch name.",
            "steps": steps,
            "current": _run_git(root, ["branch", "--show-current"]) or None,
            "branches": list_git_branches(root).get("branches", []),
        }

    exists = _run_git_step(root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"])
    if exists["ok"]:
        action = "switched"
        steps.append(_run_git_step(root, ["checkout", name]))
    else:
        action = "created"
        checkout_args = ["checkout", "-b", name]
        if base:
            checkout_args.append(base)
        steps.append(_run_git_step(root, checkout_args))

    ok = all(step["ok"] for step in steps if step["command"].startswith("git checkout"))
    listing = list_git_branches(root)
    if ok:
        message = (
            f"Created change branch {name} and switched to it."
            if action == "created"
            else f"Switched to existing branch {name}."
        )
    else:
        failed = next((step for step in steps if not step["ok"] and step["command"].startswith("git checkout")), None)
        detail = (failed or {}).get("stderr") or "Git command failed."
        # Translate the most common raw-git failure into plain language for non-programmers.
        if "would be overwritten by checkout" in detail or "commit your changes or stash" in detail:
            message = (
                f"Can't switch to {name}: this workspace has uncommitted changes that conflict with it. "
                "Commit or discard the current changes first, then try again."
            )
        else:
            message = f"Could not {'create' if action == 'created' else 'switch to'} branch {name}: {detail}"
        action = "failed"
    return {
        "ok": ok,
        "action": action,
        "branch": name,
        "base": base,
        "message": message,
        "steps": steps,
        "current": listing.get("current"),
        "branches": listing.get("branches", []),
    }


_COMMIT_IDENTITY = [
    "-c", "user.email=netcode-platform@local",
    "-c", "user.name=Netcode Platform",
    "-c", "commit.gpgsign=false",
]


def commit_change_artifacts(root: Path, message: str, add_paths: list[str] | None = None) -> dict[str, object]:
    """Stage and commit change artifacts on the current branch, with an honest command transcript."""
    message = message.strip() or "Netcode network change"
    if not _inside_work_tree(root):
        return {
            "ok": False,
            "action": "blocked",
            "message": "This workspace is not a Git repository yet. Connect Git before committing.",
            "steps": [],
            "commit": None,
            "branch": None,
        }
    if not add_paths:
        return {
            "ok": False,
            "action": "blocked",
            "message": "No reviewed change artifacts were supplied; refusing to stage the whole workspace.",
            "steps": [],
            "commit": None,
            "branch": _run_git(root, ["branch", "--show-current"]) or None,
        }
    branch = _run_git(root, ["branch", "--show-current"]) or None
    steps: list[dict[str, Any]] = []
    add_args = ["add", "--", *add_paths]
    steps.append(_run_git_step(root, add_args))
    staged_probe = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if staged_probe.returncode == 0:
        return {
            "ok": True,
            "action": "nothing_to_commit",
            "message": "Nothing new to commit. All artifacts are already committed.",
            "steps": steps,
            "commit": _run_git(root, ["rev-parse", "--short", "HEAD"]) or None,
            "branch": branch,
        }
    commit_step = _run_git_step(root, [*_COMMIT_IDENTITY, "commit", "-m", message])
    steps.append(commit_step)
    if not commit_step["ok"]:
        return {
            "ok": False,
            "action": "failed",
            "message": f"Commit failed: {commit_step['stderr'] or commit_step['stdout'] or 'unknown error'}",
            "steps": steps,
            "commit": None,
            "branch": branch,
        }
    commit_hash = _run_git(root, ["rev-parse", "--short", "HEAD"])
    return {
        "ok": True,
        "action": "committed",
        "message": f"Committed {commit_hash} on {branch or 'current branch'}: {message}",
        "steps": steps,
        "commit": commit_hash,
        "branch": branch,
    }


def push_current_branch(root: Path) -> dict[str, object]:
    """Push the current branch to origin, reporting the real result (including missing credentials)."""
    if not _inside_work_tree(root):
        return {
            "ok": False,
            "action": "blocked",
            "message": "This workspace is not a Git repository yet. Connect Git before pushing.",
            "steps": [],
            "branch": None,
        }
    branch = _run_git(root, ["branch", "--show-current"])
    if not branch:
        return {
            "ok": False,
            "action": "blocked",
            "message": "No branch is checked out, so there is nothing to push.",
            "steps": [],
            "branch": None,
        }
    remote = _run_git(root, ["remote", "get-url", "origin"])
    if not remote or "No such remote" in remote:
        return {
            "ok": False,
            "action": "blocked",
            "message": "No origin remote is configured. Connect Git with a repo URL first.",
            "steps": [],
            "branch": branch,
        }
    push_step = _run_git_step(root, ["push", "-u", "origin", branch])
    ok = push_step["ok"]
    if ok:
        message = f"Pushed {branch} to origin. The change is ready for review."
    else:
        detail = push_step["stderr"] or push_step["stdout"] or "unknown error"
        message = (
            f"Push failed: {detail}".strip()
            + f" If this runtime has no GitHub credentials, run from an authenticated terminal: git push -u origin {branch}"
        )
    return {
        "ok": ok,
        "action": "pushed" if ok else "failed",
        "message": message,
        "steps": [push_step],
        "branch": branch,
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
        current_branch = _run_git(root, ["branch", "--show-current"])
        if current_branch != branch:
            branch_probe = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            checkout_args = (
                ["checkout", branch]
                if branch_probe.returncode == 0
                else ["checkout", "-b", branch]
            )
            steps.append(_run_git_step(root, checkout_args))
    gitignore = root / ".gitignore"
    readme = root / "README.md"
    if not gitignore.exists():
        gitignore.write_text(_CHANGE_HISTORY_GITIGNORE, encoding="utf-8")
    if not readme.exists():
        readme.write_text(
            "# Netcode change history\n\nReviewed network intent, command, validation, and rollback artifacts.\n",
            encoding="utf-8",
        )
    steps.append(_run_git_step(root, ["add", "--", ".gitignore", "README.md"]))
    staged_probe = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if staged_probe.returncode != 0:
        steps.append(
            _run_git_step(
                root,
                [*_COMMIT_IDENTITY, "commit", "-m", "Initialize Netcode change history"],
            )
        )
    if repo_url:
        current_remote = _run_git(root, ["remote", "get-url", "origin"])
        if current_remote and "No such remote" not in current_remote:
            steps.append(_run_git_step(root, ["remote", "set-url", "origin", repo_url]))
        else:
            steps.append(_run_git_step(root, ["remote", "add", "origin", repo_url]))
    steps.append(_run_git_step(root, ["status", "--short"]))

    status = git_workspace_status(root)
    return {
        "ok": bool(status.get("available")) and all(
            step["ok"] for step in steps if step["command"] != "git status --short"
        ),
        "workspace": str(root),
        "repo_url": repo_url,
        "branch": branch,
        "steps": steps,
        "status": status,
    }
