"""Inventory loading and target resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netcode.models import TargetSpec
from netcode.yamlio import read_yaml


@dataclass(frozen=True)
class Device:
    id: str
    host: str
    platform: str
    username: str
    password: str
    port: int
    hostname: str
    site: str | None
    groups: tuple[str, ...]


class Inventory:
    def __init__(self, path: Path):
        self.path = path
        self.raw = read_yaml(path)
        self.defaults = self.raw.get("defaults", {})
        self.devices = [self._device(d) for d in self.raw.get("devices", [])]
        self.by_id = {d.id: d for d in self.devices}

    def _device(self, raw: dict[str, Any]) -> Device:
        defaults = self.defaults
        device_id = str(raw.get("id") or raw.get("hostname") or raw.get("host"))
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
        )

    def resolve_targets(self, target: TargetSpec, site: str | None = None) -> list[Device]:
        selected: list[Device] = []
        missing: list[str] = []
        for device_id in target.device_ids:
            device = self.by_id.get(device_id)
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
