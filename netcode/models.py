"""Intent and result models."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from netcode.yamlio import read_yaml


class TargetSpec(BaseModel):
    device_ids: list[str] = Field(default_factory=list)
    device_group: str | None = None

    @model_validator(mode="after")
    def at_least_one_target(self) -> "TargetSpec":
        if not self.device_ids and not self.device_group:
            raise ValueError("targets.device_ids or targets.device_group is required")
        return self


class SviSpec(BaseModel):
    enabled: bool = False
    gateway_ip: str | None = None


class VlanSpec(BaseModel):
    id: int
    name: str
    subnet: str
    purpose: str = "general"
    svi: SviSpec = Field(default_factory=SviSpec)

    @field_validator("id")
    @classmethod
    def vlan_id_range(cls, value: int) -> int:
        if value < 1 or value > 4094:
            raise ValueError("VLAN ID must be between 1 and 4094")
        return value

    @field_validator("name")
    @classmethod
    def vlan_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("VLAN name cannot be empty")
        return value

    @field_validator("subnet")
    @classmethod
    def valid_subnet(cls, value: str) -> str:
        ip_network(value, strict=False)
        return value


class PolicySpec(BaseModel):
    pci_reachable: bool = False
    internet_reachable: bool = True


class IntentMetadata(BaseModel):
    requested_by: str = "netcode-user"
    ticket_id: str | None = None
    learning_mode: bool = True
    change_instance_id: str | None = None


class AddVlanIntent(BaseModel):
    change_type: Literal["add_vlan"] = "add_vlan"
    site: str
    targets: TargetSpec
    vlan: VlanSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)

    @field_validator("site")
    @classmethod
    def site_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("site is required")
        return value


class InterfaceSpec(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    apply_scope: Literal["full", "admin_state"] = "full"
    mode: Literal["access", "trunk", "routed"] = "access"
    access_vlan: int | None = None
    trunk_allowed_vlans: list[int] = Field(default_factory=list)
    ip_address: str | None = None

    @field_validator("name")
    @classmethod
    def interface_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("interface name is required")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> "InterfaceSpec":
        if self.apply_scope == "admin_state":
            return self
        if self.mode == "access" and self.access_vlan is None:
            raise ValueError("access_vlan is required for access interfaces")
        if self.mode == "routed" and not self.ip_address:
            raise ValueError("ip_address is required for routed interfaces")
        return self


class InterfaceConfigIntent(BaseModel):
    change_type: Literal["interface_config"] = "interface_config"
    site: str
    targets: TargetSpec
    interface: InterfaceSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


class BgpNeighborSpec(BaseModel):
    address: str
    remote_as: int
    description: str = ""
    update_source: str | None = None
    shutdown: bool = False

    @field_validator("address")
    @classmethod
    def neighbor_address(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("neighbor address is required")
        return value


class BgpSpec(BaseModel):
    asn: int
    router_id: str | None = None
    neighbors: list[BgpNeighborSpec]

    @model_validator(mode="after")
    def has_neighbor(self) -> "BgpSpec":
        if not self.neighbors:
            raise ValueError("at least one BGP neighbor is required")
        return self


class BgpNeighborIntent(BaseModel):
    change_type: Literal["bgp_neighbor"] = "bgp_neighbor"
    site: str
    targets: TargetSpec
    bgp: BgpSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


class RoutingRedistributionSpec(BaseModel):
    from_protocol: Literal["bgp", "ospf"] = "bgp"
    to_protocol: Literal["bgp", "ospf"] = "ospf"
    target_process: str
    route_map: str
    prefix_list: str
    prefixes: list[str]
    route_tag: int

    @field_validator("target_process", "route_map", "prefix_list")
    @classmethod
    def required_safe_identifier(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("routing redistribution identifiers are required")
        if not all(character.isalnum() or character in "_.-" for character in text):
            raise ValueError("routing redistribution identifiers contain unsupported characters")
        return text

    @field_validator("prefixes")
    @classmethod
    def scoped_ipv4_prefixes(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("at least one approved prefix is required")
        normalized: list[str] = []
        for value in values:
            network = ip_network(value, strict=False)
            if network.version != 4 or network.prefixlen == 0:
                raise ValueError("redistribution prefixes must be scoped IPv4 prefixes, never a default route")
            normalized.append(str(network))
        return normalized

    @field_validator("route_tag")
    @classmethod
    def valid_route_tag(cls, value: int) -> int:
        if value < 1 or value > 4294967295:
            raise ValueError("route_tag must be between 1 and 4294967295")
        return value


class RoutingReachabilityCheck(BaseModel):
    source_device: str
    source_ip: str
    destination: str

    @field_validator("source_device")
    @classmethod
    def safe_source_device(cls, value: str) -> str:
        text = value.strip()
        if not text or not all(character.isalnum() or character in "_.-" for character in text):
            raise ValueError("reachability source_device contains unsupported characters")
        return text

    @field_validator("source_ip", "destination")
    @classmethod
    def ipv4_address(cls, value: str) -> str:
        address = ip_address(value.strip())
        if address.version != 4:
            raise ValueError("reachability checks currently require IPv4 addresses")
        return str(address)


class RoutingRedistributionIntent(BaseModel):
    change_type: Literal["routing_redistribution"] = "routing_redistribution"
    site: str
    targets: TargetSpec
    redistribution: RoutingRedistributionSpec
    reverse_redistribution: RoutingRedistributionSpec | None = None
    reachability_checks: list[RoutingReachabilityCheck] = Field(default_factory=list)
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)

    @model_validator(mode="after")
    def valid_protocol_exchange(self) -> "RoutingRedistributionIntent":
        if self.redistribution.from_protocol == self.redistribution.to_protocol:
            raise ValueError("redistribution protocols must differ")
        reverse = self.reverse_redistribution
        if reverse is not None:
            if reverse.from_protocol == reverse.to_protocol:
                raise ValueError("reverse redistribution protocols must differ")
            if (
                reverse.from_protocol != self.redistribution.to_protocol
                or reverse.to_protocol != self.redistribution.from_protocol
            ):
                raise ValueError("reverse_redistribution must reverse the primary protocol direction")
        return self


class AclRuleSpec(BaseModel):
    name: str
    sequence: int = 10
    action: Literal["permit", "deny"] = "permit"
    protocol: Literal["ip", "tcp", "udp", "icmp"] = "ip"
    source: str = "any"
    destination: str = "any"
    destination_port: str | None = None
    remark: str = ""

    @field_validator("name")
    @classmethod
    def acl_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ACL name is required")
        return value


class AclRuleIntent(BaseModel):
    change_type: Literal["acl_rule"] = "acl_rule"
    site: str
    targets: TargetSpec
    acl: AclRuleSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


class SiteDeviceSpec(BaseModel):
    device_id: str
    role: str
    platform: str
    management_ip: str
    groups: list[str] = Field(default_factory=list)
    notes: str = ""


class SiteDeviceIntent(BaseModel):
    change_type: Literal["site_device_intent"] = "site_device_intent"
    site: str
    targets: TargetSpec
    device: SiteDeviceSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


class CustomConfigSpec(BaseModel):
    """Free-form config an engineer wants to push, with an engineer-supplied rollback."""

    config_lines: str
    rollback_lines: str = ""
    verify_contains: str = ""
    description: str = ""
    acknowledge_no_rollback: bool = False

    @field_validator("config_lines")
    @classmethod
    def config_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("config_lines is required for a custom config change")
        return value


class CustomConfigIntent(BaseModel):
    change_type: Literal["custom_config"] = "custom_config"
    site: str
    targets: TargetSpec
    custom: CustomConfigSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


class NtpSpec(BaseModel):
    servers: list[str]
    prefer_first: bool = True

    @field_validator("servers")
    @classmethod
    def servers_required(cls, value: list[str]) -> list[str]:
        cleaned = [s.strip() for s in value if s and s.strip()]
        if not cleaned:
            raise ValueError("at least one NTP server is required")
        if len(cleaned) > 8:
            raise ValueError("at most 8 NTP servers")
        return list(dict.fromkeys(cleaned))


class NtpStandardizeIntent(BaseModel):
    change_type: Literal["ntp_standardize"] = "ntp_standardize"
    site: str
    targets: TargetSpec
    ntp: NtpSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


class OsUpgradeSpec(BaseModel):
    image: str
    target_version: str
    md5: str
    image_uri: str = ""
    current_version: str = ""
    rollback_image: str = ""
    maintenance_window: str
    canary_size: int = 1
    batch_size: int = 5
    verify_bgp: bool = True

    @field_validator("image", "target_version", "maintenance_window")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("OS upgrade image, target_version, and maintenance_window are required")
        return value

    @field_validator("image", "rollback_image")
    @classmethod
    def safe_image_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if any(fragment in value for fragment in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
            raise ValueError("image names cannot contain shell metacharacters")
        return value

    @field_validator("md5")
    @classmethod
    def md5_hex(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("md5 must be 32 hex characters")
        return value

    @field_validator("canary_size", "batch_size")
    @classmethod
    def positive_batch(cls, value: int) -> int:
        if value < 1:
            raise ValueError("canary_size and batch_size must be at least 1")
        return value


class OsUpgradeIntent(BaseModel):
    change_type: Literal["os_upgrade"] = "os_upgrade"
    site: str
    targets: TargetSpec
    os_upgrade: OsUpgradeSpec
    policy: PolicySpec = Field(default_factory=PolicySpec)
    metadata: IntentMetadata = Field(default_factory=IntentMetadata)


Intent = AddVlanIntent | InterfaceConfigIntent | BgpNeighborIntent | RoutingRedistributionIntent | AclRuleIntent | SiteDeviceIntent | CustomConfigIntent | NtpStandardizeIntent | OsUpgradeIntent


class CheckResult(BaseModel):
    id: str
    title: str
    status: Literal["pass", "fail"]
    severity: Literal["info", "warning", "error"] = "error"
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    status: Literal["pass", "fail"]
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return self.status == "pass"


class RenderResult(BaseModel):
    template_path: str
    config: str
    variables: dict[str, Any]


class PipelineArtifacts(BaseModel):
    intent_path: str
    rendered_path: str
    report_markdown_path: str
    report_json_path: str


class PipelineResult(BaseModel):
    status: Literal["pass", "fail"]
    intent: dict[str, Any]
    intent_yaml: str
    render: RenderResult
    validation: ValidationReport
    git: dict[str, Any]
    artifacts: PipelineArtifacts | None = None


class PhaseResult(BaseModel):
    id: str
    title: str
    status: Literal["pass", "fail", "skipped"]
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class EndToEndArtifacts(BaseModel):
    report_markdown_path: str
    report_json_path: str


class EndToEndResult(BaseModel):
    status: Literal["pass", "fail"]
    intent_path: str
    device_id: str
    apply: bool
    pipeline: PipelineResult
    phases: list[PhaseResult]
    lab: dict[str, Any] = Field(default_factory=dict)
    artifacts: EndToEndArtifacts | None = None


def load_intent(path: Path) -> Intent:
    return load_intent_data(read_yaml(path))


def load_intent_data(data: dict) -> Intent:
    """Validate a raw intent dict into its typed model via the change-type registry."""
    from netcode.change_types import spec_for  # local import avoids a cycle at module load

    return spec_for(data.get("change_type")).model.model_validate(data)
