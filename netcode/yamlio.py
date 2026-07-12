"""YAML file helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def read_yaml(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".dpapi":
        from netcode.windows_security import unprotect_machine

        text = unprotect_machine(path.read_bytes()).decode("utf-8")
        data = yaml.safe_load(text) or {}
    else:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    if path.suffix.lower() == ".dpapi":
        from netcode.windows_security import protect_machine

        path.write_bytes(protect_machine(text.encode("utf-8")))
    else:
        path.write_text(text, encoding="utf-8")


def dumps_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
