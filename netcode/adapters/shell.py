"""Vendor-aware persistent SSH adapter for the human Netcode Shell.

This is intentionally separate from execution adapters. The Shell represents an
attributed human SSH session; unattended writes still require Netcode's plan,
validation, approval, apply, verify, and rollback workflow.
"""

from __future__ import annotations

from typing import Any

from netcode.inventory import Device


NETMIKO_PLATFORM_MAP = {
    "arista": "arista_eos",
    "eos": "arista_eos",
    "arista_eos": "arista_eos",
    "cisco": "cisco_ios",
    "ios": "cisco_ios",
    "iosxe": "cisco_ios",
    "ios-xe": "cisco_ios",
    "cisco_ios": "cisco_ios",
    "cisco_xe": "cisco_ios",
    "nxos": "cisco_nxos",
    "nx-os": "cisco_nxos",
    "cisco_nxos": "cisco_nxos",
    "asa": "cisco_asa",
    "cisco_asa": "cisco_asa",
    "junos": "juniper_junos",
    "juniper": "juniper_junos",
    "juniper_junos": "juniper_junos",
    "fortigate": "fortinet",
    "fortios": "fortinet",
    "fortinet": "fortinet",
    "paloalto": "paloalto_panos",
    "palo_alto": "paloalto_panos",
    "panos": "paloalto_panos",
    "paloalto_panos": "paloalto_panos",
    "aruba": "aruba_aoscx",
    "aoscx": "aruba_aoscx",
    "aruba_aoscx": "aruba_aoscx",
    "nokia": "nokia_srl",
    "srl": "nokia_srl",
    "nokia_srl": "nokia_srl",
}


def netmiko_device_type(platform: str) -> str:
    normalized = str(platform or "").strip().lower().replace(" ", "_")
    device_type = NETMIKO_PLATFORM_MAP.get(normalized)
    if not device_type:
        raise ValueError(
            f"Platform {platform!r} has no interactive SSH adapter. "
            "API-only controllers such as Meraki Dashboard and Cisco SD-WAN use live state/API queries instead."
        )
    return device_type


def ssh_port_for(device: Device) -> int:
    value = (device.connection_options or {}).get("ssh_port", device.port)
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = int(device.port or 22)
    return port if 1 <= port <= 65535 else 22


class NetmikoShellAdapter:
    """Persistent line-oriented SSH session across Shell HTTP requests."""

    def __init__(self, device: Device, timeout: int = 45):
        self.device = device
        self.timeout = timeout
        self._conn: Any = None

    def connect(self) -> None:
        try:
            from netmiko import ConnectHandler
        except Exception as exc:  # pragma: no cover - dependency failure
            raise RuntimeError(f"netmiko is required for Netcode Shell: {exc}") from exc

        params: dict[str, Any] = {
            "device_type": netmiko_device_type(self.device.platform),
            "host": self.device.host,
            "username": self.device.username,
            "password": self.device.password,
            "port": ssh_port_for(self.device),
            "fast_cli": False,
            "conn_timeout": self.timeout,
            "auth_timeout": self.timeout,
            "banner_timeout": self.timeout,
        }
        secret = (self.device.connection_options or {}).get("secret")
        if secret:
            params["secret"] = str(secret)
        self._conn = ConnectHandler(**params)
        try:
            self._conn.enable()
        except Exception:
            # Junos, FortiOS, PAN-OS, and user-scoped accounts may not expose an
            # IOS-style enable mode. The interactive session remains usable.
            pass

    def disconnect(self) -> None:
        if self._conn is not None:
            self._conn.disconnect()
            self._conn = None

    def show(self, command: str) -> str:
        """Send one line while preserving the device's current CLI mode."""
        if self._conn is None:
            raise RuntimeError("Shell adapter is not connected")
        return self._conn.send_command_timing(
            command,
            strip_prompt=False,
            strip_command=False,
            read_timeout=self.timeout,
            delay_factor=1,
        )
