"""Arista EOS lab adapter."""

from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import subprocess
import time
from difflib import unified_diff
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from netcode.adapters.execution import ExecutionAdapter, ExecutionAdapterMetadata
from netcode.adapters.registry import AdapterRegistry
from netcode.change_types import redistribution_items, spec_for
from netcode.inventory import Device, Inventory
from netcode.intent_utils import lab_write_supported, report_stem, rollback_config
from netcode.models import AclRuleIntent, AddVlanIntent, BgpNeighborIntent, CustomConfigIntent, EndToEndArtifacts, EndToEndResult, Intent, InterfaceConfigIntent, NtpStandardizeIntent, OsUpgradeIntent, PhaseResult, RoutingRedistributionIntent, load_intent
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


ProgressCallback = Callable[[dict[str, Any]], None]


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    phase: str,
    stage: str,
    message: str,
    status: str = "running",
    current_step: int | None = None,
    total_steps: int | None = None,
    command: str | None = None,
) -> None:
    if callback is None:
        return
    event: dict[str, Any] = {
        "phase": phase,
        "stage": stage,
        "status": status,
        "message": message,
    }
    if current_step is not None:
        event["current_step"] = current_step
    if total_steps is not None:
        event["total_steps"] = total_steps
    if command:
        event["command"] = command
    try:
        callback(event)
    except Exception:
        # Telemetry is display-only. Losing a progress frame must never change
        # whether a reviewed network operation succeeds or fails.
        return


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
    normalized = AdapterRegistry.normalize_execution_platform(platform)
    aliases = {
        "nxos": "cisco_nxos",
        "iosxr": "cisco_xr",
        "junos": "juniper_junos",
    }
    return aliases.get(normalized, normalized)


_NTP_STATE_SCHEMA = "netcode.ntp-pre-change.v1"


def _ntp_line_map(output: str, managed_servers: list[str]) -> dict[str, str]:
    managed = {str(server).strip() for server in managed_servers if str(server).strip()}
    result: dict[str, str] = {}
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        match = re.match(r"^ntp\s+server\s+(\S+)(?:\s+.*)?$", line, flags=re.IGNORECASE)
        if match and match.group(1) in managed:
            result[match.group(1)] = line
    return result


def _capture_ntp_state(device: Device, intent: NtpStandardizeIntent, output: str) -> dict[str, object]:
    managed_servers = sorted({str(server).strip() for server in intent.ntp.servers if str(server).strip()})
    prior_lines = _ntp_line_map(output, managed_servers)
    identity = "\n".join(f"{server}={prior_lines.get(server, '')}" for server in managed_servers)
    return {
        "schema": _NTP_STATE_SCHEMA,
        "device_id": device.id,
        "platform": normalize_platform(device.platform),
        "managed_servers": managed_servers,
        "prior_lines": prior_lines,
        "fingerprint": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    }


def _validated_ntp_state(
    device: Device,
    intent: NtpStandardizeIntent,
    state: object,
) -> dict[str, object]:
    if not isinstance(state, dict) or state.get("schema") != _NTP_STATE_SCHEMA:
        raise ValueError("Exact pre-change NTP state is missing; rollback is blocked.")
    if str(state.get("device_id") or "").strip().lower() != device.id.strip().lower():
        raise ValueError("Pre-change NTP state belongs to a different device.")
    if normalize_platform(str(state.get("platform") or "")) != normalize_platform(device.platform):
        raise ValueError("Pre-change NTP state belongs to a different platform.")
    expected_servers = sorted({str(server).strip() for server in intent.ntp.servers if str(server).strip()})
    managed_servers = sorted(str(server).strip() for server in state.get("managed_servers", []) if str(server).strip())
    if managed_servers != expected_servers:
        raise ValueError("Pre-change NTP state does not match the reviewed server scope.")
    prior_lines = state.get("prior_lines")
    if not isinstance(prior_lines, dict):
        raise ValueError("Pre-change NTP state has no restorable line map.")
    normalized_prior: dict[str, str] = {}
    for raw_server, raw_line in prior_lines.items():
        server = str(raw_server).strip()
        line = str(raw_line).strip()
        if server not in managed_servers or not line or "\n" in str(raw_line) or "\r" in str(raw_line):
            raise ValueError("Pre-change NTP state contains an out-of-scope restore command.")
        match = re.fullmatch(r"ntp\s+server\s+(\S+)(?:\s+.*)?", line, flags=re.IGNORECASE)
        if not match or match.group(1) != server:
            raise ValueError("Pre-change NTP state contains an invalid restore command.")
        normalized_prior[server] = line
    identity = "\n".join(f"{server}={normalized_prior.get(server, '')}" for server in managed_servers)
    expected_fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(state.get("fingerprint") or ""), expected_fingerprint):
        raise ValueError("Pre-change NTP state fingerprint is invalid.")
    return {
        **state,
        "managed_servers": managed_servers,
        "prior_lines": normalized_prior,
        "fingerprint": expected_fingerprint,
    }


