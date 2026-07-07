"""Ansible workflow-pack planning.

This module deliberately does not execute Ansible. The SaaS/control plane can
inspect and gate an Ansible pack, but device-touching execution belongs on the
local runner with runner-local inventory and credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


_HIGH_RISK_MODULES = {
    "ansible.builtin.command",
    "ansible.builtin.expect",
    "ansible.builtin.raw",
    "ansible.builtin.script",
    "ansible.builtin.shell",
    "ansible.windows.win_command",
    "ansible.windows.win_shell",
    "command",
    "expect",
    "raw",
    "script",
    "shell",
    "win_command",
    "win_shell",
}

_NETWORK_CONFIG_MODULES = {
    "arista.eos.eos_config",
    "cisco.ios.ios_config",
    "cisco.nxos.nxos_config",
    "eos_config",
    "fortinet.fortios.fortios_configuration_fact",
    "fortinet.fortios.fortios_firewall_policy",
    "ios_config",
    "junipernetworks.junos.junos_config",
    "junos_config",
    "nxos_config",
}

_TASK_KEYWORDS = {
    "action",
    "always",
    "args",
    "become",
    "block",
    "changed_when",
    "check_mode",
    "collections",
    "delegate_to",
    "environment",
    "failed_when",
    "ignore_errors",
    "loop",
    "name",
    "notify",
    "register",
    "rescue",
    "tags",
    "vars",
    "when",
    "with_items",
}


def _resolve_workspace_path(workspace_root: Path, candidate: str) -> Path:
    root = workspace_root.resolve()
    if not candidate:
        raise ValueError("playbook_path is required")
    path = Path(candidate)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("playbook_path must stay inside the Netcode workspace")
    if resolved.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("playbook_path must be a YAML playbook")
    if not resolved.exists():
        raise FileNotFoundError(f"playbook not found: {candidate}")
    return resolved


def _module_from_task(task: dict[str, Any]) -> str:
    if "action" in task:
        action = task["action"]
        if isinstance(action, str):
            return action.split()[0]
        if isinstance(action, dict) and action:
            return str(next(iter(action)))
    for key in task:
        if key not in _TASK_KEYWORDS:
            return str(key)
    return ""


def _walk_tasks(items: Any) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return tasks
    for item in items:
        if not isinstance(item, dict):
            continue
        tasks.append(item)
        for nested_key in ("block", "rescue", "always"):
            tasks.extend(_walk_tasks(item.get(nested_key)))
    return tasks


def audit_ansible_playbook(playbook_path: Path) -> dict[str, Any]:
    data = yaml.safe_load(playbook_path.read_text(encoding="utf-8"))
    plays = data if isinstance(data, list) else []
    tasks: list[dict[str, Any]] = []
    for play in plays:
        if isinstance(play, dict):
            tasks.extend(_walk_tasks(play.get("tasks")))

    modules = sorted({module for task in tasks if (module := _module_from_task(task))})
    high_risk = sorted(module for module in modules if module in _HIGH_RISK_MODULES)
    network_config = sorted(module for module in modules if module in _NETWORK_CONFIG_MODULES or module.endswith("_config"))

    findings: list[str] = []
    if high_risk:
        findings.append("high_risk_modules_require_peer_review")
    if network_config:
        findings.append("network_config_modules_require_canary_and_rollback")
    if not tasks:
        findings.append("no_tasks_found")

    return {
        "ok": bool(tasks),
        "path": str(playbook_path),
        "task_count": len(tasks),
        "modules": modules,
        "high_risk_modules": high_risk,
        "network_config_modules": network_config,
        "findings": findings,
    }


def build_ansible_pack_plan(
    workspace_root: Path,
    *,
    playbook_path: str,
    rollback_playbook_path: str = "",
    targets: list[str] | None = None,
    mode: str = "check",
    requested_by: str = "operator",
) -> dict[str, Any]:
    mode = mode.lower().strip() or "check"
    if mode not in {"check", "canary", "apply"}:
        raise ValueError("mode must be one of: check, canary, apply")

    playbook = _resolve_workspace_path(workspace_root, playbook_path)
    audit = audit_ansible_playbook(playbook)
    rollback_audit: dict[str, Any] | None = None
    if rollback_playbook_path:
        rollback = _resolve_workspace_path(workspace_root, rollback_playbook_path)
        rollback_audit = audit_ansible_playbook(rollback)

    blockers: list[str] = []
    if not audit["ok"]:
        blockers.append("playbook_has_no_tasks")
    if mode in {"canary", "apply"} and not rollback_audit:
        blockers.append("rollback_playbook_required")

    gates = ["runner_local_inventory", "check_mode", "policy_review"]
    if audit["high_risk_modules"]:
        gates.append("peer_review_high_risk_modules")
    if audit["network_config_modules"]:
        gates.extend(["canary", "rollback"])
    if mode == "apply":
        gates.append("approval_required")

    status = "blocked" if blockers else ("review_required" if audit["high_risk_modules"] else "ready")
    command_preview = [
        "ansible-playbook --check --diff <playbook> --inventory <runner-local-inventory>",
    ]
    if mode in {"canary", "apply"}:
        command_preview.append("ansible-playbook <playbook> --limit <approved-canary-or-batch>")

    return {
        "ok": not blockers,
        "status": status,
        "backend": "ansible",
        "requested_by": requested_by,
        "mode": mode,
        "targets": targets or [],
        "playbook": audit,
        "rollback_playbook": rollback_audit,
        "blockers": blockers,
        "required_gates": list(dict.fromkeys(gates)),
        "execution": {
            "location": "runner",
            "runner_local_inventory": True,
            "cloud_device_access": False,
            "credentials_leave_runner": False,
            "check_mode_first": True,
        },
        "command_preview": command_preview,
    }
