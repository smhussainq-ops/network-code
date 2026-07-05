"""Read-only troubleshooting summaries backed by collected device state."""

from __future__ import annotations

import json
from typing import Any

from netcode.verification import extract_bgp_neighbors, extract_interfaces, extract_routes, extract_vlans


SUPPORTED_CHECKS = {
    "interfaces": "Interfaces",
    "vlans": "VLANs",
    "bgp": "BGP neighbors",
    "routes": "Routes",
    "acl": "ACLs",
    "live_state": "Live state summary",
}


def troubleshoot_state(
    state_result: dict[str, Any],
    *,
    check: str,
    target: str = "",
    expected: str = "",
) -> dict[str, Any]:
    """Turn raw Rez state into an engineer-readable read-only investigation result."""
    normalized_check = (check or "live_state").strip().lower()
    if normalized_check not in SUPPORTED_CHECKS:
        normalized_check = "live_state"

    state = state_result.get("state") if isinstance(state_result, dict) else None
    state_ok = bool(state_result.get("ok")) if isinstance(state_result, dict) else False
    collections = _collections(state) if state_ok else _empty_collections()
    rows = collections[normalized_check]
    target = target.strip()
    expected = expected.strip()
    matching_rows = [row for row in rows if _matches(row, target)] if target else rows
    evaluation_rows = matching_rows if target else rows
    expected_match = _contains(evaluation_rows, expected) if expected else None

    ok = state_ok and (expected_match is not False) and (not target or bool(matching_rows))
    status = "pass" if ok else "review" if state_ok else "fail"
    message = _message(
        check_label=SUPPORTED_CHECKS[normalized_check],
        target=target,
        expected=expected,
        state_ok=state_ok,
        row_count=len(rows),
        match_count=len(matching_rows),
        expected_match=expected_match,
        error=str(state_result.get("error") or ""),
    )

    return {
        "ok": ok,
        "status": status,
        "provider": "rez",
        "check": normalized_check,
        "check_label": SUPPORTED_CHECKS[normalized_check],
        "target": target,
        "expected": expected,
        "expected_match": expected_match,
        "message": message,
        "device_id": state_result.get("device_id"),
        "platform": state_result.get("platform"),
        "adapter": state_result.get("adapter"),
        "driver": state_result.get("driver"),
        "read_path": {
            "type": "read_only_rez",
            "transport": "Rez driver read path: SSH or API depending on the vendor driver.",
            "device_writes": "none",
        },
        "device_config": "read_only_no_writes",
        "collection": {
            "ok": state_ok,
            "collection_time": state_result.get("collection_time"),
            "warnings": state_result.get("warnings", []),
            "errors": state_result.get("errors", []),
            "error": state_result.get("error"),
        },
        "summary": {
            "interfaces": len(collections["interfaces"]),
            "vlans": len(collections["vlans"]),
            "bgp_neighbors": len(collections["bgp"]),
            "routes": len(collections["routes"]),
            "acls": len(collections["acl"]),
            "matched_rows": len(matching_rows),
        },
        "evidence_rows": matching_rows[:50],
        "omitted_rows": max(len(matching_rows) - 50, 0),
    }


def _collections(state: Any) -> dict[str, list[dict[str, Any]]]:
    summary = {
        "interfaces": extract_interfaces(state),
        "vlans": extract_vlans(state),
        "bgp": extract_bgp_neighbors(state),
        "routes": extract_routes(state),
        "acl": _extract_acls(state),
    }
    summary["live_state"] = [
        {
            "interfaces": len(summary["interfaces"]),
            "vlans": len(summary["vlans"]),
            "bgp_neighbors": len(summary["bgp"]),
            "routes": len(summary["routes"]),
            "acls": len(summary["acl"]),
        }
    ]
    return summary


def _empty_collections() -> dict[str, list[dict[str, Any]]]:
    return {
        "interfaces": [],
        "vlans": [],
        "bgp": [],
        "routes": [],
        "acl": [],
        "live_state": [],
    }


def _extract_acls(state: Any) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    candidates: list[Any] = []
    security = state.get("security")
    if isinstance(security, dict):
        candidates.extend([security.get("acls"), security.get("acl"), security.get("access_lists")])
    candidates.extend([state.get("acls"), state.get("acl"), state.get("access_lists")])
    return _normalize_collection(candidates)


def _normalize_collection(candidates: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            iterable = candidate.values()
        elif isinstance(candidate, list):
            iterable = candidate
        else:
            continue
        for item in iterable:
            if isinstance(item, dict):
                rows.append(item)
            else:
                rows.append({"value": item})
    return rows


def _matches(row: dict[str, Any], target: str) -> bool:
    if not target:
        return True
    return target.lower() in _json_text(row)


def _contains(rows: list[dict[str, Any]], expected: str) -> bool:
    if not expected:
        return True
    return expected.lower() in _json_text(rows)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str).lower()
    except TypeError:
        return str(value).lower()


def _message(
    *,
    check_label: str,
    target: str,
    expected: str,
    state_ok: bool,
    row_count: int,
    match_count: int,
    expected_match: bool | None,
    error: str,
) -> str:
    if not state_ok:
        return error or "Rez could not collect live state from the device."
    target_phrase = f" for '{target}'" if target else ""
    if target and match_count == 0:
        return f"{check_label}: no live rows matched{target_phrase}."
    if expected and expected_match is False:
        return f"{check_label}: live rows matched{target_phrase}, but expected text '{expected}' was not found."
    if expected and expected_match is True:
        return f"{check_label}: expected text '{expected}' was found in live evidence{target_phrase}."
    return f"{check_label}: collected {row_count} live row{'' if row_count == 1 else 's'}{target_phrase}."
