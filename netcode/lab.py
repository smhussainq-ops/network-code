"""Arista EOS lab adapter."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from difflib import unified_diff
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from netcode.adapters.execution import ExecutionAdapter, ExecutionAdapterMetadata
from netcode.adapters.registry import AdapterRegistry
from netcode.change_types import spec_for
from netcode.inventory import Device, Inventory
from netcode.intent_utils import lab_write_supported, report_stem, rollback_config
from netcode.models import AclRuleIntent, AddVlanIntent, BgpNeighborIntent, CustomConfigIntent, EndToEndArtifacts, EndToEndResult, Intent, InterfaceConfigIntent, NtpStandardizeIntent, OsUpgradeIntent, PhaseResult, load_intent
from netcode.paths import WorkspacePaths
from netcode.rendering import render_intent
from netcode.reporting import write_end_to_end_reports
from netcode.orchestrator import run_static_pipeline
from netcode.ui_config import configured_inventory_path
from netcode.validation import StaticValidator
from netcode.verification import verify_vlan_state


@dataclass
class LabResult:
    status: Literal["pass", "fail"]
    action: str
    device_id: str
    message: str
    session_name: str = ""
    evidence: dict[str, object] = field(default_factory=dict)
    dry_run_kind: str = ""


DRY_RUN_CAPABILITIES: dict[str, dict[str, str]] = {
    "arista_eos": {
        "tier": "native",
        "dry_run_kind": "native_session",
        "mechanism": "EOS configure session + show session-config diffs + abort",
    },
    "cisco_ios": {
        "tier": "offline",
        "dry_run_kind": "offline_validation",
        "mechanism": "read running-config + static validation + generated diff; canary before wider rollout",
    },
    "cisco_xe": {
        "tier": "offline",
        "dry_run_kind": "offline_validation",
        "mechanism": "read running-config + static validation + generated diff; canary before wider rollout",
    },
    "cisco_nxos": {
        "tier": "planned_native",
        "dry_run_kind": "canary_only",
        "mechanism": "native session support is planned; use canary verification until implemented",
    },
    "cisco_xr": {
        "tier": "planned_native",
        "dry_run_kind": "canary_only",
        "mechanism": "commit check support is planned; use canary verification until implemented",
    },
    "juniper_junos": {
        "tier": "planned_native",
        "dry_run_kind": "canary_only",
        "mechanism": "candidate commit-check support is planned; use canary verification until implemented",
    },
}


def normalize_platform(platform: str) -> str:
    normalized = str(platform or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "arista": "arista_eos",
        "eos": "arista_eos",
        "arista_eos": "arista_eos",
        "ios": "cisco_ios",
        "cisco_ios": "cisco_ios",
        "iosxe": "cisco_xe",
        "cisco_iosxe": "cisco_xe",
        "cisco_xe": "cisco_xe",
        "nxos": "cisco_nxos",
        "cisco_nxos": "cisco_nxos",
        "iosxr": "cisco_xr",
        "cisco_xr": "cisco_xr",
        "junos": "juniper_junos",
        "juniper_junos": "juniper_junos",
    }
    return aliases.get(normalized, normalized or "arista_eos")


def dry_run_capability(platform: str) -> dict[str, str]:
    normalized = normalize_platform(platform)
    return {
        "platform": normalized,
        **DRY_RUN_CAPABILITIES.get(
            normalized,
            {
                "tier": "canary",
                "dry_run_kind": "canary_only",
                "mechanism": "no native pre-commit or offline validator is implemented; prove on a canary before rollout",
            },
        ),
    }


class AristaEOSLabAdapter(ExecutionAdapter):
    metadata = ExecutionAdapterMetadata(
        name="netcode.arista_config_session",
        platform="arista_eos",
        capabilities=["dry_run", "diff", "apply", "rollback", "verify"],
        safe_write_model="EOS config session with abortable dry-run and explicit commit",
        production_ready=False,
    )

    def __init__(self, device: Device, timeout: int = 45):
        self.device = device
        self.timeout = timeout
        self._conn = None

    def connect(self) -> None:
        try:
            from netmiko import ConnectHandler
        except Exception as exc:
            raise RuntimeError(f"netmiko is required for lab operations: {exc}") from exc

        params = {
            "device_type": "arista_eos",
            "host": self.device.host,
            "username": self.device.username,
            "password": self.device.password,
            "port": self.device.port,
            "fast_cli": False,
            "conn_timeout": self.timeout,
            "auth_timeout": self.timeout,
            "banner_timeout": self.timeout,
        }
        self._conn = ConnectHandler(**params)
        try:
            self._conn.enable()
        except Exception:
            enable_output = self._send("enable")
            if self._cli_error(enable_output):
                raise RuntimeError(f"Could not enter privileged mode: {enable_output}")
        self._send("terminal length 0")

    def disconnect(self) -> None:
        if self._conn:
            self._conn.disconnect()

    def _send(self, command: str, delay: float = 0.2) -> str:
        if not self._conn:
            raise RuntimeError("Not connected")
        return self._conn.send_command_timing(
            command,
            strip_prompt=False,
            strip_command=False,
            read_timeout=self.timeout,
            delay_factor=1,
        )

    def _cli_error(self, output: str) -> bool:
        error_markers = (
            "% Invalid input",
            "% Incomplete command",
            "% Ambiguous command",
            "% Permission denied",
            "privileged mode required",
        )
        return any(marker in output for marker in error_markers)

    def _send_checked(self, command: str) -> str:
        output = self._send(command)
        if self._cli_error(output):
            raise RuntimeError(f"EOS rejected command {command!r}: {output}")
        return output

    def show(self, command: str) -> str:
        return self._send(command)

    def dry_run(self, intent: Intent, render) -> LabResult:
        self.connect()
        try:
            return self.config_session(render.config, "dry-run")
        finally:
            self.disconnect()

    def apply(self, intent: Intent, render) -> LabResult:
        self.connect()
        try:
            session = self.config_session(render.config, "apply")
            if session.status != "pass":
                return session
            verify = self.verify_intent(intent, present=True)
            return LabResult(
                status="pass" if verify.status == "pass" else "fail",
                action="apply",
                device_id=self.device.id,
                message=verify.message if verify.status == "pass" else "Apply completed but verification failed.",
                session_name=session.session_name,
                evidence={"session": session.evidence, "verification": verify.evidence},
            )
        finally:
            self.disconnect()

    def rollback(self, intent: Intent, render) -> LabResult:
        self.connect()
        try:
            rollback = rollback_config(intent)
            if not rollback.strip():
                return LabResult(
                    status="fail",
                    action="rollback",
                    device_id=self.device.id,
                    message=f"No rollback command is defined for {intent.change_type}.",
                )
            session = self.config_session(rollback, "rollback")
            if session.status != "pass":
                return session
            verify = self.verify_intent(intent, present=False)
            return LabResult(
                status="pass" if verify.status == "pass" else "fail",
                action="rollback",
                device_id=self.device.id,
                message=verify.message if verify.status == "pass" else "Rollback committed but verification failed.",
                session_name=session.session_name,
                evidence={"session": session.evidence, "verification": verify.evidence},
            )
        finally:
            self.disconnect()

    def config_session(self, config: str, action: Literal["dry-run", "apply", "rollback"]) -> LabResult:
        session_name = f"netcode_{int(time.time())}"
        transcript: list[dict[str, str]] = []
        try:
            transcript.append({"command": f"configure session {session_name}", "output": self._send_checked(f"configure session {session_name}")})
            for line in config.splitlines():
                if line.strip():
                    transcript.append({"command": line, "output": self._send_checked(line)})
            diff = self._send_checked("show session-config diffs")
            transcript.append({"command": "show session-config diffs", "output": diff})
            if action == "dry-run":
                final = self._send_checked("abort")
                transcript.append({"command": "abort", "output": final})
                capability = dry_run_capability(self.device.platform)
                return LabResult(
                    status="pass",
                    action=action,
                    device_id=self.device.id,
                    message="EOS accepted candidate config in a config session and the session was aborted.",
                    session_name=session_name,
                    evidence={"diff": diff, "transcript": transcript, "dry_run_capability": capability},
                    dry_run_kind="native_session",
                )
            final = self._send_checked("commit")
            transcript.append({"command": "commit", "output": final})
            return LabResult(
                status="pass",
                action=action,
                device_id=self.device.id,
                message="EOS accepted and committed candidate config in the lab.",
                session_name=session_name,
                evidence={"diff": diff, "transcript": transcript},
            )
        except Exception as exc:
            try:
                abort = self._send("abort")
                transcript.append({"command": "abort", "output": abort})
            except Exception:
                pass
            return LabResult(
                status="fail",
                action=action,
                device_id=self.device.id,
                message=f"EOS config session failed: {exc}",
                session_name=session_name,
                evidence={"transcript": transcript},
            )

    def verify_vlan(self, vlan_id: int, vlan_name: str) -> LabResult:
        outputs: dict[str, str] = {}
        for command in (f"show vlan id {vlan_id}", f"show running-config | section ^vlan {vlan_id}"):
            try:
                outputs[command] = self.show(command)
            except Exception as exc:
                outputs[command] = f"ERROR: {exc}"
        vlan_table = outputs.get(f"show vlan id {vlan_id}", "")
        vlan_config = outputs.get(f"show running-config | section ^vlan {vlan_id}", "")
        vlan_seen = (
            re.search(rf"(?m)^\s*{vlan_id}\s+\S+", vlan_table) is not None
            or re.search(rf"(?m)^vlan {vlan_id}\s*$", vlan_config) is not None
        )
        combined = "\n".join(outputs.values())
        name_seen = vlan_name in combined
        if vlan_seen and name_seen:
            return LabResult(
                status="pass",
                action="verify",
                device_id=self.device.id,
                message=f"VLAN {vlan_id} with name {vlan_name} is present on the lab device.",
                evidence={"commands": outputs},
            )
        return LabResult(
            status="fail",
            action="verify",
            device_id=self.device.id,
            message=f"Could not prove VLAN {vlan_id} with name {vlan_name} exists.",
            evidence={"commands": outputs},
        )

    def verify_vlan_absent(self, vlan_id: int) -> LabResult:
        outputs: dict[str, str] = {}
        for command in (f"show vlan id {vlan_id}", f"show running-config | section ^vlan {vlan_id}"):
            try:
                outputs[command] = self.show(command)
            except Exception as exc:
                outputs[command] = f"ERROR: {exc}"
        vlan_table = outputs.get(f"show vlan id {vlan_id}", "")
        vlan_config = outputs.get(f"show running-config | section ^vlan {vlan_id}", "")
        vlan_seen = (
            re.search(rf"(?m)^\s*{vlan_id}\s+\S+", vlan_table) is not None
            or re.search(rf"(?m)^vlan {vlan_id}\s*$", vlan_config) is not None
        )
        if not vlan_seen:
            return LabResult(
                status="pass",
                action="verify_rollback",
                device_id=self.device.id,
                message=f"VLAN {vlan_id} is absent from the lab device.",
                evidence={"commands": outputs},
            )
        return LabResult(
            status="fail",
            action="verify_rollback",
            device_id=self.device.id,
            message=f"VLAN {vlan_id} is still present after rollback.",
            evidence={"commands": outputs},
        )

    def verify_intent(self, intent: Intent, present: bool = True) -> LabResult:
        # The registry names the verify method per change type; a new type adds a
        # _verify_* method here and points its spec at it — no ladder to edit.
        return getattr(self, spec_for(intent).verify_method)(intent, present)

    def _verify_add_vlan(self, intent: AddVlanIntent, present: bool) -> LabResult:
        return self.verify_vlan(intent.vlan.id, intent.vlan.name) if present else self.verify_vlan_absent(intent.vlan.id)

    def _verify_unsupported(self, intent: Intent, present: bool) -> LabResult:
        return LabResult(
            status="fail",
            action="verify",
            device_id=self.device.id,
            message=f"No live verification is defined for {intent.change_type}.",
        )

    def _verify_interface(self, intent: InterfaceConfigIntent, present: bool) -> LabResult:
        command = f"show running-config interfaces {intent.interface.name}"
        output = self.show(command)
        expected = f"interface {intent.interface.name}"
        seen = expected in output
        if present:
            if intent.interface.description:
                seen = seen and intent.interface.description in output
            if intent.interface.mode == "access" and intent.interface.access_vlan is not None:
                seen = seen and f"switchport access vlan {intent.interface.access_vlan}" in output
            if intent.interface.mode == "routed" and intent.interface.ip_address:
                seen = seen and "no switchport" in output and f"ip address {intent.interface.ip_address}" in output
            return LabResult(
                status="pass" if seen else "fail",
                action="verify",
                device_id=self.device.id,
                message=f"Interface {intent.interface.name} config {'matches' if seen else 'does not match'} desired state.",
                evidence={"commands": {command: output}},
            )
        absent = intent.interface.description not in output if intent.interface.description else True
        return LabResult(
            status="pass" if absent else "fail",
            action="verify_rollback",
            device_id=self.device.id,
            message=f"Interface {intent.interface.name} rollback {'was verified' if absent else 'still shows desired fragments'}.",
            evidence={"commands": {command: output}},
        )

    def _verify_bgp(self, intent: BgpNeighborIntent, present: bool) -> LabResult:
        command = f"show running-config | section router bgp {intent.bgp.asn}"
        output = self.show(command)
        neighbors = [neighbor.address for neighbor in intent.bgp.neighbors]
        seen = f"router bgp {intent.bgp.asn}" in output and all(f"neighbor {neighbor} remote-as" in output for neighbor in neighbors)
        if not present:
            seen = not any(f"neighbor {neighbor}" in output for neighbor in neighbors)
        return LabResult(
            status="pass" if seen else "fail",
            action="verify" if present else "verify_rollback",
            device_id=self.device.id,
            message=f"BGP neighbor config {'is present' if present and seen else 'is absent' if not present and seen else 'did not match expected state'}.",
            evidence={"commands": {command: output}, "neighbors": neighbors},
        )

    def _verify_acl(self, intent: AclRuleIntent, present: bool) -> LabResult:
        command = f"show running-config | section ip access-list {intent.acl.name}"
        output = self.show(command)
        line_seen = re.search(rf"(?m)^\s*{intent.acl.sequence}\s+{intent.acl.action}\s+{intent.acl.protocol}\s+", output) is not None
        seen = line_seen if present else not line_seen
        return LabResult(
            status="pass" if seen else "fail",
            action="verify" if present else "verify_rollback",
            device_id=self.device.id,
            message=f"ACL {intent.acl.name} sequence {intent.acl.sequence} {'matches' if seen else 'does not match'} expected state.",
            evidence={"commands": {command: output}},
        )

    def _verify_custom(self, intent: CustomConfigIntent, present: bool) -> LabResult:
        needle = intent.custom.verify_contains.strip()
        if not needle:
            lines = [line.strip() for line in intent.custom.config_lines.splitlines() if line.strip()]
            needle = lines[0] if lines else ""
        command = "show running-config"
        output = self.show(command)
        found = needle in output if needle else False
        seen = found if present else not found
        return LabResult(
            status="pass" if seen else "fail",
            action="verify" if present else "verify_rollback",
            device_id=self.device.id,
            message=(
                f"Custom config fragment {'is present' if found else 'is absent'} in running-config: {needle!r}."
                if needle
                else "No verify fragment available for this custom config."
            ),
            evidence={"command": command, "needle": needle, "found": found},
        )


    def _verify_ntp(self, intent: NtpStandardizeIntent, present: bool) -> LabResult:
        command = "show running-config | include ntp server"
        output = self.show(command)
        missing = [s for s in intent.ntp.servers if f"ntp server {s}" not in output]
        if present:
            ok = not missing
            message = (f"All {len(intent.ntp.servers)} approved NTP servers are configured."
                       if ok else f"Missing NTP servers: {', '.join(missing)}.")
        else:
            still = [s for s in intent.ntp.servers if f"ntp server {s}" in output]
            ok = not still
            message = ("Rollback verified: added NTP servers are gone."
                       if ok else f"Rollback incomplete: still configured: {', '.join(still)}.")
        return LabResult(
            status="pass" if ok else "fail",
            action="verify" if present else "verify_rollback",
            device_id=self.device.id,
            message=message,
            evidence={"commands": {command: output}, "servers": intent.ntp.servers},
        )

    def _verify_os_upgrade(self, intent: OsUpgradeIntent, present: bool) -> LabResult:
        command = "show running-config | include ^boot system"
        output = self.show(command)
        image = intent.os_upgrade.image
        staged = f"boot system flash:{image}" in output or image in output
        ok = staged if present else not staged
        return LabResult(
            status="pass" if ok else "fail",
            action="verify" if present else "verify_rollback",
            device_id=self.device.id,
            message=(
                f"Boot image {image} is staged; reload remains a separate approved maintenance-window action."
                if present and ok
                else f"Boot image {image} is not staged."
                if not present and ok
                else f"Boot image {image} did not match expected staged state."
            ),
            evidence={"commands": {command: output}, "image": image, "target_version": intent.os_upgrade.target_version},
        )


def _netmiko_device_type(platform: str) -> str:
    normalized = normalize_platform(platform)
    mapping = {
        "arista_eos": "arista_eos",
        "cisco_ios": "cisco_ios",
        "cisco_xe": "cisco_xe",
        "cisco_nxos": "cisco_nxos",
        "juniper_junos": "juniper_junos",
    }
    return mapping.get(normalized, normalized)


def _collect_running_config(device: Device, timeout: int = 45) -> str:
    try:
        from netmiko import ConnectHandler
    except Exception as exc:
        raise RuntimeError(f"netmiko is required for offline validation collection: {exc}") from exc

    params = {
        "device_type": _netmiko_device_type(device.platform),
        "host": device.host,
        "username": device.username,
        "password": device.password,
        "port": device.port,
        "fast_cli": False,
        "conn_timeout": timeout,
        "auth_timeout": timeout,
        "banner_timeout": timeout,
    }
    conn = ConnectHandler(**params)
    try:
        try:
            conn.enable()
        except Exception:
            pass
        return conn.send_command("show running-config", read_timeout=timeout)
    finally:
        conn.disconnect()


def _offline_preconditions(rendered_config: str, running_config: str) -> list[dict[str, Any]]:
    rendered_lines = [line.rstrip() for line in rendered_config.splitlines() if line.strip()]
    checks: list[dict[str, Any]] = [
        {
            "id": "rendered_config_present",
            "status": "pass" if bool(rendered_lines) else "fail",
            "message": f"{len(rendered_lines)} rendered config line(s) available for offline validation.",
        },
        {
            "id": "plain_cli_lines",
            "status": "pass" if not any(("\x00" in line or "\r" in line) for line in rendered_lines) else "fail",
            "message": "Rendered config uses plain CLI lines without control characters.",
        },
        {
            "id": "current_config_collected",
            "status": "pass" if bool(running_config.strip()) else "fail",
            "message": "Current running-config was collected read-only from the device.",
        },
    ]
    for line in rendered_lines:
        stripped = line.strip()
        if stripped.startswith("interface "):
            interface = stripped.split(" ", 1)[1]
            checks.append(
                {
                    "id": "interface_precondition",
                    "status": "pass" if f"interface {interface}" in running_config else "warning",
                    "message": (
                        f"Interface {interface} exists in running-config."
                        if f"interface {interface}" in running_config
                        else f"Interface {interface} was not found in running-config; canary must prove this line."
                    ),
                }
            )
        if stripped.startswith("vlan "):
            vlan = stripped.split(" ", 1)[1]
            checks.append(
                {
                    "id": "vlan_precondition",
                    "status": "pass" if f"vlan {vlan}" not in running_config else "warning",
                    "message": (
                        f"VLAN {vlan} is not already present."
                        if f"vlan {vlan}" not in running_config
                        else f"VLAN {vlan} already exists; rendered change may be idempotent."
                    ),
                }
            )
    return checks


def offline_dry_run(device: Device, intent: Intent, render, running_config: str | None = None) -> LabResult:
    capability = dry_run_capability(device.platform)
    try:
        current_config = running_config if running_config is not None else _collect_running_config(device)
    except Exception as exc:
        return LabResult(
            status="fail",
            action="dry-run",
            device_id=device.id,
            message=f"Offline validation could not collect running-config: {exc}",
            evidence={"dry_run_capability": capability, "collection_error": str(exc)},
            dry_run_kind="offline_validation",
        )
    checks = _offline_preconditions(render.config, current_config)
    passed = all(check["status"] in {"pass", "warning"} for check in checks)
    proposed = (current_config.rstrip() + "\n" + render.config.strip() + "\n") if current_config.strip() else render.config.strip() + "\n"
    diff = "\n".join(
        unified_diff(
            current_config.splitlines(),
            proposed.splitlines(),
            fromfile=f"{device.id}:running-config",
            tofile=f"{device.id}:candidate",
            lineterm="",
        )
    )
    return LabResult(
        status="pass" if passed else "fail",
        action="dry-run",
        device_id=device.id,
        message=(
            f"No native pre-commit on {normalize_platform(device.platform)}; validated by static analysis "
            "and read-only precondition checks. Change must still be proven on a canary before rollout."
        ),
        evidence={
            "dry_run_capability": capability,
            "preconditions": checks,
            "diff": diff,
            "rendered_config_lines": len([line for line in render.config.splitlines() if line.strip()]),
            "current_config_collected": bool(current_config.strip()),
        },
        dry_run_kind="offline_validation",
    )


def run_lab_action_for_device(device: Device, intent: Intent, render, action: Literal["dry-run", "apply", "rollback"]) -> LabResult:
    platform = normalize_platform(device.platform)
    if action == "dry-run":
        if platform == "arista_eos":
            return AristaEOSLabAdapter(device).dry_run(intent, render)
        capability = dry_run_capability(platform)
        if capability["dry_run_kind"] == "offline_validation":
            return offline_dry_run(device, intent, render)
        return LabResult(
            status="fail",
            action=action,
            device_id=device.id,
            message=f"No native or offline dry-run validator is implemented for {platform}; use a human-approved canary.",
            evidence={"dry_run_capability": capability},
            dry_run_kind="canary_only",
        )
    if platform != "arista_eos":
        return LabResult(
            status="fail",
            action=action,
            device_id=device.id,
            message=f"{action} is not implemented for {platform} in this runner yet.",
            evidence={"platform": platform, "write_supported": False},
        )
    adapter = AristaEOSLabAdapter(device)
    if action == "apply":
        return adapter.apply(intent, render)
    if action == "rollback":
        return adapter.rollback(intent, render)
    raise ValueError(f"Unsupported lab action {action}")


def lab_status() -> dict[str, object]:
    if not shutil.which("clab"):
        return {"ok": False, "message": "clab is not on PATH"}
    completed = subprocess.run(
        ["clab", "inspect", "--all"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _device_for_intent(paths: WorkspacePaths, intent: Intent, device_id: str | None) -> Device:
    inventory = Inventory(configured_inventory_path(paths))
    if device_id:
        if device_id not in inventory.by_id:
            raise ValueError(f"Unknown device {device_id}")
        return inventory.by_id[device_id]
    return inventory.resolve_targets(intent.targets, site=intent.site)[0]


def run_lab_action(paths: WorkspacePaths, intent_path: Path, action: Literal["dry-run", "apply", "rollback"], device_id: str | None = None) -> dict[str, object]:
    intent = load_intent(intent_path)
    render = render_intent(intent, paths)
    validation = StaticValidator(paths).validate(intent, render)
    if not validation.passed:
        return LabResult(
            status="fail",
            action=action,
            device_id=device_id or "unresolved",
            message="Static validation failed. Lab action blocked.",
            evidence={"validation": validation.model_dump()},
        ).__dict__
    if not lab_write_supported(intent):
        return LabResult(
            status="fail",
            action=action,
            device_id=device_id or "unresolved",
            message=f"{intent.change_type} is source-of-truth only in this MVP. Device write is locked.",
            evidence={"apply_locked": True, "change_type": intent.change_type},
        ).__dict__

    device = _device_for_intent(paths, intent, device_id)
    result = run_lab_action_for_device(device, intent, render, action)

    payload = result.__dict__.copy()
    if result.status == "pass" and action in {"apply", "rollback"}:
        state_result = AdapterRegistry().rez.collect_device_state(device)
        payload.setdefault("evidence", {})
        payload["evidence"]["rez_state"] = {
            "ok": state_result.get("ok"),
            "adapter": state_result.get("adapter"),
            "platform": state_result.get("platform"),
            "collection_time": state_result.get("collection_time"),
            "warnings": state_result.get("warnings", []),
            "errors": state_result.get("errors", []),
            "error": state_result.get("error"),
        }
        if isinstance(intent, AddVlanIntent):
            payload["evidence"]["rez_verification"] = verify_vlan_state(
                state_result,
                intent.vlan.id,
                intent.vlan.name,
                present=action == "apply",
            )
    return payload


def run_arista_end_to_end(paths: WorkspacePaths, intent_path: Path, device_id: str | None = None, apply: bool = True) -> EndToEndResult:
    pipeline = run_static_pipeline(paths, intent_path)
    intent = load_intent(intent_path)
    resolved_device = _device_for_intent(paths, intent, device_id)
    adapter_capabilities = AdapterRegistry().device_capabilities(resolved_device)
    phases: list[PhaseResult] = [
        PhaseResult(
            id="static_pipeline",
            title="Static Pipeline",
            status=pipeline.status,
            message="YAML, Jinja rendering, Git evidence, and static validation completed.",
            evidence={"checks": [check.model_dump() for check in pipeline.validation.checks]},
        )
    ]
    execution_adapter = adapter_capabilities.get("execution")
    state_info = adapter_capabilities.get("state", {})
    if not execution_adapter:
        phases.append(
            PhaseResult(
                id="adapter_contract",
                title="Adapter Contract",
                status="fail",
                message=f"No execution adapter is registered for {resolved_device.platform}.",
                evidence=adapter_capabilities,
            )
        )
        status = "fail"
        lab_evidence: dict[str, object] = {}
    else:
        state_available = bool(state_info.get("available")) if isinstance(state_info, dict) else False
        state_supported = bool(state_info.get("supported")) if isinstance(state_info, dict) else False
        adapter_status: Literal["pass", "skipped"] = "pass" if state_available and state_supported else "skipped"
        phases.append(
            PhaseResult(
                id="adapter_contract",
                title="Adapter Contract",
                status=adapter_status,
                message=(
                    "Execution adapter is registered and Rez state adapter supports this platform."
                    if adapter_status == "pass"
                    else "Execution adapter is registered; Rez state adapter is unavailable or unsupported in this runtime."
                ),
                evidence=adapter_capabilities,
            )
        )
        status = pipeline.status
        lab_evidence = {}

    if status == "fail":
        phases.append(
            PhaseResult(
                id="lab_dry_run",
                title="Arista Lab Dry-Run",
                status="skipped",
                message="Skipped because adapter contract failed.",
            )
        )
    elif pipeline.status != "pass":
        phases.append(
            PhaseResult(
                id="lab_dry_run",
                title="Arista Lab Dry-Run",
                status="skipped",
                message="Skipped because static validation failed.",
            )
        )
        status = "fail"
    else:
        dry_run = run_lab_action(paths, intent_path, "dry-run", resolved_device.id)
        lab_evidence["dry_run"] = dry_run
        phases.append(
            PhaseResult(
                id="lab_dry_run",
                title="Arista Lab Dry-Run",
                status="pass" if dry_run.get("status") == "pass" else "fail",
                message=str(dry_run.get("message", "")),
                evidence=dry_run,
            )
        )
        status = "pass" if dry_run.get("status") == "pass" else "fail"

        if status == "pass" and apply:
            apply_result = run_lab_action(paths, intent_path, "apply", resolved_device.id)
            lab_evidence["apply"] = apply_result
            phases.append(
                PhaseResult(
                    id="lab_apply_verify",
                    title="Arista Lab Apply And Verify",
                    status="pass" if apply_result.get("status") == "pass" else "fail",
                    message=str(apply_result.get("message", "")),
                    evidence=apply_result,
                )
            )
            status = "pass" if apply_result.get("status") == "pass" else "fail"
        elif not apply:
            phases.append(
                PhaseResult(
                    id="lab_apply_verify",
                    title="Arista Lab Apply And Verify",
                    status="skipped",
                    message="Apply was not requested.",
                )
            )

    stem = report_stem(intent)
    partial = EndToEndResult(
        status=status,
        intent_path=str(intent_path.resolve()),
        device_id=resolved_device.id,
        apply=apply,
        pipeline=pipeline,
        phases=phases,
        lab=lab_evidence,
        artifacts=None,
    )
    md_path, json_path = write_end_to_end_reports(paths, partial, stem)
    return partial.model_copy(
        update={
            "artifacts": EndToEndArtifacts(
                report_markdown_path=str(md_path),
                report_json_path=str(json_path),
            )
        }
    )
