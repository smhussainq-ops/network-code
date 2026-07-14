"""Bridge to Rez driver adapters.

This module treats Rez as an external adapter provider. It does not modify the
Rez repository; it imports the driver registry when available and degrades
cleanly when Rez dependencies are missing.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any

from netcode.inventory import Device

PLATFORM_ALIASES = {
    "arista": "arista_eos",
    "eos": "arista_eos",
    "cisco": "cisco_ios",
    "ios": "cisco_ios",
    "iosxe": "cisco_ios",
    "ios-xe": "cisco_ios",
    "nxos": "cisco_nxos",
    "nx-os": "cisco_nxos",
    "asa": "cisco_asa",
    "junos": "juniper_junos",
    "juniper": "juniper_junos",
    "fortigate": "fortinet",
    "fortios": "fortinet",
    "paloalto": "palo_alto",
    "palo-alto": "palo_alto",
    "aruba": "aruba_aoscx",
    "aoscx": "aruba_aoscx",
    "srl": "nokia_srl",
    "nokia": "nokia_srl",
}

READ_TRANSPORTS: dict[str, tuple[str, ...]] = {
    "arista_eos": ("ssh", "api"),
    "cisco_ios": ("ssh",),
    "cisco_nxos": ("ssh",),
    "cisco_asa": ("ssh",),
    "juniper_junos": ("ssh",),
    "nokia_srl": ("ssh",),
    "fortinet": ("ssh", "api"),
    "palo_alto": ("ssh", "api"),
    "aruba_aoscx": ("api",),
    "cisco_sdwan": ("api",),
    "meraki": ("api",),
    "fortimanager": ("api",),
    "panorama": ("api",),
}


class RezAdapterBridge:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or self._default_root()).expanduser()
        self._driver_map: dict[str, Any] | None = None
        self._error: str | None = None

    def _default_root(self) -> Path:
        candidates = [
            os.environ.get("NETCODE_REZ_ROOT"),
            "/Users/syedhussain/Dev/Claude/resonance-core",
            "/home/syedhussain/resonance-core",
            "/Users/syedhussain/Dev/Prod/resonance-core",
            "/Users/syedhussain/resonance-core",
        ]
        for candidate in candidates:
            if candidate and (Path(candidate) / "drivers" / "collector.py").is_file():
                return Path(candidate)
        return Path("/Users/syedhussain/Dev/Claude/resonance-core")

    def _load_driver_map(self) -> dict[str, Any]:
        if self._driver_map is not None:
            return self._driver_map
        if not self.root.exists():
            self._error = f"Rez root not found: {self.root}"
            self._driver_map = {}
            return self._driver_map
        root_str = str(self.root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        try:
            self._clear_stale_driver_modules()
            collector = importlib.import_module("drivers.collector")
            self._driver_map = dict(getattr(collector, "DRIVER_MAP"))
            self._error = None
        except Exception as exc:
            self._driver_map = {}
            self._error = f"{type(exc).__name__}: {exc}"
        return self._driver_map

    def driver_map(self) -> dict[str, Any]:
        """Return the loaded Rez platform driver registry."""
        return self._load_driver_map()

    def supported_platforms(self) -> list[str]:
        return sorted(self._load_driver_map().keys())

    def normalize_platform(self, value: str | None) -> str:
        platform = (value or "").strip().lower().replace(" ", "_")
        if not platform or platform in {"auto", "autodetect", "detect"}:
            return ""
        return PLATFORM_ALIASES.get(platform, platform)

    def _clear_stale_driver_modules(self) -> None:
        """Avoid reusing a previously imported Rez drivers package from another root."""
        root = self.root.resolve()
        for module_name in ("drivers.configured_state", "drivers.collector", "drivers"):
            module = sys.modules.get(module_name)
            module_file = getattr(module, "__file__", None) if module else None
            if not module_file:
                continue
            try:
                module_path = Path(module_file).resolve()
            except OSError:
                continue
            if not module_path.is_relative_to(root):
                sys.modules.pop(module_name, None)

    def summary(self) -> dict[str, object]:
        driver_map = self._load_driver_map()
        return {
            "available": bool(driver_map),
            "root": str(self.root),
            "platforms": sorted(driver_map.keys()),
            "platform_count": len(driver_map),
            "error": self._error,
        }

    def health(self) -> dict[str, object]:
        driver_map = self._load_driver_map()
        return {
            "ok": bool(driver_map),
            "provider": "rez",
            "root": str(self.root),
            "driver_registry": "drivers.collector.DRIVER_MAP",
            "platform_count": len(driver_map),
            "platforms": sorted(driver_map.keys()),
            "error": self._error,
        }

    def platforms(self) -> dict[str, object]:
        driver_map = self._load_driver_map()
        return {
            "ok": bool(driver_map),
            "provider": "rez",
            "platforms": [
                {
                    "platform": platform,
                    "driver": f"{driver_cls.__module__}.{driver_cls.__name__}",
                    "capabilities": ["connect", "disconnect", "get_full_state"],
                    "read_transports": list(READ_TRANSPORTS.get(platform, ("ssh",))),
                }
                for platform, driver_cls in sorted(driver_map.items())
            ],
            "error": self._error,
        }

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _as_port(value: Any, default: int) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return default
        return port if 1 <= port <= 65535 else default

    def _driver_kwargs(self, device: Device, platform: str) -> dict[str, Any]:
        """Build the real constructor contract for one Rez vendor driver.

        Netcode's legacy bridge passed ``device.port`` as the fourth positional
        argument for every driver. That silently treated forwarded SSH ports as
        HTTPS ports on FortiGate/PAN-OS and discarded API tokens/controller IDs.
        The runner-local inventory now carries those fields explicitly.
        """
        options = dict(device.connection_options or {})
        transport = str(options.get("transport") or "auto").strip().lower()
        raw_port = self._as_port(device.port, 22)
        explicit_api_port = options.get("api_port")
        explicit_ssh_port = options.get("ssh_port")
        api_port = self._as_port(
            explicit_api_port,
            raw_port if transport == "api" or (raw_port != 22 and raw_port in {443, 8443}) else 443,
        )
        ssh_port = self._as_port(
            explicit_ssh_port,
            raw_port if transport != "api" and raw_port not in {443, 8443} else 22,
        )
        common: dict[str, Any] = {
            "hostname": device.host,
            "username": device.username,
            "password": device.password,
        }

        if platform == "fortinet":
            api_hint = bool(options.get("api_token") or explicit_api_port is not None or raw_port in {443, 8443})
            use_api = self._as_bool(options.get("use_api"), transport == "api" or (transport == "auto" and api_hint))
            return {
                **common,
                "port": api_port,
                "ssh_port": ssh_port,
                "api_token": options.get("api_token"),
                "use_api": use_api,
                "verify_ssl": self._as_bool(options.get("verify_ssl"), False),
                "vdom": str(options.get("vdom") or "root"),
            }
        if platform == "palo_alto":
            api_hint = explicit_api_port is not None or raw_port in {443, 8443}
            use_api = self._as_bool(options.get("use_api"), transport == "api" or (transport == "auto" and api_hint))
            return {
                **common,
                "port": api_port,
                "ssh_port": ssh_port,
                "use_api": use_api,
                "verify_ssl": self._as_bool(options.get("verify_ssl"), False),
            }
        if platform == "arista_eos":
            return {
                **common,
                "port": ssh_port,
                "eapi_port": self._as_port(options.get("eapi_port") or explicit_api_port, 443),
                "use_eapi": self._as_bool(options.get("use_eapi"), transport == "api"),
            }
        if platform == "meraki":
            return {
                **common,
                "port": api_port,
                "api_key": options.get("api_key"),
                "organization_id": options.get("organization_id"),
                "network_id": options.get("network_id"),
            }
        if platform == "cisco_sdwan":
            return {
                **common,
                "port": api_port,
                "device_id": options.get("managed_device_id"),
            }
        if platform == "aruba_aoscx":
            return {
                **common,
                "port": api_port,
                "verify_ssl": self._as_bool(options.get("verify_ssl"), False),
                "api_version": options.get("api_version"),
            }
        return {**common, "port": ssh_port}

    def build_driver(self, device: Device) -> tuple[str, Any]:
        """Instantiate the normalized Rez driver for a runner-local device."""
        driver_map = self._load_driver_map()
        platform = self.normalize_platform(device.platform)
        driver_cls = driver_map.get(platform)
        if not driver_cls:
            raise ValueError(f"Rez has no driver for platform {device.platform}")
        kwargs = self._driver_kwargs(device, platform)
        signature = inspect.signature(driver_cls)
        parameters = signature.parameters
        accepts_any = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        if "hostname" not in parameters and "host" in parameters:
            kwargs["host"] = kwargs.pop("hostname")
        if not accepts_any:
            kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        driver = driver_cls(**kwargs)
        # Privileged EXEC is required by several CLI platforms to read the
        # running configuration. The secret remains in runner-local inventory;
        # it is attached only to the in-process driver and is never serialized.
        if platform in {"arista_eos", "cisco_ios", "cisco_nxos", "cisco_asa"}:
            options = dict(device.connection_options or {})
            setattr(driver, "enable_secret", str(options.get("secret") or device.password or ""))
        return platform, driver

    async def collect_device_state_async(self, device: Device) -> dict[str, object]:
        started = time.perf_counter()
        driver_map = self._load_driver_map()
        if not driver_map:
            return {
                "ok": False,
                "device_id": device.id,
                "platform": device.platform,
                "adapter": "rez",
                "state": None,
                "warnings": [],
                "errors": [self._error or "Rez drivers unavailable"],
                "collection_time": round(time.perf_counter() - started, 3),
                "error": self._error or "Rez drivers unavailable",
            }
        platform = self.normalize_platform(device.platform)
        driver_cls = driver_map.get(platform)
        if not driver_cls:
            error = f"Rez has no driver for platform {device.platform}"
            return {
                "ok": False,
                "device_id": device.id,
                "platform": device.platform,
                "adapter": "rez",
                "state": None,
                "warnings": [],
                "errors": [error],
                "collection_time": round(time.perf_counter() - started, 3),
                "error": error,
                "supported_platforms": sorted(driver_map.keys()),
            }

        _, driver = self.build_driver(device)
        try:
            await driver.connect()
            state = await driver.get_full_state()
            if hasattr(state, "model_dump"):
                state_payload = state.model_dump()
            elif hasattr(state, "dict"):
                state_payload = state.dict()
            else:
                state_payload = state
            configuration_warning = ""
            if isinstance(state_payload, dict):
                # Configuration facts are normalized while the runner-local
                # read session is still open. Raw configuration and secrets do
                # not cross the connector boundary.
                try:
                    configured_module = importlib.import_module("drivers.configured_state")
                    configured_state = await configured_module.collect_configured_state(
                        platform,
                        driver,
                        state_payload,
                    )
                except Exception as exc:
                    configuration_warning = f"configured_state: {type(exc).__name__}: {exc}"
                    configured_state = None
                if configured_state is not None:
                    state_payload["configured_state"] = configured_state
            warnings = []
            errors = []
            if isinstance(state_payload, dict):
                warnings = list(state_payload.get("collection_warnings") or [])
                errors = list(state_payload.get("collection_errors") or [])
            if configuration_warning:
                warnings.append(configuration_warning)
            return {
                "ok": True,
                "device_id": device.id,
                "platform": platform,
                "driver": f"{driver_cls.__module__}.{driver_cls.__name__}",
                "adapter": f"rez.{platform}",
                "read_transports": list(READ_TRANSPORTS.get(platform, ("ssh",))),
                "state": state_payload,
                "warnings": warnings,
                "errors": errors,
                "collection_time": round(time.perf_counter() - started, 3),
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return {
                "ok": False,
                "device_id": device.id,
                "platform": platform,
                "driver": f"{driver_cls.__module__}.{driver_cls.__name__}",
                "adapter": f"rez.{platform}",
                "state": None,
                "warnings": [],
                "errors": [error],
                "collection_time": round(time.perf_counter() - started, 3),
                "error": error,
            }
        finally:
            try:
                await driver.disconnect()
            except Exception:
                pass

    def collect_device_state(self, device: Device) -> dict[str, object]:
        return asyncio.run(self.collect_device_state_async(device))

    async def collect_many_async(self, devices: list[Device], max_concurrent: int = 25) -> dict[str, object]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def collect_one(device: Device) -> dict[str, object]:
            async with semaphore:
                return await self.collect_device_state_async(device)

        results = await asyncio.gather(*(collect_one(device) for device in devices))
        return {
            "ok": all(bool(result.get("ok")) for result in results),
            "provider": "rez",
            "device_count": len(devices),
            "max_concurrent": max_concurrent,
            "results": results,
        }

    def collect_many(self, devices: list[Device], max_concurrent: int = 25) -> dict[str, object]:
        return asyncio.run(self.collect_many_async(devices, max_concurrent=max_concurrent))
