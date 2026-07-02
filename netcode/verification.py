"""Vendor-neutral live-state verification helpers."""

from __future__ import annotations

from typing import Any


def verify_vlan_state(state_result: dict[str, Any], vlan_id: int, vlan_name: str | None = None, present: bool = True) -> dict[str, Any]:
    """Verify VLAN presence or absence from a normalized Rez state response."""
    if not state_result.get("ok"):
        return {
            "ok": False,
            "status": "unsupported",
            "message": "Live state is unavailable, so VLAN state cannot be verified through Rez.",
            "evidence": {
                "state_ok": state_result.get("ok"),
                "error": state_result.get("error"),
                "errors": state_result.get("errors", []),
                "platform": state_result.get("platform"),
            },
        }

    state = state_result.get("state")
    vlans = extract_vlans(state)
    match = next((vlan for vlan in vlans if vlan.get("vlan_id") == vlan_id), None)
    name_matches = bool(match and (not vlan_name or str(match.get("name", "")).upper() == vlan_name.upper()))

    if present and match and name_matches:
        return {
            "ok": True,
            "status": "pass",
            "message": f"Rez live state shows VLAN {vlan_id} present.",
            "evidence": {"matched_vlan": match, "vlan_count": len(vlans), "adapter": state_result.get("adapter")},
        }
    if not present and not match:
        return {
            "ok": True,
            "status": "pass",
            "message": f"Rez live state shows VLAN {vlan_id} absent.",
            "evidence": {"vlan_count": len(vlans), "adapter": state_result.get("adapter")},
        }

    expected = "present" if present else "absent"
    return {
        "ok": False,
        "status": "fail",
        "message": f"Rez live state did not prove VLAN {vlan_id} is {expected}.",
        "evidence": {
            "matched_vlan": match,
            "vlan_count": len(vlans),
            "vlans": vlans[:25],
            "expected_name": vlan_name,
            "adapter": state_result.get("adapter"),
        },
    }


def verify_interface_state(state_result: dict[str, Any], interface: str, expected: str = "up") -> dict[str, Any]:
    if not state_result.get("ok"):
        return _unsupported(state_result, "Live state is unavailable, so interface state cannot be verified.")
    interfaces = extract_interfaces(state_result.get("state"))
    match = next((item for item in interfaces if str(item.get("name")).lower() == interface.lower()), None)
    if not match:
        return _fail("Interface was not found in live state.", {"interface": interface, "known_interfaces": [item.get("name") for item in interfaces[:25]]})
    actual = str(match.get("oper_status") or match.get("admin_status") or match.get("status") or "").lower()
    ok = expected.lower() in actual if actual else False
    return _pass(f"Interface {interface} matched expected state {expected}.", {"interface": match}) if ok else _fail(
        f"Interface {interface} did not match expected state {expected}.",
        {"interface": match, "expected": expected, "actual": actual},
    )


def verify_bgp_neighbor_established(state_result: dict[str, Any], neighbor: str) -> dict[str, Any]:
    if not state_result.get("ok"):
        return _unsupported(state_result, "Live state is unavailable, so BGP state cannot be verified.")
    neighbors = extract_bgp_neighbors(state_result.get("state"))
    match = next((item for item in neighbors if str(item.get("neighbor") or item.get("peer") or item.get("ip")) == neighbor), None)
    if not match:
        return _fail("BGP neighbor was not found in live state.", {"neighbor": neighbor, "known_neighbors": neighbors[:25]})
    state = str(match.get("state") or match.get("session_state") or "").lower()
    ok = state == "established"
    return _pass(f"BGP neighbor {neighbor} is established.", {"neighbor": match}) if ok else _fail(
        f"BGP neighbor {neighbor} is not established.",
        {"neighbor": match, "state": state},
    )


def verify_route_present(state_result: dict[str, Any], prefix: str) -> dict[str, Any]:
    if not state_result.get("ok"):
        return _unsupported(state_result, "Live state is unavailable, so route state cannot be verified.")
    routes = extract_routes(state_result.get("state"))
    match = next((item for item in routes if str(item.get("prefix") or item.get("network")) == prefix), None)
    return _pass(f"Route {prefix} is present.", {"route": match}) if match else _fail(
        f"Route {prefix} was not found.",
        {"prefix": prefix, "known_routes": routes[:25]},
    )


def verify_management_reachable(state_result: dict[str, Any]) -> dict[str, Any]:
    if not state_result.get("ok"):
        return _unsupported(state_result, "Management reachability cannot be verified because state collection failed.")
    return _pass("Management plane was reachable for state collection.", {"adapter": state_result.get("adapter"), "collection_time": state_result.get("collection_time")})


