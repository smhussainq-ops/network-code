"""Intent and result models."""

from __future__ import annotations

from ipaddress import ip_network
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


Intent = AddVlanIntent | InterfaceConfigIntent | BgpNeighborIntent | AclRuleIntent | SiteDeviceIntent


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
    data = read_yaml(path)
    change_type = data.get("change_type")
    if change_type == "add_vlan":
        return AddVlanIntent.model_validate(data)
    if change_type == "interface_config":
        return InterfaceConfigIntent.model_validate(data)
    if change_type == "bgp_neighbor":
        return BgpNeighborIntent.model_validate(data)
    if change_type == "acl_rule":
        return AclRuleIntent.model_validate(data)
    if change_type == "site_device_intent":
        return SiteDeviceIntent.model_validate(data)
    raise ValueError(f"Unsupported change_type: {change_type!r}")
