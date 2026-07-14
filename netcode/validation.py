"""Fail-closed static validation."""

from __future__ import annotations

import re
from ipaddress import ip_address
from ipaddress import ip_network
from pathlib import Path

from netcode.change_types import redistribution_items, spec_for
from netcode.inventory import Inventory
from netcode.models import (
    AclRuleIntent,
    AddVlanIntent,
    BgpNeighborIntent,
    CheckResult,
    CustomConfigIntent,
    Intent,
    InterfaceConfigIntent,
    OsUpgradeIntent,
    RenderResult,
    SiteDeviceIntent,
    ValidationReport,
)
from netcode.paths import WorkspacePaths
from netcode.store import DEFAULT_ORG_ID, PlatformStore
from netcode.rendering import render_intent
from netcode.ui_config import configured_inventory_path, configured_policy_path
from netcode.yamlio import read_yaml


class StaticValidator:
    def __init__(
        self,
        paths: WorkspacePaths,
        inventory_path: Path | None = None,
        policy_path: Path | None = None,
        *,
        org_id: str = DEFAULT_ORG_ID,
    ):
        self.paths = paths
        self.inventory = Inventory(inventory_path or configured_inventory_path(paths))
        self.policy = read_yaml(policy_path or configured_policy_path(paths))
        self.org_id = org_id
        self.catalog = PlatformStore(paths)

    def validate(self, intent: Intent, render: RenderResult) -> ValidationReport:
        checks: list[CheckResult] = []
        # Each change type declares its policy checks by method name in the registry,
        # so a new type never edits this dispatch.
        policy_checks = [getattr(self, name) for name in spec_for(intent).policy_checks]
        validators = [self._schema_present, self._targets_exist, *policy_checks, self._render_scope, self._deterministic_render]
        for check in validators:
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
            f"Intent loaded into the {intent.change_type} model.",
            change_type=intent.change_type,
            site=intent.site,
        )

    def _targets_exist(self, intent: Intent, render: RenderResult) -> CheckResult:
        if isinstance(intent, SiteDeviceIntent):
            return self._pass(
                "targets",
                "Target Resolution",
                "Site/device intent may describe a new source-of-truth record. Device writes stay locked.",
                device_id=intent.device.device_id,
            )
        if intent.targets.device_group and not intent.targets.device_ids:
            devices = self.inventory.resolve_targets(intent.targets, site=intent.site)
            resolved_ids = [device.id for device in devices]
            source = "workspace_inventory"
        else:
            resolved_ids: list[str] = []
            missing: list[str] = []
            sources: set[str] = set()
            for device_id in intent.targets.device_ids:
                inventory_device = self.inventory.find_device(device_id)
                if inventory_device:
                    resolved_ids.append(inventory_device.id)
                    sources.add("workspace_inventory")
                    continue
                catalog_device = self.catalog.resolve_device(self.org_id, device_id)
                if catalog_device:
                    resolved_ids.append(str(catalog_device["canonical_id"]))
                    sources.add("runner_catalog")
                    continue
                missing.append(device_id)
            if missing:
                raise ValueError(f"Unknown target device(s): {', '.join(missing)}")
            if not resolved_ids:
                raise ValueError("No target devices resolved from intent")
            source = "+".join(sorted(sources))
        return self._pass(
            "targets",
            "Target Resolution",
            "All requested target devices resolve in the shared source of truth.",
            devices=resolved_ids,
            source=source,
        )

    def _site_policy(self, intent: SiteDeviceIntent, render: RenderResult) -> CheckResult:
        return self._pass(
            "site_policy",
            "Site/Device Policy",
            "Site/device intent is source-of-truth only. Device writes stay locked.",
            device_id=intent.device.device_id,
        )

    def _vlan_policy(self, intent: AddVlanIntent, render: RenderResult) -> CheckResult:
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

    def _subnet_overlap(self, intent: AddVlanIntent, render: RenderResult) -> CheckResult:
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

    def _segmentation(self, intent: AddVlanIntent, render: RenderResult) -> CheckResult:
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

    def _interface_policy(self, intent: InterfaceConfigIntent, render: RenderResult) -> CheckResult:
        name = intent.interface.name.lower()
        if name.startswith("management"):
            return self._fail("interface_policy", "Interface Policy", "Management interfaces are blocked from this UI workflow.", interface=intent.interface.name)
        if intent.interface.mode == "access" and intent.interface.access_vlan is not None:
            vlan_policy = self.policy.get("vlan", {})
            allowed_min, allowed_max = vlan_policy.get("allowed_range", [2, 4094])
            if intent.interface.access_vlan < allowed_min or intent.interface.access_vlan > allowed_max:
                return self._fail(
                    "interface_policy",
                    "Interface Policy",
                    f"Access VLAN must be in approved range {allowed_min}-{allowed_max}.",
                    vlan_id=intent.interface.access_vlan,
                )
        return self._pass(
            "interface_policy",
            "Interface Policy",
            "Interface intent stays within editable access/trunk/routed interface scope.",
            interface=intent.interface.name,
            mode=intent.interface.mode,
        )

    def _bgp_policy(self, intent: BgpNeighborIntent, render: RenderResult) -> CheckResult:
        if intent.bgp.asn < 1 or intent.bgp.asn > 4294967295:
            return self._fail("bgp_policy", "BGP Policy", "BGP ASN is outside valid range.", asn=intent.bgp.asn)
        for neighbor in intent.bgp.neighbors:
            try:
                ip_address(neighbor.address)
            except ValueError:
                return self._fail("bgp_policy", "BGP Policy", "BGP neighbor must be a valid IP address.", neighbor=neighbor.address)
            if neighbor.remote_as < 1 or neighbor.remote_as > 4294967295:
                return self._fail("bgp_policy", "BGP Policy", "Neighbor remote-as is outside valid range.", neighbor=neighbor.address, remote_as=neighbor.remote_as)
        return self._pass(
            "bgp_policy",
            "BGP Policy",
            "BGP neighbor intent has valid ASN and neighbor addressing. Treat as high risk until lab/canary proof exists.",
            asn=intent.bgp.asn,
            neighbors=[neighbor.address for neighbor in intent.bgp.neighbors],
        )

    def _routing_redistribution_policy(self, intent, render: RenderResult) -> CheckResult:
        items = redistribution_items(intent)
        supported = {("bgp", "ospf"), ("ospf", "bgp")}
        for item in items:
            if (item.from_protocol, item.to_protocol) not in supported:
                return self._fail(
                    "routing_redistribution_policy",
                    "Route Redistribution Policy",
                    "This controlled workflow supports only BGP/OSPF protocol boundaries.",
                )
            if any(str(prefix) == "0.0.0.0/0" for prefix in item.prefixes):
                return self._fail(
                    "routing_redistribution_policy",
                    "Route Redistribution Policy",
                    "Default-route redistribution is forbidden in this workflow.",
                )
            if item.to_protocol == "bgp" and not str(item.target_process).isdigit():
                return self._fail(
                    "routing_redistribution_policy",
                    "Route Redistribution Policy",
                    "The BGP target process must be a numeric ASN.",
                )
            statement = f"redistribute {item.from_protocol} route-map {item.route_map}"
            if statement not in render.config:
                return self._fail(
                    "routing_redistribution_policy",
                    "Route Redistribution Policy",
                    "Every redistribution direction must be constrained by its approved route-map.",
                )

        if len(items) == 2:
            forward = [ip_network(prefix, strict=False) for prefix in items[0].prefixes]
            reverse = [ip_network(prefix, strict=False) for prefix in items[1].prefixes]
            if any(left.overlaps(right) for left in forward for right in reverse):
                return self._fail(
                    "routing_redistribution_policy",
                    "Route Redistribution Policy",
                    "Bidirectional redistribution prefix scopes must not overlap.",
                )
        return self._pass(
            "routing_redistribution_policy",
            "Route Redistribution Policy",
            "Each route-exchange direction is constrained by a disjoint prefix list and explicit route-map.",
            boundaries=[
                {
                    "direction": f"{item.from_protocol}_to_{item.to_protocol}",
                    "route_map": item.route_map,
                    "prefix_list": item.prefix_list,
                    "prefixes": item.prefixes,
                    "route_tag": item.route_tag,
                }
                for item in items
            ],
        )

    def _acl_policy(self, intent: AclRuleIntent, render: RenderResult) -> CheckResult:
        if intent.acl.sequence < 1 or intent.acl.sequence > 9999:
            return self._fail("acl_policy", "ACL Policy", "ACL sequence must be between 1 and 9999.", sequence=intent.acl.sequence)
        if intent.acl.destination_port and intent.acl.protocol not in {"tcp", "udp"}:
            return self._fail("acl_policy", "ACL Policy", "Destination port is only valid for TCP or UDP rules.", protocol=intent.acl.protocol)
        return self._pass(
            "acl_policy",
            "ACL Policy",
            "ACL rule intent is syntactically scoped to one named ACL and sequence.",
            acl=intent.acl.name,
            sequence=intent.acl.sequence,
        )

    def _ntp_policy(self, intent, render: RenderResult) -> CheckResult:
        """Approved-source check: if the policy file names approved NTP servers,
        every server in the intent must be on that list (fail-closed standardization)."""
        servers = intent.ntp.servers
        for server in servers:
            try:
                ip_address(server)
            except ValueError:
                if not re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,253}$", server):
                    return self._fail("ntp_policy", "NTP Policy", f"'{server}' is not a valid IP or hostname.", server=server)
        approved = [str(s) for s in (self.policy.get("ntp", {}).get("approved_servers") or [])]
        if approved:
            rogue = [s for s in servers if s not in approved]
            if rogue:
                return self._fail(
                    "ntp_policy", "NTP Policy",
                    f"Not on the approved NTP server list: {', '.join(rogue)}. Approved: {', '.join(approved)}.",
                    rogue=rogue,
                )
        return self._pass(
            "ntp_policy", "NTP Policy",
            f"{len(servers)} NTP server{'s' if len(servers) != 1 else ''} "
            + ("validated against the approved list." if approved else "well-formed (no approved list in policy — add ntp.approved_servers to enforce)."),
            servers=servers,
        )

    def _custom_config_policy(self, intent: CustomConfigIntent, render: RenderResult) -> CheckResult:
        lines = [line for line in intent.custom.config_lines.splitlines() if line.strip()]
        if not lines:
            return self._fail("custom_policy", "Custom Config Policy", "Custom config has no config lines.")
        has_rollback = bool(intent.custom.rollback_lines.strip())
        if not has_rollback and not intent.custom.acknowledge_no_rollback:
            return self._fail(
                "custom_policy",
                "Custom Config Policy",
                "Custom config requires rollback commands, or an explicit acknowledgment that no rollback exists.",
                config_lines=len(lines),
            )
        return self._pass(
            "custom_policy",
            "Custom Config Policy",
            f"Custom config carries {len(lines)} line{'s' if len(lines) != 1 else ''} with "
            + ("engineer-supplied rollback." if has_rollback else "an explicit no-rollback acknowledgment."),
            config_lines=len(lines),
            rollback_supplied=has_rollback,
            acknowledged_no_rollback=intent.custom.acknowledge_no_rollback,
        )

    def _os_upgrade_policy(self, intent: OsUpgradeIntent, render: RenderResult) -> CheckResult:
        upgrade = intent.os_upgrade
        if not upgrade.maintenance_window.strip():
            return self._fail(
                "os_upgrade_policy",
                "OS Upgrade Policy",
                "A maintenance window is required before staging an OS upgrade.",
            )
        if "reload" in {line.strip().lower() for line in render.config.splitlines()}:
            return self._fail(
                "os_upgrade_policy",
                "OS Upgrade Policy",
                "Rendered OS upgrade config must not include a reload command.",
            )
        if upgrade.batch_size < upgrade.canary_size:
            return self._fail(
                "os_upgrade_policy",
                "OS Upgrade Policy",
                "Batch size must be greater than or equal to canary size.",
                canary_size=upgrade.canary_size,
                batch_size=upgrade.batch_size,
            )
        return self._pass(
            "os_upgrade_policy",
            "OS Upgrade Policy",
            "OS upgrade is staged only: image, MD5, maintenance window, canary, and rollback gates are present; reload is not rendered.",
            target_version=upgrade.target_version,
            image=upgrade.image,
            md5=upgrade.md5,
            maintenance_window=upgrade.maintenance_window,
            canary_size=upgrade.canary_size,
            batch_size=upgrade.batch_size,
        )

    def _render_scope(self, intent: Intent, render: RenderResult) -> CheckResult:
        scope = self.policy.get("render_scope", {})
        spec = spec_for(intent)
        # Allow-list and per-type block-list carve-outs come from the registry; a policy
        # YAML override (`<type>_allowed_prefixes`) still wins if present.
        allowed = tuple(scope.get(f"{intent.change_type}_allowed_prefixes", spec.allow_prefixes))
        carveouts = {c.lower() for c in spec.block_carveouts}
        blocked = [str(v).lower() for v in scope.get("blocked_fragments", []) if str(v).lower() not in carveouts]
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
                f"Rendered config contains lines outside the allowed {intent.change_type} scope.",
                unexpected_lines=unexpected_lines,
                allowed_prefixes=list(allowed),
            )
        return self._pass(
            "render_scope",
            "Rendered Config Scope",
            f"Rendered config only touches the intended {intent.change_type} feature scope.",
            line_count=len(render.config.splitlines()),
        )

    def _deterministic_render(self, intent: Intent, render: RenderResult) -> CheckResult:
        template_platform = Path(render.template_path).parent.name
        second = render_intent(intent, self.paths, platform=template_platform)
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
