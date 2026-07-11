"""Inventory loading and target resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netcode.models import TargetSpec
from netcode.yamlio import read_yaml


@dataclass(frozen=True)
class Device:
    id: str
    host: str
    platform: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    port: int
    hostname: str
    site: str | None
    groups: tuple[str, ...]
    role: str | None = None
    aliases: tuple[str, ...] = ()
    # Public, non-secret ownership metadata for devices controlled by a native
    # manager such as FortiManager or Panorama.
    management: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # Runner-local transport metadata. Secrets in this mapping are consumed only
    # by device adapters and are deliberately omitted from public inventory APIs.
    connection_options: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


class Inventory:
    def __init__(self, path: Path):
        self.path = path
        self.raw = read_yaml(path)
        self.defaults = self.raw.get("defaults", {})
        self.devices = [self._device(d) for d in self.raw.get("devices", [])]
        self.by_id = {d.id: d for d in self.devices}
        self.by_id_normalized = {self.normalize_id(d.id): d for d in self.devices}

    def _device(self, raw: dict[str, Any]) -> Device:
        defaults = self.defaults
        device_id = str(raw.get("id") or raw.get("hostname") or raw.get("host"))
        management = self._management(device_id, raw)
        return Device(
            id=device_id,
            hostname=str(raw.get("hostname") or device_id),
            host=str(raw.get("host")),
            platform=str(raw.get("platform") or defaults.get("platform") or "arista_eos"),
            username=str(raw.get("username") or defaults.get("username") or ""),
            password=str(raw.get("password") or defaults.get("password") or ""),
            port=int(raw.get("port") or defaults.get("port") or 22),
            site=raw.get("site"),
            groups=tuple(raw.get("groups") or []),
            role=str(raw.get("role") or "").strip() or None,
            aliases=tuple(str(item).strip() for item in (raw.get("aliases") or []) if str(item).strip()),
            management=management,
            connection_options=self._connection_options(defaults, raw),
        )

    @staticmethod
    def _management(device_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        value = raw.get("management")
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"management for {device_id} must be a mapping")
        from netcode.firewall_managers import ManagerOwnership, assert_no_secrets

        assert_no_secrets(value, f"inventory.{device_id}.management")
        ownership = ManagerOwnership.model_validate({"device_id": device_id, **value})
        return ownership.public_dict()

    @staticmethod
    def _connection_options(defaults: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        """Return only adapter transport fields from runner-local inventory.

        The inventory accepts either flat keys or grouped ``connection``/``api``
        blocks. Username/password remain first-class Device fields for existing
        SSH paths; vendor tokens and controller identifiers stay in this private
        adapter mapping and are never included by source-of-truth serializers.
        """
        allowed = {
            "transport",
            "ssh_port",
            "api_port",
            "api_token",
            "api_key",
            "organization_id",
            "network_id",
            "managed_device_id",
            "use_api",
            "use_eapi",
            "eapi_port",
            "verify_ssl",
            "vdom",
            "api_version",
            "secret",
            "manager_type",
            "manager_capabilities",
            "workspace_mode",
            "ca_bundle",
        }
        options: dict[str, Any] = {}
        for source in (defaults, defaults.get("connection"), defaults.get("api"), raw, raw.get("connection"), raw.get("api")):
            if not isinstance(source, dict):
                continue
            for key in allowed:
                if key in source and source[key] is not None:
                    options[key] = source[key]
        return options

    @staticmethod
    def normalize_id(value: str) -> str:
        return str(value or "").strip().lower()

    def find_device(self, identifier: str) -> Device | None:
        """Resolve common cross-product identifiers without requiring exact case.

        Netcode source-of-truth ids are slugged/lowercase while Rez device ids can
        preserve mixed case. The runner is the shared trust boundary, so lookup
        must be tolerant at that boundary without changing the public id stored
        for each device.
        """
        target = str(identifier or "").strip()
        if not target:
            return None
        direct = self.by_id.get(target)
        if direct:
            return direct
        normalized = self.normalize_id(target)
        by_normalized = self.by_id_normalized.get(normalized)
        if by_normalized:
            return by_normalized
        for device in self.devices:
            host_port = f"{device.host}:{device.port}"
            candidates = {
                str(device.id),
                str(device.hostname),
                str(device.host),
                host_port,
                *device.aliases,
            }
            if target in candidates or normalized in {self.normalize_id(item) for item in candidates}:
                return device
        return None

    def resolve_targets(self, target: TargetSpec, site: str | None = None) -> list[Device]:
        selected: list[Device] = []
        missing: list[str] = []
        for device_id in target.device_ids:
            device = self.find_device(device_id)
            if device:
                selected.append(device)
            else:
                missing.append(device_id)

        if target.device_group:
            selected.extend(
                d
                for d in self.devices
                if target.device_group in d.groups and (site is None or d.site == site)
            )

        deduped = list({d.id: d for d in selected}.values())
        if missing:
            raise ValueError(f"Unknown target device(s): {', '.join(missing)}")
        if not deduped:
            raise ValueError("No target devices resolved from intent")
        return deduped

    def known_subnets(self, site: str) -> list[str]:
        subnets = self.raw.get("known_subnets", {})
        values = subnets.get(site, [])
        return [str(item) for item in values]
