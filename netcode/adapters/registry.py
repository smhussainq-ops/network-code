"""Platform adapter registry.

The execution adapter remains intentionally small for the first workflow, while
state collection and multi-vendor inventory support come from Rez drivers.
"""

from __future__ import annotations

from netcode.adapters.rez import READ_TRANSPORTS, RezAdapterBridge
from netcode.inventory import Device


class AdapterRegistry:
    """Registry for execution and state adapters."""

    EXECUTION_ADAPTERS = {
        "arista_eos": {
            "name": "netcode.arista_config_session",
            "capabilities": ["dry_run", "diff", "apply", "rollback", "verify"],
            "status": "implemented_lab",
            "write_supported": True,
            "safe_write_model": "EOS config session with abortable dry-run and explicit commit",
            "production_ready": False,
        },
        "cisco_ios": {
            "name": "netcode.cisco_ios_execution_stub",
            "capabilities": [],
            "status": "planned_stub",
            "write_supported": False,
            "safe_write_model": "requires adapter SDK implementation",
            "production_ready": False,
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
