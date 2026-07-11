"""Typed firewall-manager contracts and fail-closed capability checks.

The control plane may describe manager ownership and reviewed intent, but it
never receives manager credentials. Execution resolves those credentials from
the customer-side runner inventory.
"""

from __future__ import annotations

import hashlib
import json
import re
from ipaddress import ip_address, ip_network
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ManagerType = Literal["fortimanager", "panorama"]
ManagerAction = Literal[
    "probe",
    "snapshot",
    "preview",
    "validate",
    "lock",
    "stage",
    "deploy",
    "poll",
    "verify",
    "discard",
    "unlock",
    "rollback",
]

READ_ACTIONS = frozenset({"probe", "snapshot", "preview", "validate", "poll", "verify"})
WRITE_ACTIONS = frozenset({"lock", "stage", "deploy", "discard", "unlock", "rollback"})

_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_token",
        "api_key",
        "private_key",
        "client_secret",
        "authorization",
    }
)


def _safe_name(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if not re.fullmatch(r"[A-Za-z0-9_.:/ -]+", text):
        raise ValueError(f"{field_name} contains unsupported characters")
    return text


class ManagerScope(BaseModel):
    adom: str | None = None
    policy_package: str | None = None
    vdom: str | None = None
    install_target: str | None = None
    device_group: str | None = None
    template_stack: str | None = None
    vsys: str | None = None
    rulebase: Literal["pre", "post"] | None = None

    @field_validator("adom", "policy_package", "vdom", "install_target", "device_group", "template_stack", "vsys")
    @classmethod
    def safe_scope_value(cls, value: str | None, info) -> str | None:  # noqa: ANN001
        return _safe_name(value, info.field_name) if value is not None else None

    def validate_for(self, manager_type: ManagerType) -> None:
        if manager_type == "fortimanager":
            missing = [name for name in ("adom", "policy_package", "vdom", "install_target") if not getattr(self, name)]
        else:
            missing = [name for name in ("device_group", "template_stack", "vsys", "rulebase") if not getattr(self, name)]
        if missing:
            raise ValueError(f"{manager_type} ownership is missing scope field(s): {', '.join(missing)}")


class ManagerOwnership(BaseModel):
    device_id: str
    management_mode: Literal["manager"] = "manager"
    manager_id: str
    manager_type: ManagerType
    scope: ManagerScope
    managed_serial: str

    @field_validator("device_id", "manager_id", "managed_serial")
    @classmethod
    def safe_identifier(cls, value: str, info) -> str:  # noqa: ANN001
        return _safe_name(value, info.field_name)

    @model_validator(mode="after")
    def complete_scope(self) -> "ManagerOwnership":
        self.scope.validate_for(self.manager_type)
        return self

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ManagerCapabilities(BaseModel):
    manager_type: ManagerType
    version: str
    read: bool = True
    workspace_lock: bool = False
    snapshot: bool = False
    preview: bool = False
    validation: bool = False
    scoped_push: bool = False
    task_poll: bool = False
    rollback: bool = False
    candidate_filter: bool = False
    partial_install: bool = False
    source: Literal["live_probe", "configured"] = "live_probe"

    @field_validator("version")
    @classmethod
    def version_required(cls, value: str) -> str:
        return _safe_name(value, "version")

    def blockers_for(self, action: ManagerAction) -> list[str]:
        required: dict[str, tuple[str, ...]] = {
            "probe": ("read",),
            "snapshot": ("read", "snapshot"),
            "preview": ("read", "preview"),
            "validate": ("read", "validation"),
            "lock": ("workspace_lock",),
            "stage": ("workspace_lock", "snapshot", "validation", "candidate_filter"),
            "deploy": ("workspace_lock", "validation", "scoped_push", "task_poll"),
            "poll": ("task_poll",),
            "verify": ("read",),
            "discard": ("workspace_lock", "candidate_filter"),
            "unlock": ("workspace_lock",),
            "rollback": ("workspace_lock", "snapshot", "scoped_push", "task_poll", "rollback"),
        }
        return [name for name in required[action] if not bool(getattr(self, name))]

    def require(self, action: ManagerAction) -> None:
        blockers = self.blockers_for(action)
        if blockers:
            raise ValueError(
                f"{self.manager_type} {self.version} cannot run {action}; "
                f"live capability probe did not prove: {', '.join(blockers)}"
            )


def capabilities_from_probe(manager_type: ManagerType, version: str, advertised: dict[str, Any]) -> ManagerCapabilities:
    """Normalize a live manager probe without inferring write support from version.

    Version ranges are useful for operator messaging, but only explicitly
    advertised/probed capabilities can unlock a manager write.
    """
    allowed = {
        "read",
        "workspace_lock",
        "snapshot",
        "preview",
        "validate",
        "scoped_push",
        "task_poll",
        "rollback",
        "candidate_filter",
        "partial_install",
    }
    values = {key: bool(advertised.get(key, False)) for key in allowed if key != "validate"}
    values["validation"] = bool(advertised.get("validate", advertised.get("validation", False)))
    return ManagerCapabilities(manager_type=manager_type, version=version, source="live_probe", **values)


class ApplicationFlow(BaseModel):
    source_site: str
    source_device: str
    source_ip: str
    destination_ip: str
    protocol: Literal["tcp", "udp", "icmp"]
    destination_port: int | None = None
    expected_route_owner: str | None = None
    expected_sdwan_class: str | None = None
    expected_firewall_action: Literal["allow", "deny"] = "allow"
    expected_nat: Literal["none", "snat", "dnat"] = "none"
    expected_application_result: Literal["tcp_connect", "http_status", "icmp_reply"] = "tcp_connect"

    @field_validator("source_site", "source_device")
    @classmethod
    def safe_flow_identifier(cls, value: str, info) -> str:  # noqa: ANN001
        return _safe_name(value, info.field_name)

    @field_validator("source_ip", "destination_ip")
    @classmethod
    def valid_ip(cls, value: str) -> str:
        return str(ip_address(value.strip()))

    @field_validator("destination_port")
    @classmethod
    def valid_port(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 65535:
            raise ValueError("destination_port must be between 1 and 65535")
        return value

    @model_validator(mode="after")
    def port_matches_protocol(self) -> "ApplicationFlow":
        if self.protocol in {"tcp", "udp"} and self.destination_port is None:
            raise ValueError("TCP/UDP flows require destination_port")
        if self.protocol == "icmp" and self.destination_port is not None:
            raise ValueError("ICMP flows cannot carry destination_port")
        return self


class FirewallObjectRef(BaseModel):
    name: str
    value: str
    kind: Literal["address", "service", "application"]
    create_if_missing: bool = False

    @field_validator("name")
    @classmethod
    def safe_object_name(cls, value: str) -> str:
        return _safe_name(value, "object name")

    @field_validator("value")
    @classmethod
    def valid_object_value(cls, value: str, info) -> str:  # noqa: ANN001
        text = value.strip()
        if info.data.get("kind") == "address":
            ip_network(text, strict=False)
        elif not text:
            raise ValueError("object value is required")
        return text


class FirewallPolicyChange(BaseModel):
    change_type: Literal["firewall_policy_change"] = "firewall_policy_change"
    name: str
    ownership: ManagerOwnership
    source_zones: list[str]
    destination_zones: list[str]
    source_objects: list[FirewallObjectRef]
    destination_objects: list[FirewallObjectRef]
    services: list[FirewallObjectRef]
    applications: list[FirewallObjectRef] = Field(default_factory=list)
    action: Literal["allow", "deny"]
    log: bool = True
    security_profiles: list[str] = Field(default_factory=list)
    insertion: dict[str, str]
    target_device_ids: list[str]
    ticket_id: str
    expires_at: str | None = None

    @field_validator("name", "ticket_id")
    @classmethod
    def safe_policy_text(cls, value: str, info) -> str:  # noqa: ANN001
        return _safe_name(value, info.field_name)

    @field_validator("source_zones", "destination_zones", "target_device_ids")
    @classmethod
    def non_empty_scope(cls, values: list[str], info) -> list[str]:  # noqa: ANN001
        cleaned = [_safe_name(value, info.field_name) for value in values]
        if not cleaned:
            raise ValueError(f"{info.field_name} cannot be empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(f"{info.field_name} contains duplicates")
        return cleaned

    @model_validator(mode="after")
    def exact_targets(self) -> "FirewallPolicyChange":
        if self.ownership.device_id not in self.target_device_ids:
            raise ValueError("ownership device must be included in target_device_ids")
        if not self.source_objects or not self.destination_objects or not self.services:
            raise ValueError("policy requires resolved source, destination, and service objects")
        if set(self.insertion) != {"position", "reference_rule"}:
            raise ValueError("insertion must contain exact position and reference_rule")
        if self.insertion["position"] not in {"before", "after"}:
            raise ValueError("insertion position must be before or after")
        _safe_name(self.insertion["reference_rule"], "reference_rule")
        return self


class FirewallNatChange(BaseModel):
    change_type: Literal["firewall_nat_change"] = "firewall_nat_change"
    name: str
    ownership: ManagerOwnership
    nat_type: Literal["snat", "dnat"]
    original_source: str
    original_destination: str
    translated_source: str | None = None
    translated_destination: str | None = None
    service: str
    target_device_ids: list[str]
    ticket_id: str

    @field_validator("original_source", "original_destination", "translated_source", "translated_destination")
    @classmethod
    def valid_network(cls, value: str | None) -> str | None:
        return str(ip_network(value, strict=False)) if value is not None else None

    @model_validator(mode="after")
    def translation_present(self) -> "FirewallNatChange":
        if self.ownership.device_id not in self.target_device_ids:
            raise ValueError("ownership device must be included in target_device_ids")
        if self.nat_type == "snat" and not self.translated_source:
            raise ValueError("SNAT requires translated_source")
        if self.nat_type == "dnat" and not self.translated_destination:
            raise ValueError("DNAT requires translated_destination")
        return self


class ApprovalProof(BaseModel):
    approved: bool = False
    requested_by: str
    approved_by: str | None = None
    workflow_state: str

    def require_for_write(self) -> None:
        if not self.approved or self.workflow_state != "approved" or not self.approved_by:
            raise ValueError("manager write requires an approved workflow and a named second engineer")
        if self.requested_by.strip().lower() == self.approved_by.strip().lower():
            raise ValueError("requester cannot approve their own manager write")


class ManagerJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ManagerAction
    operation_id: str
    change_id: str
    manager_id: str
    ownership: ManagerOwnership
    capabilities: ManagerCapabilities
    policy_change: FirewallPolicyChange | None = None
    nat_change: FirewallNatChange | None = None
    flow: ApplicationFlow | None = None
    approval: ApprovalProof
    expected_candidate_owner: str
    expected_candidate_location: str
    unrelated_candidate_changes: list[dict[str, Any]] = Field(default_factory=list)
    manager_task_id: str | None = None
    pre_change_revision: str | None = None

    @model_validator(mode="before")
    @classmethod
    def raw_payload_has_no_credentials(cls, value: Any) -> Any:
        assert_no_secrets(value)
        return value

    @field_validator("operation_id", "change_id", "manager_id", "expected_candidate_owner", "expected_candidate_location")
    @classmethod
    def safe_job_identifier(cls, value: str, info) -> str:  # noqa: ANN001
        return _safe_name(value, info.field_name)

    @model_validator(mode="after")
    def fail_closed(self) -> "ManagerJobRequest":
        if self.manager_id != self.ownership.manager_id:
            raise ValueError("manager_id does not match ownership")
        if self.capabilities.manager_type != self.ownership.manager_type:
            raise ValueError("capability manager type does not match ownership")
        self.capabilities.require(self.action)
        if self.action in WRITE_ACTIONS:
            self.approval.require_for_write()
        if self.action in {"stage", "deploy", "rollback"} and not (self.policy_change or self.nat_change):
            raise ValueError(f"{self.action} requires typed firewall intent")
        if self.action == "poll" and not self.manager_task_id:
            raise ValueError("poll requires manager_task_id")
        if self.action == "rollback" and not self.pre_change_revision:
            raise ValueError("rollback requires pre_change_revision")
        if self.unrelated_candidate_changes:
            raise ValueError("manager candidate contains unrelated changes; isolate or clear them before execution")
        assert_no_secrets(self.model_dump())
        return self

    @property
    def idempotency_key(self) -> str:
        material = json.dumps(self.model_dump(exclude={"approval"}), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def assert_no_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _SECRET_KEYS or any(fragment in normalized for fragment in ("password", "secret", "token", "private_key")):
                raise ValueError(f"credential-shaped field is forbidden in control-plane manager payload: {path}.{key}")
            assert_no_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_secrets(child, f"{path}[{index}]")


def validate_unique_ownership(records: list[ManagerOwnership]) -> dict[str, ManagerOwnership]:
    by_device: dict[str, ManagerOwnership] = {}
    for record in records:
        key = record.device_id.strip().lower()
        existing = by_device.get(key)
        if existing and existing.model_dump() != record.model_dump():
            raise ValueError(f"managed firewall {record.device_id} has conflicting manager ownership")
        by_device[key] = record
    return by_device
