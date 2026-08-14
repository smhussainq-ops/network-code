"""Shared hard safety floor for unattended custom configuration."""

from __future__ import annotations

import re


_CHAIN_SEPARATORS = (";", "&&", "||", "`", "$(", ">", "<", "\x00")
_PROHIBITED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^rel(?:o(?:a(?:d)?)?)?(?:\s|$)", re.IGNORECASE), "device reload"),
    (re.compile(r"^(?:write|wr)(?:\s|$)", re.IGNORECASE), "startup configuration write or erase"),
    (re.compile(r"^copy\s+(?:running-config|run)\s+(?:startup-config|start)(?:\s|$)", re.IGNORECASE), "startup configuration write"),
    (re.compile(r"^erase\s+(?:startup-config|nvram)(?:\s|$)", re.IGNORECASE), "startup configuration erase"),
    (re.compile(r"^format(?:\s|$)", re.IGNORECASE), "filesystem format"),
    (re.compile(r"^delete\s+(?:flash:|bootflash:|startup-config|nvram)(?:\s|$)", re.IGNORECASE), "system file deletion"),
    (re.compile(r"^(?:configure|config)\s+replace(?:\s|$)", re.IGNORECASE), "full configuration replacement"),
    (re.compile(r"^boot\s+system(?:\s|$)", re.IGNORECASE), "boot configuration change"),
    (re.compile(r"^install\s+(?:add|activate|commit)(?:\s|$)", re.IGNORECASE), "software lifecycle operation"),
    (re.compile(r"^request\s+system\s+(?:reboot|power-off|halt)(?:\s|$)", re.IGNORECASE), "system lifecycle operation"),
    (re.compile(r"^(?:bash|python3?|tclsh|zsh|ash|guestshell)(?:\s|$)", re.IGNORECASE), "shell escape"),
)


def prohibited_custom_config_lines(text: str) -> list[dict[str, str]]:
    """Return exact prohibited lines without rejecting ordinary network config.

    Custom configuration is executed unattended after approval, so destructive
    device lifecycle, startup persistence, shell escape, and command chaining
    remain hard blocks. Feature configuration such as ``no shutdown`` or
    ``route-map ... deny`` is intentionally not classified by this floor.
    """
    findings: list[dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        lowered = line.lower()
        separator = next((item for item in _CHAIN_SEPARATORS if item in lowered), None)
        if separator is not None:
            findings.append({"line": raw_line, "reason": "command chaining or redirection"})
            continue
        effective = line[3:].strip() if lowered.startswith("do ") else line
        reason = next(
            (label for pattern, label in _PROHIBITED_PATTERNS if pattern.search(effective)),
            None,
        )
        if reason:
            findings.append({"line": raw_line, "reason": reason})
    return findings
