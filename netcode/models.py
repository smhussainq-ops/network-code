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


Intent = AddVlanIntent


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
    raise ValueError(f"Unsupported change_type: {change_type!r}")
