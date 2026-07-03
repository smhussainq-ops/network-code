"""Execution adapter SDK contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from netcode.inventory import Device
from netcode.models import Intent, RenderResult

ExecutionAction = Literal["dry-run", "apply", "rollback"]


@dataclass(frozen=True)
class ExecutionAdapterMetadata:
    name: str
    platform: str
    capabilities: list[str]
    safe_write_model: str
    production_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "platform": self.platform,
            "capabilities": self.capabilities,
            "safe_write_model": self.safe_write_model,
            "production_ready": self.production_ready,
        }


@dataclass
class ExecutionResult:
    status: Literal["pass", "fail"]
    action: str
    device_id: str
    message: str
    session_name: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "device_id": self.device_id,
            "message": self.message,
            "session_name": self.session_name,
            "evidence": self.evidence,
        }


class ExecutionAdapter(ABC):
    """Controlled write-path contract for vendor adapters."""

    metadata: ExecutionAdapterMetadata

    def __init__(self, device: Device):
        self.device = device

    @abstractmethod
    def dry_run(self, intent: Intent, render: RenderResult) -> ExecutionResult:
        """Prove candidate acceptance without committing it."""

    @abstractmethod
    def apply(self, intent: Intent, render: RenderResult) -> ExecutionResult:
        """Apply a validated candidate and verify the target state."""

    @abstractmethod
    def rollback(self, intent: Intent, render: RenderResult) -> ExecutionResult:
        """Apply a compensating rollback and verify rollback state."""

    def capabilities(self) -> dict[str, Any]:
        return self.metadata.as_dict()