def _ntp_state_matches(output: str, state: dict[str, object]) -> bool:
    servers = [str(value) for value in state["managed_servers"]]
    return _ntp_line_map(output, servers) == state["prior_lines"]


def _ntp_restore_config(output: str, state: dict[str, object]) -> str:
    servers = [str(value) for value in state["managed_servers"]]
    current = _ntp_line_map(output, servers)
    prior = {str(key): str(value) for key, value in dict(state["prior_lines"]).items()}
    commands: list[str] = []
    for server in servers:
        if current.get(server) == prior.get(server):
            continue
        if current.get(server):
            commands.append(f"no ntp server {server}")
        if prior.get(server):
            commands.append(prior[server])
    return "\n".join(commands) + ("\n" if commands else "")


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

    def __init__(
        self,
        device: Device,
        timeout: int = 45,
        *,
        progress: ProgressCallback | None = None,
        operation: str = "execution",
        operation_id: str = "",
        operation_context: dict[str, object] | None = None,
    ):
        self.device = device
        self.timeout = timeout
        self._conn = None
        self.progress = progress
        self.operation = operation
        self.operation_id = str(operation_id or "").strip()
        self.operation_context = dict(operation_context or {})
        self._verify_current = 0
        self._verify_total = 0
        self._verify_phase = "verify"
        self._verify_stage = "check_completed"

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
        _emit_progress(
            self.progress,
            phase=self.operation,
            stage="connected",
            message=f"Connected to {self.device.id} through the local runner.",
        )

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
        output = self._send(command)
        if self._verify_total:
            self._verify_current += 1
            _emit_progress(
                self.progress,
                phase=self._verify_phase,
                stage=self._verify_stage,
                message=f"Completed live check {self._verify_current} of {self._verify_total}.",
                current_step=self._verify_current,
                total_steps=self._verify_total,
                command=command,
            )
        return output

    def dry_run(self, intent: Intent, render) -> LabResult:
        self.operation = "dry-run"
        self.connect()
        try:
            rollback_state = None
            if isinstance(intent, NtpStandardizeIntent):
                current = self.show("show running-config | include ntp server")
                rollback_state = _capture_ntp_state(self.device, intent, current)
            result = self.config_session(render.config, "dry-run")
            if rollback_state is not None:
                result.evidence["rollback_state"] = rollback_state
            _emit_progress(
                self.progress,
                phase="dry-run",
                stage="passed" if result.status == "pass" else "failed",
                status="passed" if result.status == "pass" else "failed",
                message=result.message,
            )
            return result
        finally:
            self.disconnect()

    def apply(self, intent: Intent, render) -> LabResult:
        self.operation = "apply"
        self.connect()
        try:
            rollback_state = None
            if isinstance(intent, NtpStandardizeIntent):
                current = self.show("show running-config | include ntp server")
                try:
                    rollback_state = _validated_ntp_state(
                        self.device,
                        intent,
                        self.operation_context.get("approved_pre_change_state"),
                    )
                except ValueError as exc:
                    return LabResult(
                        status="fail",
                        action="apply",
                        device_id=self.device.id,
                        message=str(exc),
                        evidence={"write_started": False},
                    )
                if not _ntp_state_matches(current, rollback_state):
                    return LabResult(
                        status="fail",
                        action="apply",
                        device_id=self.device.id,
                        message="Live NTP state changed after the approved dry-run; re-run validation before applying.",
                        evidence={"write_started": False, "approved_pre_change_state": rollback_state},
                    )
            session = self.config_session(render.config, "apply")
            if session.status != "pass":
                if rollback_state is not None:
                    session.evidence["rollback_state"] = rollback_state
                return session
            _emit_progress(
                self.progress,
                phase="apply",
                stage="safety_check_started",
                message="Commit accepted; running the immediate post-change safety check.",
            )
            verify = self.verify_intent(
                intent,
                present=True,
                progress_phase="apply",
                progress_stage="safety_check",
            )
            result = LabResult(
                status="pass" if verify.status == "pass" else "fail",
                action="apply",
                device_id=self.device.id,
                message=verify.message if verify.status == "pass" else "Apply completed but verification failed.",
                session_name=session.session_name,
                evidence={
                    "session": session.evidence,
                    "verification": verify.evidence,
                    **({"rollback_state": rollback_state} if rollback_state is not None else {}),
                },
            )
            _emit_progress(
                self.progress,
                phase="apply",
                stage="passed" if result.status == "pass" else "failed",
                status="passed" if result.status == "pass" else "failed",
                message=result.message,
            )
            return result
        finally:
            self.disconnect()

    def rollback(self, intent: Intent, render) -> LabResult:
        self.operation = "rollback"
        self.connect()
        try:
            rollback_state = None
            if isinstance(intent, NtpStandardizeIntent):
                try:
                    rollback_state = _validated_ntp_state(
                        self.device,
                        intent,
                        self.operation_context.get("rollback_state"),
                    )
                except ValueError as exc:
                    return LabResult(
                        status="fail",
                        action="rollback",
                        device_id=self.device.id,
                        message=str(exc),
                        evidence={"write_started": False},
                    )
                current = self.show("show running-config | include ntp server")
                rollback = _ntp_restore_config(current, rollback_state)
            else:
                rollback = rollback_config(intent)
            if not rollback.strip():
                if rollback_state is not None:
                    verify = self._verify_ntp_state(rollback_state)
                    return LabResult(
                        status=verify.status,
                        action="rollback",
                        device_id=self.device.id,
                        message="NTP state already matches the exact pre-change state.",
                        evidence={"no_op": True, "verification": verify.evidence, "rollback_state": rollback_state},
                    )
                return LabResult(
                    status="fail",
                    action="rollback",
                    device_id=self.device.id,
                    message=f"No rollback command is defined for {intent.change_type}.",
                )
            session = self.config_session(rollback, "rollback")
            if session.status != "pass":
                return session
            verify = (
                self._verify_ntp_state(rollback_state)
                if rollback_state is not None
                else self.verify_intent(
                    intent,
                    present=False,
                    progress_phase="rollback",
                    progress_stage="previous_state_check",
                )
            )
            result = LabResult(
                status="pass" if verify.status == "pass" else "fail",
                action="rollback",
                device_id=self.device.id,
                message=verify.message if verify.status == "pass" else "Rollback committed but verification failed.",
                session_name=session.session_name,
                evidence={
                    "session": session.evidence,
                    "verification": verify.evidence,
                    **({"rollback_state": rollback_state} if rollback_state is not None else {}),
                },
            )
            _emit_progress(
                self.progress,
                phase="rollback",
                stage="passed" if result.status == "pass" else "failed",
                status="passed" if result.status == "pass" else "failed",
                message=result.message,
            )
            return result
        finally:
            self.disconnect()

    def config_session(self, config: str, action: Literal["dry-run", "apply", "rollback"]) -> LabResult:
        session_name = (
            f"netcode_{hashlib.sha256(self.operation_id.encode('utf-8')).hexdigest()[:12]}"
            if self.operation_id
            else f"netcode_{int(time.time())}"
        )
        transcript: list[dict[str, str]] = []
        commands = [line for line in config.splitlines() if line.strip()]
        try:
            transcript.append({"command": f"configure session {session_name}", "output": self._send_checked(f"configure session {session_name}")})
            _emit_progress(
                self.progress,
                phase=action,
                stage="session_created",
                message=f"Created candidate configuration session {session_name}.",
            )
            command_stage = "commands_staged" if action == "dry-run" else "reverse_commands_applied" if action == "rollback" else "commands_applied"
            for index, line in enumerate(commands, start=1):
                transcript.append({"command": line, "output": self._send_checked(line)})
                _emit_progress(
                    self.progress,
                    phase=action,
                    stage=command_stage,
                    message=f"Accepted command {index} of {len(commands)} into the candidate session.",
                    current_step=index,
                    total_steps=len(commands),
                    command=line,
                )
            diff = self._send_checked("show session-config diffs")
            transcript.append({"command": "show session-config diffs", "output": diff})
            _emit_progress(
                self.progress,
                phase=action,
                stage="diff_generated",
                message="Generated the candidate-versus-running configuration diff.",
                command="show session-config diffs",
            )
            if action == "dry-run":
                final = self._send_checked("abort")
                transcript.append({"command": "abort", "output": final})
                _emit_progress(
                    self.progress,
                    phase=action,
                    stage="session_aborted",
                    message="Aborted the candidate session; no configuration was written.",
                    command="abort",
                )
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
            _emit_progress(
                self.progress,
                phase=action,
                stage="commit_started",
                message="Submitting the reviewed candidate session for commit.",
                command="commit",
            )
            final = self._send_checked("commit")
            transcript.append({"command": "commit", "output": final})
            _emit_progress(
                self.progress,
                phase=action,
                stage="commit_accepted",
                message="The device accepted the commit.",
                command="commit",
            )
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
            _emit_progress(
                self.progress,
                phase=action,
                stage="failed",
                status="failed",
                message=f"Configuration session failed: {exc}",
            )
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

    def verify_intent(
        self,
        intent: Intent,
        present: bool = True,
        *,
        progress_phase: str = "verify",
        progress_stage: str = "check_completed",
    ) -> LabResult:
        # The registry names the verify method per change type; a new type adds a
        # _verify_* method here and points its spec at it — no ladder to edit.
        if isinstance(intent, AddVlanIntent):
            total = 2
        elif isinstance(intent, RoutingRedistributionIntent):
            total = max(1, len(redistribution_items(intent)))
        else:
            total = 1
        self._verify_current = 0
        self._verify_total = total
        self._verify_phase = progress_phase
        self._verify_stage = progress_stage
        _emit_progress(
            self.progress,
            phase=progress_phase,
            stage="checks_started" if progress_phase == "verify" else progress_stage,
            message=f"Running {total} live verification check{'s' if total != 1 else ''}.",
            current_step=0,
            total_steps=total,
        )
        try:
            result = getattr(self, spec_for(intent).verify_method)(intent, present)
        finally:
            self._verify_total = 0
        _emit_progress(
            self.progress,
            phase=progress_phase,
            stage="expected_actual",
            status="passed" if result.status == "pass" else "failed",
            message=result.message,
            current_step=total,
            total_steps=total,
        )
        if progress_phase == "verify":
            _emit_progress(
                self.progress,
                phase="verify",
                stage="passed" if result.status == "pass" else "failed",
                status="passed" if result.status == "pass" else "failed",
                message=result.message,
            )
        return result

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
        if intent.interface.apply_scope == "admin_state":
            command = f"show interfaces {intent.interface.name}"
            output = self.show(command)
            administratively_down = "administratively down" in output.lower()
            expected_enabled = intent.interface.enabled if present else not intent.interface.enabled
            matched = not administratively_down if expected_enabled else administratively_down
            return LabResult(
                status="pass" if matched else "fail",
                action="verify" if present else "verify_rollback",
                device_id=self.device.id,
                message=(
                    f"Interface {intent.interface.name} administrative state "
                    f"{'matches' if matched else 'does not match'} the expected "
                    f"{'enabled' if expected_enabled else 'disabled'} state."
                ),
                evidence={"commands": {command: output}, "expected_enabled": expected_enabled},
            )
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

    def _verify_redistribution(self, intent: RoutingRedistributionIntent, present: bool) -> LabResult:
        commands: dict[str, str] = {}
        boundaries: list[dict[str, object]] = []
        for item in redistribution_items(intent):
            command = f"show running-config | section router {item.to_protocol} {item.target_process}"
            output = self.show(command)
            commands[command] = output
            statement = f"redistribute {item.from_protocol} route-map {item.route_map}"
            boundaries.append({
                "direction": f"{item.from_protocol}_to_{item.to_protocol}",
                "statement": statement,
                "found": statement in output,
            })
        seen = (
            all(bool(item["found"]) for item in boundaries)
            if present
            else all(not bool(item["found"]) for item in boundaries)
        )
        state = "present" if present and seen else "absent" if not present and seen else "did not match expected state"
        return LabResult(
            status="pass" if seen else "fail",
            action="verify" if present else "verify_rollback",
            device_id=self.device.id,
            message=f"Controlled route-exchange boundaries are {state}.",
            evidence={"commands": commands, "boundaries": boundaries},
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

    def _verify_ntp_state(self, state: dict[str, object]) -> LabResult:
        command = "show running-config | include ntp server"
        output = self.show(command)
        matched = _ntp_state_matches(output, state)
        return LabResult(
            status="pass" if matched else "fail",
            action="verify_rollback",
            device_id=self.device.id,
            message=(
                "Exact pre-change NTP state was restored."
                if matched
                else "NTP rollback did not restore the exact reviewed pre-change state."
            ),
            evidence={
                "commands": {command: output},
                "expected_lines": state["prior_lines"],
                "managed_servers": state["managed_servers"],
            },
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


class CiscoIOSNtpAdapter(AristaEOSLabAdapter):
    """Governed IOS/IOS-XE execution for the Community Golden Baseline pack.

    IOS has no candidate commit contract comparable to EOS config sessions, so
    the reviewed dry-run is offline and the first target is the proof device.
    Running config is saved only after the live NTP verification succeeds.
    """

    metadata = ExecutionAdapterMetadata(
        name="netcode.cisco_ios_ntp",
        platform="cisco_ios",
        capabilities=["dry_run", "diff", "apply", "rollback", "verify"],
        safe_write_model="offline validation, first-device proof, verify-before-save, exact pre-change rollback",
        production_ready=False,
    )

    def connect(self) -> None:
        try:
            from netmiko import ConnectHandler
        except Exception as exc:
            raise RuntimeError(f"netmiko is required for Cisco IOS operations: {exc}") from exc

        self._conn = ConnectHandler(
            device_type="cisco_ios",
            host=self.device.host,
            username=self.device.username,
            password=self.device.password,
            port=self.device.port,
            fast_cli=False,
            conn_timeout=self.timeout,
            auth_timeout=self.timeout,
            banner_timeout=self.timeout,
        )
        try:
            self._conn.enable()
        except Exception as exc:
            raise RuntimeError(f"Could not enter Cisco IOS privileged mode: {exc}") from exc
        self._send("terminal length 0")
        _emit_progress(
            self.progress,
            phase=self.operation,
            stage="connected",
            message=f"Connected to {self.device.id} through the Local Connector.",
        )

    def _cli_error(self, output: str) -> bool:
        markers = (
            "% Invalid input",
            "% Incomplete command",
            "% Ambiguous command",
            "% Authorization failed",
            "% Configuration failed",
            "% Permission denied",
        )
        return any(marker.lower() in str(output or "").lower() for marker in markers)

    def _configure(
        self,
        config: str,
        phase: str,
        *,
        reverse: bool = False,
    ) -> tuple[str, list[dict[str, str]]]:
        if not self._conn:
            raise RuntimeError("Not connected")
        commands = [line.strip() for line in config.splitlines() if line.strip()]
        if not commands:
            return "", []
        output = self._conn.send_config_set(
            config_commands=commands,
            read_timeout=self.timeout,
            error_pattern=r"%\s*(?:Invalid input|Incomplete command|Ambiguous command|Authorization failed|Configuration failed)",
        )
        if self._cli_error(output):
            raise RuntimeError(f"Cisco IOS rejected the reviewed configuration batch: {output}")
        transcript: list[dict[str, str]] = []
        for index, command in enumerate(commands, start=1):
            transcript.append({
                "command": command,
                "output": output if index == len(commands) else "Accepted in reviewed configuration batch.",
            })
            _emit_progress(
                self.progress,
                phase=phase,
                stage="reverse_commands_applied" if reverse or phase == "rollback" else "commands_applied",
                message=f"Device accepted command {index} of {len(commands)}.",
                current_step=index,
                total_steps=len(commands),
                command=command,
            )
        return output, transcript

    def _save_verified_config(self, phase: str) -> str:
        if not self._conn or not hasattr(self._conn, "save_config"):
            raise RuntimeError("Cisco IOS connector cannot save the verified running configuration.")
        output = str(self._conn.save_config())
        if self._cli_error(output):
            raise RuntimeError(f"Cisco IOS rejected the save operation: {output}")
        _emit_progress(
            self.progress,
            phase=phase,
            stage="startup_config_saved",
            message="Verified running configuration was saved to startup configuration.",
        )
        return output

    def _restore_running_ntp_state(
        self,
        state: dict[str, object],
        *,
        persist: bool = False,
    ) -> dict[str, object]:
        """Best-effort compensation for a failed IOS transaction.

        IOS changes running-config immediately. A failed command, verification,
        or save therefore must restore the exact reviewed pre-change state
        before the job returns. If compensation cannot be proven, the result
        remains failed and explicitly requires reconciliation.
        """
        evidence: dict[str, object] = {
            "attempted": True,
            "status": "failed",
            "startup_config_saved": False,
        }
        _emit_progress(
            self.progress,
            phase=self.operation,
            stage="automatic_rollback_started",
            message="Apply did not complete safely; restoring the reviewed pre-change NTP state.",
        )
        try:
            current = self.show("show running-config | include ntp server")
            rollback = _ntp_restore_config(current, state)
            transcript: list[dict[str, str]] = []
            if rollback:
                _, transcript = self._configure(
                    rollback,
                    self.operation,
                    reverse=True,
                )
            verify = self._verify_ntp_state(state)
            evidence.update({
                "status": verify.status,
                "commands": transcript,
                "verification": verify.evidence,
                "no_op": not bool(rollback),
            })
            if verify.status != "pass":
                evidence["error"] = verify.message
                return evidence
            if persist:
                evidence["save_output"] = self._save_verified_config(self.operation)
                evidence["startup_config_saved"] = True
            _emit_progress(
                self.progress,
                phase=self.operation,
                stage="automatic_rollback_verified",
                status="passed",
                message="Exact pre-change NTP state was restored and verified.",
            )
            return evidence
        except Exception as exc:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            _emit_progress(
                self.progress,
                phase=self.operation,
                stage="automatic_rollback_failed",
                status="failed",
                message="Automatic restoration could not be proven; manual reconciliation is required.",
            )
            return evidence

    def apply(self, intent: Intent, render) -> LabResult:
        self.operation = "apply"
        if not isinstance(intent, NtpStandardizeIntent):
            return LabResult(
                status="fail",
                action="apply",
                device_id=self.device.id,
                message=f"Cisco IOS Community execution supports ntp_standardize, not {intent.change_type}.",
                evidence={"write_started": False},
            )
        self.connect()
        rollback_state: dict[str, object] | None = None
        transcript: list[dict[str, str]] = []
        try:
            current = self.show("show running-config | include ntp server")
            try:
                rollback_state = _validated_ntp_state(
                    self.device,
                    intent,
                    self.operation_context.get("approved_pre_change_state"),
                )
            except ValueError as exc:
                return LabResult(
                    status="fail",
                    action="apply",
                    device_id=self.device.id,
                    message=str(exc),
                    evidence={"write_started": False},
                )
            if not _ntp_state_matches(current, rollback_state):
                return LabResult(
                    status="fail",
                    action="apply",
                    device_id=self.device.id,
                    message="Live NTP state changed after the approved dry-run; re-run validation before applying.",
                    evidence={"write_started": False, "approved_pre_change_state": rollback_state},
                )
            try:
                _, transcript = self._configure(render.config, "apply")
            except Exception as exc:
                compensation = self._restore_running_ntp_state(rollback_state)
                return LabResult(
                    status="fail",
                    action="apply",
                    device_id=self.device.id,
                    message=(
                        f"Cisco IOS apply failed before verification: {exc}. "
                        + (
                            "The exact pre-change state was restored."
                            if compensation.get("status") == "pass"
                            else "Automatic restoration was not proven; manual reconciliation is required."
                        )
                    ),
                    evidence={
                        "transcript": transcript,
                        "rollback_state": rollback_state,
                        "startup_config_saved": False,
                        "automatic_rollback": compensation,
                        "running_config_may_be_modified": compensation.get("status") != "pass",
                    },
                )
            verify = self.verify_intent(
                intent,
                present=True,
                progress_phase="apply",
                progress_stage="safety_check",
            )
            if verify.status != "pass":
                compensation = self._restore_running_ntp_state(rollback_state)
                return LabResult(
                    status="fail",
                    action="apply",
                    device_id=self.device.id,
                    message=(
                        "Cisco IOS live verification failed; startup config was not saved. "
                        + (
                            "The exact pre-change running state was restored."
                            if compensation.get("status") == "pass"
                            else "Automatic restoration was not proven; manual reconciliation is required."
                        )
                    ),
                    evidence={
                        "transcript": transcript,
                        "verification": verify.evidence,
                        "rollback_state": rollback_state,
                        "startup_config_saved": False,
                        "automatic_rollback": compensation,
                        "running_config_may_be_modified": compensation.get("status") != "pass",
                    },
                )
            try:
                save_output = self._save_verified_config("apply")
            except Exception as exc:
                compensation = self._restore_running_ntp_state(rollback_state, persist=True)
                return LabResult(
                    status="fail",
                    action="apply",
                    device_id=self.device.id,
                    message=(
                        f"Live verification passed, but startup-config save failed: {exc}. "
                        + (
                            "The exact pre-change state was restored and saved."
                            if compensation.get("status") == "pass" and compensation.get("startup_config_saved")
                            else "Automatic restoration was not proven durable; manual reconciliation is required."
                        )
                    ),
                    evidence={
                        "transcript": transcript,
                        "verification": verify.evidence,
                        "rollback_state": rollback_state,
                        "startup_config_saved": False,
                        "automatic_rollback": compensation,
                        "running_config_may_be_modified": compensation.get("status") != "pass",
                    },
                )
            return LabResult(
                status="pass",
                action="apply",
                device_id=self.device.id,
                message="Cisco IOS NTP standardization applied, verified live, and saved.",
                evidence={
                    "transcript": transcript,
                    "verification": verify.evidence,
                    "rollback_state": rollback_state,
                    "startup_config_saved": True,
                    "save_output": save_output,
                },
            )
        finally:
            self.disconnect()

    def rollback(self, intent: Intent, render) -> LabResult:
        self.operation = "rollback"
        if not isinstance(intent, NtpStandardizeIntent):
            return LabResult(
                status="fail",
                action="rollback",
                device_id=self.device.id,
                message=f"Cisco IOS Community rollback supports ntp_standardize, not {intent.change_type}.",
                evidence={"write_started": False},
            )
        try:
            rollback_state = _validated_ntp_state(
                self.device,
                intent,
                self.operation_context.get("rollback_state"),
            )
        except ValueError as exc:
            return LabResult(
                status="fail",
                action="rollback",
                device_id=self.device.id,
                message=str(exc),
                evidence={"write_started": False},
            )
        self.connect()
        transcript: list[dict[str, str]] = []
        try:
            current = self.show("show running-config | include ntp server")
            rollback = _ntp_restore_config(current, rollback_state)
            if rollback:
                try:
                    _, transcript = self._configure(rollback, "rollback")
                except Exception as exc:
                    return LabResult(
                        status="fail",
                        action="rollback",
                        device_id=self.device.id,
                        message=f"Cisco IOS rollback command failed: {exc}",
                        evidence={"transcript": transcript, "rollback_state": rollback_state},
                    )
            verify = self._verify_ntp_state(rollback_state)
            if verify.status != "pass":
                return LabResult(
                    status="fail",
                    action="rollback",
                    device_id=self.device.id,
                    message=verify.message,
                    evidence={"transcript": transcript, "verification": verify.evidence, "rollback_state": rollback_state},
                )
            try:
                save_output = self._save_verified_config("rollback")
            except Exception as exc:
                return LabResult(
                    status="fail",
                    action="rollback",
                    device_id=self.device.id,
                    message=f"NTP state was restored in running config, but startup-config save failed: {exc}",
                    evidence={"transcript": transcript, "verification": verify.evidence, "rollback_state": rollback_state},
                )
            return LabResult(
                status="pass",
                action="rollback",
                device_id=self.device.id,
                message="Cisco IOS exact pre-change NTP state restored, verified, and saved.",
                evidence={
                    "transcript": transcript,
                    "verification": verify.evidence,
                    "rollback_state": rollback_state,
                    "startup_config_saved": True,
                    "save_output": save_output,
                },
            )
        finally:
            self.disconnect()


def _netmiko_device_type(platform: str) -> str:
    normalized = normalize_platform(platform)
    mapping = {
        "arista_eos": "arista_eos",
        "cisco_ios": "cisco_ios",
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


def offline_dry_run(
    device: Device,
    intent: Intent,
    render,
    running_config: str | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> LabResult:
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
    _emit_progress(
        progress,
        phase="dry-run",
        stage="live_state_collected",
        message="Collected running configuration for offline validation.",
    )
    checks = _offline_preconditions(render.config, current_config)
    for index, check in enumerate(checks, start=1):
        _emit_progress(
            progress,
            phase="dry-run",
            stage="check_completed",
            status="passed" if check["status"] in {"pass", "warning"} else "failed",
            message=str(check.get("message") or check.get("title") or f"Offline check {index}"),
            current_step=index,
            total_steps=len(checks),
        )
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
    _emit_progress(
        progress,
        phase="dry-run",
        stage="diff_generated",
        message="Generated the offline candidate-versus-running diff.",
    )
    result = LabResult(
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
            **(
                {"rollback_state": _capture_ntp_state(device, intent, current_config)}
                if isinstance(intent, NtpStandardizeIntent)
                else {}
            ),
        },
        dry_run_kind="offline_validation",
    )
    _emit_progress(
        progress,
        phase="dry-run",
        stage="passed" if result.status == "pass" else "failed",
        status="passed" if result.status == "pass" else "failed",
        message=result.message,
    )
    return result


def run_lab_action_for_device(
    device: Device,
    intent: Intent,
    render,
    action: Literal["dry-run", "apply", "rollback"],
    *,
    progress: ProgressCallback | None = None,
    operation_id: str = "",
    operation_context: dict[str, object] | None = None,
) -> LabResult:
    platform = normalize_platform(device.platform)
    try:
        AdapterRegistry.require_execution_support(platform, intent.change_type)
    except ValueError as exc:
        return LabResult(
            status="fail",
            action=action,
            device_id=device.id,
            message=str(exc),
            evidence={"platform": platform, "write_started": False},
        )
    if action == "dry-run":
        if platform == "arista_eos":
            return AristaEOSLabAdapter(
                device,
                progress=progress,
                operation=action,
                operation_id=operation_id,
                operation_context=operation_context,
            ).dry_run(intent, render)
        capability = dry_run_capability(platform)
        if capability["dry_run_kind"] == "offline_validation":
            return offline_dry_run(device, intent, render, progress=progress)
        return LabResult(
            status="fail",
            action=action,
            device_id=device.id,
            message=f"No native or offline dry-run validator is implemented for {platform}; use a human-approved canary.",
            evidence={"dry_run_capability": capability},
            dry_run_kind="canary_only",
        )
    adapter_class = AristaEOSLabAdapter if platform == "arista_eos" else CiscoIOSNtpAdapter
    adapter = adapter_class(
        device,
        progress=progress,
        operation=action,
        operation_id=operation_id,
        operation_context=operation_context,
    )
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


def run_lab_action(
    paths: WorkspacePaths,
    intent_path: Path,
    action: Literal["dry-run", "apply", "rollback"],
    device_id: str | None = None,
    *,
    operation_context: dict[str, object] | None = None,
) -> dict[str, object]:
    intent = load_intent(intent_path)
    device = _device_for_intent(paths, intent, device_id)
    render = render_intent(intent, paths, platform=device.platform)
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

    result = run_lab_action_for_device(
        device,
        intent,
        render,
        action,
        operation_context=operation_context,
    )

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