def verify_prefix_not_leaking(state_result: dict[str, Any], prefix: str, forbidden_tables: list[str] | None = None) -> dict[str, Any]:
    if not state_result.get("ok"):
        return _unsupported(state_result, "Live state is unavailable, so prefix leak checks cannot be verified.")
    forbidden = {item.lower() for item in (forbidden_tables or ["internet", "default", "global"])}
    routes = extract_routes(state_result.get("state"))
    leaks = [
        route for route in routes
        if str(route.get("prefix") or route.get("network")) == prefix
        and str(route.get("table") or route.get("vrf") or "").lower() in forbidden
    ]
    return _pass(f"Prefix {prefix} was not found in forbidden route tables.", {"prefix": prefix, "forbidden_tables": sorted(forbidden)}) if not leaks else _fail(
        f"Prefix {prefix} appears in forbidden route table(s).",
        {"prefix": prefix, "leaks": leaks},
    )


def verify_state(state_result: dict[str, Any], check: str, **params: Any) -> dict[str, Any]:
    checks = {
        "vlan_exists": lambda: verify_vlan_state(state_result, int(params["vlan_id"]), params.get("name"), present=True),
        "vlan_absent": lambda: verify_vlan_state(state_result, int(params["vlan_id"]), params.get("name"), present=False),
        "interface_state": lambda: verify_interface_state(state_result, str(params["interface"]), str(params.get("expected", "up"))),
        "bgp_neighbor_established": lambda: verify_bgp_neighbor_established(state_result, str(params["neighbor"])),
        "route_present": lambda: verify_route_present(state_result, str(params["prefix"])),
        "prefix_not_leaking": lambda: verify_prefix_not_leaking(state_result, str(params["prefix"]), params.get("forbidden_tables")),
        "management_reachable": lambda: verify_management_reachable(state_result),
    }
    if check not in checks:
        return {"ok": False, "status": "unsupported", "message": f"Unknown verification check {check}.", "evidence": {"supported_checks": sorted(checks)}}
    return checks[check]()


def extract_vlans(state: Any) -> list[dict[str, Any]]:
    """Extract VLANs from common DeviceStateV2 dictionary shapes."""
    if not isinstance(state, dict):
        return []

    candidates: list[Any] = []
    layer2 = state.get("layer2")
    if isinstance(layer2, dict):
        candidates.append(layer2.get("vlans"))
    candidates.append(state.get("vlans"))
    candidates.append(state.get("vlan"))

    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            iterable = candidate.values()
        elif isinstance(candidate, list):
            iterable = candidate
        else:
            continue
        for item in iterable:
            vlan = _normalize_vlan(item)
            if vlan:
                normalized.append(vlan)
    return list({vlan["vlan_id"]: vlan for vlan in normalized}.values())


def extract_interfaces(state: Any) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    candidates = [state.get("interfaces")]
    if isinstance(state.get("layer1"), dict):
        candidates.append(state["layer1"].get("interfaces"))
    return _normalize_collection(candidates)


def extract_routes(state: Any) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    candidates = [state.get("routes"), state.get("routing_table")]
    if isinstance(state.get("routing"), dict):
        candidates.extend([state["routing"].get("routes"), state["routing"].get("rib")])
    return _normalize_collection(candidates)


def extract_bgp_neighbors(state: Any) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    candidates = [state.get("bgp_neighbors")]
    if isinstance(state.get("routing"), dict):
        candidates.append(state["routing"].get("bgp_neighbors"))
    if isinstance(state.get("bgp"), dict):
        candidates.append(state["bgp"].get("neighbors"))
    return _normalize_collection(candidates)


def _normalize_collection(candidates: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            iterable = candidate.values()
        elif isinstance(candidate, list):
            iterable = candidate
        else:
            continue
        for item in iterable:
            if isinstance(item, dict):
                normalized.append(item)
    return normalized


def _normalize_vlan(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    raw_id = item.get("vlan_id", item.get("id", item.get("vlan")))
    try:
        vlan_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return {
        "vlan_id": vlan_id,
        "name": str(item.get("name") or item.get("vlan_name") or ""),
        "state": item.get("state"),
        "source": item,
    }


def _unsupported(state_result: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unsupported",
        "message": message,
        "evidence": {
            "state_ok": state_result.get("ok"),
            "error": state_result.get("error"),
            "errors": state_result.get("errors", []),
            "platform": state_result.get("platform"),
        },
    }


def _pass(message: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "status": "pass", "message": message, "evidence": evidence}


def _fail(message: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "status": "fail", "message": message, "evidence": evidence}
