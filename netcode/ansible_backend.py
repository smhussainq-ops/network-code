"""Ansible workflow-pack planning.

This module deliberately does not execute Ansible. The SaaS/control plane can
inspect and gate an Ansible pack, but device-touching execution belongs on the
local runner with runner-local inventory and credentials.
"""

from __future__ import annotations

import hashlib
import re
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

_SENSITIVE_VARIABLES = {
    "ansible_password",
    "ansible_ssh_pass",
    "ansible_become_password",
    "password",
    "private_key",
    "private_key_file",
    "secret",
    "token",
}

_GUIDED_MODULES = {
    "arista_eos": {
        "show": "arista.eos.eos_command",
        "config": "arista.eos.eos_config",
    },
    "cisco_ios": {
        "show": "cisco.ios.ios_command",
        "config": "cisco.ios.ios_config",
    },
    "cisco_xe": {
        "show": "cisco.ios.ios_command",
        "config": "cisco.ios.ios_config",
    },
    "cisco_nxos": {
        "show": "cisco.nxos.nxos_command",
        "config": "cisco.nxos.nxos_config",
    },
    "juniper_junos": {
        "show": "junipernetworks.junos.junos_command",
        "config": "junipernetworks.junos.junos_config",
    },
}

_MAX_PLAYBOOK_BYTES = 256 * 1024


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


def _sensitive_paths(value: Any, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            path = f"{prefix}.{raw_key}" if prefix else str(raw_key)
            if key in _SENSITIVE_VARIABLES and nested not in (None, "", "vault", "prompt"):
                findings.append(path)
            findings.extend(_sensitive_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(_sensitive_paths(nested, f"{prefix}[{index}]"))
    return findings


def audit_ansible_playbook(playbook_path: Path) -> dict[str, Any]:
    content = playbook_path.read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > _MAX_PLAYBOOK_BYTES:
        raise ValueError("playbook exceeds the 256 KiB review limit")
    data = yaml.safe_load(content)
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
        findings.append("high_risk_modules_forbidden")
    if network_config:
        findings.append("network_config_modules_require_canary_and_rollback")
    if not tasks:
        findings.append("no_tasks_found")
    sensitive_paths = sorted(set(_sensitive_paths(data)))
    if sensitive_paths:
        findings.append("inline_credentials_forbidden")

    return {
        "ok": bool(tasks) and not sensitive_paths and not high_risk,
        "path": str(playbook_path),
        "task_count": len(tasks),
        "modules": modules,
        "high_risk_modules": high_risk,
        "network_config_modules": network_config,
        "sensitive_paths": sensitive_paths,
        "findings": findings,
    }


def package_ansible_playbooks(
    workspace_root: Path,
    *,
    playbook_path: str,
    rollback_playbook_path: str = "",
) -> dict[str, str]:
    """Package reviewed YAML for runner transport without any credentials."""
    playbook = _resolve_workspace_path(workspace_root, playbook_path)
    content = playbook.read_text(encoding="utf-8")
    bundle = {
        "playbook_name": playbook.name,
        "playbook_content": content,
        "playbook_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    if rollback_playbook_path:
        rollback = _resolve_workspace_path(workspace_root, rollback_playbook_path)
        rollback_content = rollback.read_text(encoding="utf-8")
        bundle.update(
            {
                "rollback_playbook_name": rollback.name,
                "rollback_playbook_content": rollback_content,
                "rollback_playbook_sha256": hashlib.sha256(
                    rollback_content.encode("utf-8")
                ).hexdigest(),
            }
        )
    return bundle


def build_guided_ansible_playbook(
    workspace_root: Path,
    *,
    name: str,
    platform: str,
    operation: str,
    commands: list[str],
    rollback_commands: list[str] | None = None,
) -> dict[str, Any]:
    """Create reviewed Ansible YAML from a form; no Python authoring required."""
    normalized_platform = platform.strip().lower()
    normalized_operation = operation.strip().lower()
    modules = _GUIDED_MODULES.get(normalized_platform)
    if not modules:
        raise ValueError(
            "guided Ansible supports arista_eos, cisco_ios/cisco_xe, cisco_nxos, and juniper_junos"
        )
    if normalized_operation not in {"show", "config"}:
        raise ValueError("operation must be show or config")
    clean_commands = [str(command).strip() for command in commands if str(command).strip()]
    if not clean_commands:
        raise ValueError("at least one command is required")
    rollback = [
        str(command).strip()
        for command in (rollback_commands or [])
        if str(command).strip()
    ]
    if normalized_operation == "config" and not rollback:
        raise ValueError("configuration playbooks require explicit rollback commands")

    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "guided-workflow"
    generated_dir = workspace_root.resolve() / ".netcode" / "generated-playbooks"
    generated_dir.mkdir(parents=True, exist_ok=True)
    module = modules[normalized_operation]
    task: dict[str, Any] = {
        "name": name.strip() or "Netcode guided workflow",
        module: {"commands" if normalized_operation == "show" else "lines": clean_commands},
    }
    if normalized_operation == "show":
        task["check_mode"] = False
    playbook_data = [
        {
            "name": name.strip() or "Netcode guided workflow",
            "hosts": "all",
            "gather_facts": False,
            "tasks": [task],
        }
    ]
    playbook = generated_dir / f"{slug}.yml"
    playbook.write_text(yaml.safe_dump(playbook_data, sort_keys=False), encoding="utf-8")

    rollback_path = ""
    if rollback:
        rollback_data = [
            {
                "name": f"Rollback {name.strip() or 'Netcode guided workflow'}",
                "hosts": "all",
                "gather_facts": False,
                "tasks": [
                    {
                        "name": "Apply reviewed rollback",
                        modules["config"]: {"lines": rollback},
                    }
                ],
            }
        ]
        rollback_file = generated_dir / f"{slug}-rollback.yml"
        rollback_file.write_text(
            yaml.safe_dump(rollback_data, sort_keys=False), encoding="utf-8"
        )
        rollback_path = str(rollback_file.relative_to(workspace_root.resolve()))

    relative_playbook = str(playbook.relative_to(workspace_root.resolve()))
    return {
        "ok": True,
        "name": name,
        "platform": normalized_platform,
        "operation": normalized_operation,
        "playbook_path": relative_playbook,
        "rollback_playbook_path": rollback_path,
        "playbook_yaml": playbook.read_text(encoding="utf-8"),
        "rollback_yaml": (
            (workspace_root.resolve() / rollback_path).read_text(encoding="utf-8")
            if rollback_path
            else ""
        ),
        "audit": audit_ansible_playbook(playbook),
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
    if not audit["task_count"]:
        blockers.append("playbook_has_no_tasks")
    if audit["sensitive_paths"]:
        blockers.append("inline_credentials_forbidden")
    if audit["high_risk_modules"]:
        blockers.append("high_risk_modules_forbidden")
    if mode in {"canary", "apply"} and not rollback_audit:
        blockers.append("rollback_playbook_required")

    gates = ["runner_local_inventory", "check_mode", "policy_review"]
    if audit["network_config_modules"]:
        gates.extend(["canary", "rollback"])
    if mode == "apply":
        gates.append("approval_required")

    status = "blocked" if blockers else "ready"
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
