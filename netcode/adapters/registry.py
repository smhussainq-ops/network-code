"""Platform adapter registry.

The execution adapter remains intentionally small for the first workflow, while
state collection and multi-vendor inventory support come from Rez drivers.
"""

from __future__ import annotations

from netcode.adapters.rez import READ_TRANSPORTS, RezAdapterBridge
from netcode.inventory import Device


class AdapterRegistry:
    """Registry for execution and state adapters."""

    PLATFORM_ALIASES = {
        "arista": "arista_eos",
        "eos": "arista_eos",
        "ios": "cisco_ios",
        "iosxe": "cisco_ios",
        "ios_xe": "cisco_ios",
        "cisco_xe": "cisco_ios",
        "cisco_iosxe": "cisco_ios",
        "cisco_ios_xe": "cisco_ios",
    }

    EXECUTION_ADAPTERS = {
        "arista_eos": {
            "name": "netcode.arista_config_session",
            "capabilities": ["dry_run", "diff", "apply", "rollback", "verify"],
            "status": "implemented_lab",
            "write_supported": True,
            "safe_write_model": "EOS config session with abortable dry-run and explicit commit",
            "production_ready": False,
            "supported_change_types": [
                "add_vlan",
                "interface_config",
                "bgp_neighbor",
                "routing_redistribution",
                "acl_rule",
                "custom_config",
                "ntp_standardize",
                "os_upgrade",
            ],
        },
        "cisco_ios": {
            "name": "netcode.cisco_ios_ntp",
            "capabilities": ["dry_run", "diff", "apply", "rollback", "verify"],
            "status": "contract_tested",
            "write_supported": True,
            "safe_write_model": "offline validation, first-device proof, verify-before-save, exact pre-change rollback",
            "production_ready": False,
            "supported_change_types": ["ntp_standardize"],
        },
        "cisco_nxos": {
            "name": "netcode.cisco_nxos_execution_stub",
            "capabilities": [],
            "status": "planned_stub",
            "write_supported": False,
            "safe_write_model": "requires adapter SDK implementation",
            "production_ready": False,
        },
        "juniper_junos": {
            "name": "netcode.juniper_junos_execution_stub",
            "capabilities": [],
            "status": "planned_stub",
            "write_supported": False,
            "safe_write_model": "requires adapter SDK implementation",
            "production_ready": False,
        },
        "fortinet": {
            "name": "netcode.fortinet_execution_stub",
            "capabilities": [],
            "status": "planned_stub",
            "write_supported": False,
            "safe_write_model": "requires policy adapter implementation",
            "production_ready": False,
        },
        "palo_alto": {
            "name": "netcode.palo_alto_execution_stub",
            "capabilities": [],
            "status": "planned_stub",
            "write_supported": False,
            "safe_write_model": "requires policy adapter implementation",
            "production_ready": False,
        }
    }

    def __init__(self, rez: RezAdapterBridge | None = None):
        self.rez = rez or RezAdapterBridge()

    @classmethod
    def normalize_execution_platform(cls, platform: str) -> str:
        normalized = str(platform or "").strip().lower().replace("-", "_").replace(" ", "_")
        return cls.PLATFORM_ALIASES.get(normalized, normalized or "unknown")

    @classmethod
    def execution_support(cls, platform: str, change_type: str) -> dict[str, object]:
        normalized = cls.normalize_execution_platform(platform)
        adapter = cls.EXECUTION_ADAPTERS.get(normalized)
        supported = set(adapter.get("supported_change_types", [])) if adapter else set()
        return {
            "platform": normalized,
            "change_type": str(change_type or "").strip(),
            "adapter": adapter,
            "supported": bool(adapter and adapter.get("write_supported") and change_type in supported),
            "supported_change_types": sorted(supported),
        }

    @classmethod
    def require_execution_support(cls, platform: str, change_type: str) -> dict[str, object]:
        support = cls.execution_support(platform, change_type)
        if support["supported"]:
            return support
        supported = support["supported_change_types"]
        detail = ", ".join(supported) if supported else "none"
        raise ValueError(
            f"{support['platform']} does not support governed '{change_type}' execution. "
            f"Supported change types: {detail}."
        )

    def summary(self) -> dict[str, object]:
        rez_summary = self.rez.summary()
        return {
            "execution_adapters": self.EXECUTION_ADAPTERS,
            "adapter_matrix": self.adapter_matrix(),
            "conformance": self.conformance(),
            "state_adapters": {
                "rez": rez_summary,
            },
        }

    def adapter_matrix(self) -> list[dict[str, object]]:
        rez_summary = self.rez.summary()
        rez_platforms = set(rez_summary.get("platforms", [])) if rez_summary.get("available") else set()
        platforms = sorted(set(self.EXECUTION_ADAPTERS) | rez_platforms)
        return [
            {
                "platform": platform,
                "read_provider": "rez" if platform in rez_platforms else None,
                "read_supported": platform in rez_platforms,
                "read_transports": list(READ_TRANSPORTS.get(platform, ("ssh",))) if platform in rez_platforms else [],
                "shell_supported": platform in rez_platforms and "ssh" in READ_TRANSPORTS.get(platform, ("ssh",)),
                "write_adapter": self.EXECUTION_ADAPTERS.get(platform),
                "write_supported": bool(self.EXECUTION_ADAPTERS.get(platform, {}).get("write_supported")),
            }
            for platform in platforms
        ]

    def device_capabilities(self, device: Device) -> dict[str, object]:
        rez_summary = self.rez.summary()
        rez_platforms = set(rez_summary.get("platforms", [])) if rez_summary.get("available") else set()
        platform = self.rez.normalize_platform(device.platform)
        read_transports = list(READ_TRANSPORTS.get(platform, ("ssh",)))
        return {
            "device_id": device.id,
            "platform": platform,
            "execution": self.EXECUTION_ADAPTERS.get(platform),
            "shell": {
                "supported": "ssh" in read_transports,
                "transport": "ssh" if "ssh" in read_transports else None,
            },
            "state": {
                "provider": "rez",
                "available": bool(rez_summary.get("available")),
                "supported": platform in rez_platforms,
                "transports": read_transports,
                "rez_root": rez_summary.get("root"),
                "error": rez_summary.get("error"),
            },
        }

    def conformance(self) -> list[dict[str, object]]:
        rez_platforms = set(self.rez.summary().get("platforms", []))
        required_write = {"dry_run", "diff", "apply", "rollback", "verify"}
        platforms = sorted(set(self.EXECUTION_ADAPTERS) | rez_platforms)
        rows: list[dict[str, object]] = []
        for platform in platforms:
            write = self.EXECUTION_ADAPTERS.get(platform)
            write_capabilities = set(write.get("capabilities", [])) if write else set()
            write_missing = sorted(required_write - write_capabilities) if write and write.get("write_supported") else sorted(required_write)
            read_supported = platform in rez_platforms
            write_supported = bool(write and write.get("write_supported"))
            if read_supported and write_supported and not write_missing:
                status = "pass"
            elif read_supported or write_supported:
                status = "partial"
            else:
                status = "planned"
            rows.append(
                {
                    "platform": platform,
                    "status": status,
                    "read_contract": {
                        "provider": "rez" if read_supported else None,
                        "required": ["connect", "disconnect", "get_full_state"],
                        "supported": read_supported,
                    },
                    "write_contract": {
                        "adapter": write.get("name") if write else None,
                        "supported": write_supported,
                        "required": sorted(required_write),
                        "capabilities": sorted(write_capabilities),
                        "missing": write_missing,
                        "production_ready": bool(write and write.get("production_ready")),
                    },
                }
            )
        return rows
