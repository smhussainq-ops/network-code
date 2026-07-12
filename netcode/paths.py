"""Workspace path helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def intents(self) -> Path:
        return self.root / "intents"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def policies(self) -> Path:
        return self.root / "policies"

    @property
    def inventories(self) -> Path:
        return self.root / "inventories"

    @property
    def rendered(self) -> Path:
        return self.root / "rendered"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def state(self) -> Path:
        return self.root / ".netcode"

    @property
    def database(self) -> Path:
        return self.state / "netcode.db"

    @property
    def git_workspace(self) -> Path:
        configured = os.environ.get("NETCODE_GIT_WORKSPACE", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return self.state / "change-history"

    @property
    def static(self) -> Path:
        return self.root / "static"

    def ensure(self) -> None:
        for path in (
            self.intents,
            self.intents / "examples",
            self.templates / "arista",
            self.policies,
            self.inventories,
            self.rendered,
            self.reports,
            self.state,
            self.git_workspace,
            self.static,
        ):
            path.mkdir(parents=True, exist_ok=True)


def workspace_root() -> Path:
    return Path(os.environ.get("NETCODE_WORKSPACE", os.getcwd())).resolve()


def paths(root: Path | None = None) -> WorkspacePaths:
    return WorkspacePaths((root or workspace_root()).resolve())
