"""Runner-side exact-flow evidence collection for cross-domain changes."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Any, Callable

from netcode.cross_domain import CheckEvidence, flow_key
from netcode.firewall_managers import ApplicationFlow


StateCollector = Callable[[str], dict[str, Any]]
ApplicationProbe = Callable[[ApplicationFlow], dict[str, Any]]


def _nested(value: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, [], {}):
            return current
    return None


def _lists_for_keys(value: Any, keys: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in keys and isinstance(child, list):
                rows.extend(item for item in child if isinstance(item, dict))
            else:
                rows.extend(_lists_for_keys(child, keys))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_lists_for_keys(child, keys))
    return rows


def _route_check(state: dict[str, Any], destination: str) -> tuple[str, dict[str, Any]]:
    target = ip_address(destination)
    candidates: list[tuple[Any, dict[str, Any]]] = []
    for row in _lists_for_keys(state, {"routes", "routing_table", "route_table"}):
        prefix = row.get("prefix") or row.get("network") or row.get("destination") or row.get("route")
        if not prefix or str(prefix).lower() in {"default", "0/0"}:
            prefix = "0.0.0.0/0" if str(prefix).lower() in {"default", "0/0"} else prefix
        try:
            network = ip_network(str(prefix), strict=False)
        except (TypeError, ValueError):
            continue
        if target in network:
            candidates.append((network, row))
    if not candidates:
        return "fail", {"reason": "no_lpm_route", "destination": destination}
    network, route = max(candidates, key=lambda item: item[0].prefixlen)
    state_text = str(route.get("state") or route.get("status") or "").lower()
    protocol = str(route.get("protocol") or route.get("source") or "").lower()
    unusable = any(marker in state_text for marker in ("inactive", "unreachable", "discard", "down")) or protocol in {"null", "discard"}
    return ("fail" if unusable else "pass"), {"prefix": str(network), "route": route, "usable": not unusable}


def _value_matches_ip(values: Any, address: str) -> bool | None:
    if not isinstance(values, list):
        values = [values]
    resolved = False
    for value in values:
        text = str(value or "").strip().lower()
        if text in {"all", "any", "0.0.0.0/0"}:
            return True
        try:
            network = ip_network(text, strict=False)
        except ValueError:
            continue
        resolved = True
        if ip_address(address) in network:
            return True
    return False if resolved else None


def _service_matches(row: dict[str, Any], flow: ApplicationFlow) -> bool | None:
    protocol = str(row.get("protocol") or row.get("ip_protocol") or "").lower()
    if protocol and protocol not in {"any", "ip", flow.protocol}:
        return False
    if flow.protocol == "icmp":
        return True
    ports = row.get("destination_ports") or row.get("dst_ports") or row.get("destination_port") or row.get("dst_port")
    if ports is None:
        service = row.get("service") or row.get("services")
        values = service if isinstance(service, list) else [service]
        if any(str(value).lower() in {"any", "all", f"{flow.protocol}/{flow.destination_port}"} for value in values):
            return True
        return None
    if not isinstance(ports, list):
        ports = [ports]
    for value in ports:
        text = str(value)
        if text.isdigit() and int(text) == flow.destination_port:
            return True
        if "-" in text:
            try:
                start, end = (int(item) for item in text.split("-", 1))
            except ValueError:
                continue
            if start <= int(flow.destination_port or 0) <= end:
                return True
    return False


def _policy_check(state: dict[str, Any], flow: ApplicationFlow) -> tuple[str, dict[str, Any]]:
    lookup = _nested(
        state,
        ("security", "policy_lookup"),
        ("security", "policy_match"),
        ("policy_lookup",),
    )
    if isinstance(lookup, dict):
        same_flow = (
            str(lookup.get("source_ip") or lookup.get("src_ip") or "") == flow.source_ip
            and str(lookup.get("destination_ip") or lookup.get("dst_ip") or "") == flow.destination_ip
            and str(lookup.get("protocol") or "").lower() == flow.protocol
            and (flow.destination_port is None or int(lookup.get("destination_port") or lookup.get("dst_port") or 0) == flow.destination_port)
        )
        if same_flow:
            verdict = str(lookup.get("action") or lookup.get("verdict") or "").lower()
            if verdict in {"allow", "accept", "permit", "deny", "drop", "reject"}:
                return ("pass" if verdict in {"allow", "accept", "permit"} else "fail"), {"lookup": lookup}

    policies = _lists_for_keys(state, {"firewall_policies", "security_policies", "policies"})
    unknown_object = False
    for row in policies:
        source = _value_matches_ip(row.get("source") or row.get("sources") or row.get("srcaddr"), flow.source_ip)
        destination = _value_matches_ip(
            row.get("destination") or row.get("destinations") or row.get("dstaddr"), flow.destination_ip
        )
        service = _service_matches(row, flow)
        if None in {source, destination, service}:
            unknown_object = True
            continue
        if source and destination and service:
            action = str(row.get("action") or row.get("verdict") or "").lower()
            return ("pass" if action in {"allow", "accept", "permit"} else "fail"), {"policy": row}
    return "unknown" if unknown_object else "fail", {"reason": "no_resolved_matching_policy", "policy_count": len(policies)}


def _nat_check(state: dict[str, Any], flow: ApplicationFlow) -> tuple[str, dict[str, Any]]:
    section_status = _nested(state, ("section_status",), ("security", "section_status"))
    collected = isinstance(section_status, dict) and str(section_status.get("nat_rules") or "").lower() == "ok"
    rules = _lists_for_keys(state, {"nat_rules"})
    matching: list[dict[str, Any]] = []
    for row in rules:
        source = _value_matches_ip(row.get("original_source") or row.get("source"), flow.source_ip)
        destination = _value_matches_ip(row.get("original_destination") or row.get("destination"), flow.destination_ip)
        if source is True and destination is True:
            matching.append(row)
    if flow.expected_nat == "none":
        if not collected:
            return "unknown", {"reason": "nat_collection_not_proven"}
        return ("fail" if matching else "pass"), {"matching_rules": matching, "collection_proven": True}
    expected_type = flow.expected_nat
    for row in matching:
        nat_type = str(row.get("nat_type") or row.get("type") or "").lower()
        if expected_type in nat_type:
            return "pass", {"rule": row, "collection_proven": collected}
    return ("fail" if collected else "unknown"), {"matching_rules": matching, "collection_proven": collected}


def _sdwan_check(state: dict[str, Any], flow: ApplicationFlow) -> tuple[str, dict[str, Any]]:
    expected = str(flow.expected_sdwan_class or "").strip().lower()
    if not expected:
        return "pass", {"reason": "no_sdwan_dependency"}
    selections = _lists_for_keys(state, {"sdwan_selections", "policy_matches", "service_rules"})
    for row in selections:
        traffic_class = str(row.get("class") or row.get("name") or row.get("service") or "").strip().lower()
        if traffic_class != expected:
            continue
        member = row.get("selected_member") or row.get("member")
        healthy = row.get("healthy") if "healthy" in row else row.get("alive")
        if member and healthy is True:
            return "pass", {"selection": row}
        if member and healthy is False:
            return "fail", {"selection": row}
    return "unknown", {"reason": "no_exact_sdwan_class_selection", "expected_class": expected}


def collect_exact_flow_evidence(
    payload: dict[str, Any],
    *,
    collect_state: StateCollector,
    application_probe: ApplicationProbe,
) -> dict[str, Any]:
    flow = ApplicationFlow.model_validate(payload.get("flow"))
    required = [str(item) for item in payload.get("required_checks") or []]
    devices = dict(payload.get("devices") or {})
    source_id = str(devices.get("source") or flow.source_device)
    route_owner_id = str(devices.get("route_owner") or source_id)
    firewall_id = str(devices.get("firewall") or "")
    state_ids = {source_id, route_owner_id, firewall_id}
    states: dict[str, dict[str, Any]] = {}
    collection_errors: dict[str, Any] = {}
    for device_id in sorted(item for item in state_ids if item):
        result = collect_state(device_id)
        if result.get("ok") and isinstance(result.get("state"), dict):
            states[device_id] = result["state"]
        else:
            collection_errors[device_id] = result

    key = flow_key(flow)
    checks: list[CheckEvidence] = []
    for check in required:
        status = "unknown"
        observed: Any = {"reason": "unsupported_check"}
        source = "runner-live"
        if check == "forward_route" and source_id in states:
            status, observed = _route_check(states[source_id], flow.destination_ip)
            source = f"rez-state:{source_id}"
        elif check == "return_route" and route_owner_id in states:
            status, observed = _route_check(states[route_owner_id], flow.source_ip)
            source = f"rez-state:{route_owner_id}"
        elif check == "sdwan_selection" and source_id in states:
            status, observed = _sdwan_check(states[source_id], flow)
            source = f"rez-state:{source_id}"
        elif check == "installed_policy_match" and firewall_id in states:
            status, observed = _policy_check(states[firewall_id], flow)
            source = f"rez-state:{firewall_id}"
        elif check == "nat_behavior" and firewall_id in states:
            status, observed = _nat_check(states[firewall_id], flow)
            source = f"rez-state:{firewall_id}"
        elif check == "application_probe":
            probe = application_probe(flow)
            if probe.get("connected") is True or probe.get("listener_present") is True:
                status = "pass"
            elif probe.get("refused") is True or probe.get("listener_present") is False:
                status = "fail"
            else:
                status = "unknown"
            observed = probe
            source = "runner-source-probe"
        checks.append(CheckEvidence(
            check=check,
            status=status,
            fresh=True,
            flow_key=key,
            source=source,
            observed=observed,
            expected="pass",
            evidence_refs=[f"{source}:{check}"],
        ))
    return {
        "ok": True,
        "status": "pass",
        "change_id": str(payload.get("change_id") or ""),
        "plan_id": str(payload.get("plan_id") or ""),
        "flow_key": key,
        "service_checks": [item.model_dump(mode="json") for item in checks],
        "collection_errors": collection_errors,
        "credentials_leave_runner": False,
        "message": "Exact-flow evidence collected through the customer-side runner.",
    }
