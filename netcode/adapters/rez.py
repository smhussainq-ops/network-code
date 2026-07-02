"""Bridge to Rez driver adapters.

This module treats Rez as an external adapter provider. It does not modify the
Rez repository; it imports the driver registry when available and degrades
cleanly when Rez dependencies are missing.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Any

from netcode.inventory import Device


class RezAdapterBridge:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or self._default_root()).expanduser()
        self._driver_map: dict[str, Any] | None = None
        self._error: str | None = None

    def _default_root(self) -> Path:
        candidates = [
            os.environ.get("NETCODE_REZ_ROOT"),
            "/Users/syedhussain/Dev/Prod/resonance-core",
            "/home/syedhussain/resonance-core",
            "/Users/syedhussain/Dev/Prod/resonance-core/Claude/resonance-core",
            "/Users/syedhussain/Dev/Claude/resonance-core",
            "/Users/syedhussain/resonance-core",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
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

    def _clear_stale_driver_modules(self) -> None:
        """Avoid reusing a previously imported Rez drivers package from another root."""
        root = self.root.resolve()
        for module_name in ("drivers.collector", "drivers"):
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
                }
                for platform, driver_cls in sorted(driver_map.items())
            ],
            "error": self._error,
        }

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
        driver_cls = driver_map.get(device.platform)
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

        driver = driver_cls(device.host, device.username, device.password, device.port)
        try:
            await driver.connect()
            state = await driver.get_full_state()
            if hasattr(state, "model_dump"):
                state_payload = state.model_dump()
            elif hasattr(state, "dict"):
                state_payload = state.dict()
            else:
                state_payload = state
            warnings = []
            errors = []
            if isinstance(state_payload, dict):
                warnings = list(state_payload.get("collection_warnings") or [])
                errors = list(state_payload.get("collection_errors") or [])
            return {
                "ok": True,
                "device_id": device.id,
                "platform": device.platform,
                "driver": f"{driver_cls.__module__}.{driver_cls.__name__}",
                "adapter": f"rez.{device.platform}",
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
                "platform": device.platform,
                "driver": f"{driver_cls.__module__}.{driver_cls.__name__}",
                "adapter": f"rez.{device.platform}",
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
