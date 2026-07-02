"""Fail-closed static validation."""

from __future__ import annotations

import re
from ipaddress import ip_network
from pathlib import Path
from typing import Callable

from netcode.inventory import Inventory
from netcode.models import CheckResult, Intent, RenderResult, ValidationReport
from netcode.paths import WorkspacePaths
from netcode.rendering import render_intent
from netcode.yamlio import read_yaml


class StaticValidator:
    def __init__(self, paths: WorkspacePaths, inventory_path: Path | None = None, policy_path: Path | None = None):
        self.paths = paths
        self.inventory = Inventory(inventory_path or paths.inventories / "lab.yaml")
        self.policy = read_yaml(policy_path or paths.policies / "invariants.yaml")

    def validate(self, intent: Intent, render: RenderResult) -> ValidationReport:
        checks: list[CheckResult] = []
        for check in (
            self._schema_present,
            self._targets_exist,
            self._vlan_policy,
            self._subnet_overlap,
            self._segmentation,
            self._render_scope,
            self._deterministic_render,
        ):
            try:
                checks.append(check(intent, render))
            except Exception as exc:
                checks.append(
                    CheckResult(
                        id=check.__name__.lstrip("_"),
                        title=check.__name__.lstrip("_").replace("_", " ").title(),
                        status="fail",
                        severity="error",
                        message=f"Validator error: {exc}",
                        evidence={"fail_closed": True},
                    )
                )
        status = "pass" if all(c.status == "pass" for c in checks) else "fail"
        return ValidationReport(status=status, checks=checks)

    def _pass(self, check_id: str, title: str, message: str, **evidence: object) -> CheckResult:
        return CheckResult(id=check_id, title=title, status="pass", severity="info", message=message, evidence=evidence)

    def _fail(self, check_id: str, title: str, message: str, **evidence: object) -> CheckResult:
        return CheckResult(id=check_id, title=title, status="fail", severity="error", message=message, evidence=evidence)

    def _schema_present(self, intent: Intent, render: RenderResult) -> CheckResult:
        return self._pass(
            "schema",
            "Intent Schema",
            "Intent loaded into the add_vlan model.",
            change_type=intent.change_type,
            site=intent.site,
        )

    def _targets_exist(self, intent: Intent, render: RenderResult) -> CheckResult:
        devices = self.inventory.resolve_targets(intent.targets, site=intent.site)
        return self._pass(
            "targets",
            "Target Resolution",
            "All requested target devices resolve in inventory.",
            devices=[d.id for d in devices],
        )

    def _vlan_policy(self, intent: Intent, render: RenderResult) -> CheckResult:
        vlan_policy = self.policy.get("vlan", {})
        allowed_min, allowed_max = vlan_policy.get("allowed_range", [2, 4094])
        reserved = set(int(v) for v in vlan_policy.get("reserved", []))
        pattern = vlan_policy.get("name_pattern", r"^[A-Z0-9_\-]{2,32}$")
        vlan_id = intent.vlan.id
        if vlan_id < allowed_min or vlan_id > allowed_max:
            return self._fail(
                "vlan_policy",
                "VLAN Policy",
                f"VLAN {vlan_id} is outside the approved range {allowed_min}-{allowed_max}.",
                vlan_id=vlan_id,
            )
        if vlan_id in reserved:
            return self._fail(
                "vlan_policy",
                "VLAN Policy",
                f"VLAN {vlan_id} is reserved and cannot be used.",
                vlan_id=vlan_id,
            )
        if not re.match(pattern, intent.vlan.name):
            return self._fail(
                "vlan_policy",
                "VLAN Policy",
                "VLAN name does not match the naming standard.",
                name=intent.vlan.name,
                pattern=pattern,
            )
        return self._pass(
            "vlan_policy",
            "VLAN Policy",
            "VLAN ID and name match policy.",
            vlan_id=vlan_id,
            name=intent.vlan.name,
        )

    def _subnet_overlap(self, intent: Intent, render: RenderResult) -> CheckResult:
        candidate = ip_network(intent.vlan.subnet, strict=False)
        overlaps = []
        for existing in self.inventory.known_subnets(intent.site):
            existing_net = ip_network(existing, strict=False)
            if candidate.overlaps(existing_net):
                overlaps.append(existing)
        if overlaps:
            return self._fail(
                "subnet_overlap",
                "Subnet Overlap",
                "Requested VLAN subnet overlaps existing inventory subnet(s).",
                subnet=str(candidate),
                overlaps=overlaps,
            )
        return self._pass(
            "subnet_overlap",
            "Subnet Overlap",
            "Requested subnet does not overlap known site subnets.",
            subnet=str(candidate),
        )

    def _segmentation(self, intent: Intent, render: RenderResult) -> CheckResult:
        segmentation = self.policy.get("segmentation", {})
        guest_purposes = {str(v).lower() for v in segmentation.get("guest_purposes", [])}
        purpose = intent.vlan.purpose.lower()
        if purpose in guest_purposes and intent.policy.pci_reachable:
            return self._fail(
                "segmentation",
                "PCI Segmentation",
                "Guest VLANs cannot be marked PCI reachable.",
                purpose=purpose,
                pci_reachable=intent.policy.pci_reachable,
            )
        return self._pass(
            "segmentation",
            "PCI Segmentation",
            "Segmentation policy is preserved for this intent.",
            purpose=purpose,
            pci_reachable=intent.policy.pci_reachable,
        )

    def _render_scope(self, intent: Intent, render: RenderResult) -> CheckResult:
        scope = self.policy.get("render_scope", {})
        allowed = tuple(scope.get("add_vlan_allowed_prefixes", []))
        blocked = [str(v).lower() for v in scope.get("blocked_fragments", [])]
        unexpected_lines: list[str] = []
        blocked_lines: list[str] = []
        for line in render.config.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lower_line = line.lower()
            if any(fragment in lower_line for fragment in blocked):
                blocked_lines.append(line)
            if allowed and not line.startswith(allowed):
                unexpected_lines.append(line)
        if blocked_lines:
            return self._fail(
                "render_scope",
                "Rendered Config Scope",
                "Rendered config contains blocked management/routing/security fragments.",
                blocked_lines=blocked_lines,
            )
        if unexpected_lines:
            return self._fail(
                "render_scope",
                "Rendered Config Scope",
                "Rendered config contains lines outside the allowed add_vlan scope.",
                unexpected_lines=unexpected_lines,
                allowed_prefixes=list(allowed),
            )
        return self._pass(
            "render_scope",
            "Rendered Config Scope",
            "Rendered config only touches the intended VLAN feature scope.",
            line_count=len(render.config.splitlines()),
        )

    def _deterministic_render(self, intent: Intent, render: RenderResult) -> CheckResult:
        second = render_intent(intent, self.paths)
        if second.config != render.config:
            return self._fail(
                "deterministic_render",
                "Deterministic Render",
                "Rendering the same intent twice produced different output.",
            )
        return self._pass(
            "deterministic_render",
            "Deterministic Render",
            "Same intent renders to the same EOS config every time.",
        )
