"""Bounded discovery profiles shared by Local Connector discovery jobs.

The control plane may request public targets and scope, but only the Local
Connector expands and touches them.  An existing connector inventory is an
approved exact-host scope; discovering unknown hosts requires an explicit IP,
range, or CIDR in the request.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any, Iterable

from netcode.inventory import Device, Inventory


class DiscoveryProfileError(ValueError):
    """Raised when a discovery request is unsafe or cannot be resolved."""


@dataclass(frozen=True)
class DiscoveryTarget:
    host: str
    device_id: str = ""
    platform: str = ""
    port: int = 22
    site: str = ""
    groups: tuple[str, ...] = ()
    optional_probe: bool = False

    def scan_payload(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "device_id": self.device_id,
            "platform": self.platform,
            "port": self.port,
            "site": self.site,
            "groups": list(self.groups),
        }


def _host_network(value: str) -> ipaddress._BaseNetwork:  # type: ignore[name-defined]
    address = ipaddress.ip_address(value)
    return ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=False)


def _parse_networks(values: Iterable[Any], *, field: str) -> tuple[ipaddress._BaseNetwork, ...]:  # type: ignore[name-defined]
    networks: list[ipaddress._BaseNetwork] = []  # type: ignore[name-defined]
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise DiscoveryProfileError(f"Invalid {field} network '{value}'.") from exc
    return tuple(networks)


def _split_host_port(value: str) -> tuple[str, int | None]:
    cleaned = value.strip()
    try:
        ipaddress.ip_address(cleaned)
        return cleaned, None
    except ValueError:
        pass
    if cleaned.count(":") == 1:
        host, port = cleaned.rsplit(":", 1)
        if port.isdigit() and 1 <= int(port) <= 65535:
            return host, int(port)
    return cleaned, None


def _expand_ip_range(token: str, *, remaining: int) -> list[str] | None:
    shorthand = re.fullmatch(r"(.+\.)(\d+)-(\d+)", token)
    if shorthand:
        start_text = f"{shorthand.group(1)}{shorthand.group(2)}"
        try:
            start = ipaddress.ip_address(start_text)
        except ValueError:
            return None
        if start.version != 4:
            return None
        end_octet = int(shorthand.group(3))
        start_octet = int(shorthand.group(2))
        if end_octet < start_octet or end_octet > 255:
            raise DiscoveryProfileError(f"Invalid discovery range '{token}'.")
        count = end_octet - start_octet + 1
        if count > remaining:
            raise DiscoveryProfileError("Discovery seed range exceeds max_devices.")
        base = int(start) - start_octet
        return [str(ipaddress.ip_address(base + octet)) for octet in range(start_octet, end_octet + 1)]

    if "-" not in token:
        return None
    start_text, end_text = (part.strip() for part in token.split("-", 1))
    try:
        start = ipaddress.ip_address(start_text)
        end = ipaddress.ip_address(end_text)
    except ValueError:
        return None
    if start.version != end.version or int(end) < int(start):
        raise DiscoveryProfileError(f"Invalid discovery range '{token}'.")
    count = int(end) - int(start) + 1
    if count > remaining:
        raise DiscoveryProfileError("Discovery seed range exceeds max_devices.")
    return [str(ipaddress.ip_address(value)) for value in range(int(start), int(end) + 1)]


def _target_for_device(device: Device) -> DiscoveryTarget:
    return DiscoveryTarget(
        host=device.host,
        device_id=device.id,
        platform=device.platform,
        port=device.port,
        site=device.site or "",
        groups=device.groups,
    )


@dataclass(frozen=True)
class DiscoveryProfile:
    profile_id: str
    seeds: tuple[DiscoveryTarget, ...]
    allowed_networks: tuple[ipaddress._BaseNetwork, ...]  # type: ignore[name-defined]
    excluded_networks: tuple[ipaddress._BaseNetwork, ...]  # type: ignore[name-defined]
    max_depth: int
    max_devices: int
    concurrency: int
    scope_source: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any], inventory: Inventory) -> "DiscoveryProfile":
        try:
            max_devices = int(payload.get("max_devices") or 256)
            max_depth = int(payload.get("depth") or 0)
            concurrency = int(payload.get("concurrency") or 8)
        except (TypeError, ValueError) as exc:
            raise DiscoveryProfileError("Discovery limits must be integers.") from exc
        if not 1 <= max_devices <= 5000:
            raise DiscoveryProfileError("max_devices must be between 1 and 5000.")
        if not 0 <= max_depth <= 10:
            raise DiscoveryProfileError("depth must be between 0 and 10.")
        if not 1 <= concurrency <= 32:
            raise DiscoveryProfileError("concurrency must be between 1 and 32.")

        explicit_allowed = _parse_networks(payload.get("allowed_cidrs") or [], field="allowed")
        excluded = _parse_networks(payload.get("excluded_cidrs") or [], field="excluded")
        allowed: list[ipaddress._BaseNetwork] = list(explicit_allowed)  # type: ignore[name-defined]
        scope_source = "explicit_profile" if explicit_allowed else "connector_inventory_and_explicit_seeds"

        if not explicit_allowed:
            for device in inventory.devices:
                try:
                    allowed.append(_host_network(device.host))
                except ValueError:
                    continue

        seed_text = str(payload.get("seed_node") or payload.get("seeds") or payload.get("host") or "").strip()
        raw_tokens = [item.strip() for item in seed_text.split(",") if item.strip()]
        if not raw_tokens:
            raise DiscoveryProfileError("At least one discovery seed is required.")

        targets: list[DiscoveryTarget] = []
        seen_targets: set[tuple[str, int]] = set()

        def add_host(
            host: str,
            *,
            requested_port: int | None = None,
            known_device: Device | None = None,
            optional_probe: bool = False,
        ) -> None:
            known = known_device or inventory.find_device(host)
            if known:
                target = _target_for_device(known)
            else:
                try:
                    ipaddress.ip_address(host)
                except ValueError as exc:
                    raise DiscoveryProfileError(
                        f"Unknown hostname '{host}'. Add it to the Local Connector inventory or use an IP address."
                    ) from exc
                target = DiscoveryTarget(
                    host=host,
                    port=requested_port or 22,
                    optional_probe=optional_probe,
                )
            if requested_port and requested_port != target.port:
                target = DiscoveryTarget(
                    host=target.host,
                    device_id=target.device_id,
                    platform=target.platform,
                    port=requested_port,
                    site=target.site,
                    groups=target.groups,
                    optional_probe=target.optional_probe,
                )
            target_key = (target.host, target.port)
            if target_key in seen_targets:
                return
            targets.append(target)
            seen_targets.add(target_key)
            if not explicit_allowed:
                allowed.append(_host_network(host))

        for token in raw_tokens:
            if len(targets) >= max_devices:
                raise DiscoveryProfileError("Discovery seeds exceed max_devices.")
            host_token, requested_port = _split_host_port(token)
            known = inventory.find_device(host_token)
            if known:
                add_host(known.host, requested_port=requested_port, known_device=known)
                continue
            network = None
            if "/" in host_token:
                try:
                    network = ipaddress.ip_network(host_token, strict=False)
                except ValueError:
                    network = None
            if network is not None:
                host_count = int(network.num_addresses)
                if network.version == 4 and network.prefixlen < 31:
                    host_count = max(0, host_count - 2)
                if host_count > max_devices - len(targets):
                    raise DiscoveryProfileError(
                        f"Discovery CIDR '{host_token}' contains {host_count} hosts, above the remaining max_devices limit."
                    )
                if not explicit_allowed:
                    allowed.append(network)
                for address in network.hosts():
                    add_host(
                        str(address),
                        requested_port=requested_port,
                        optional_probe=True,
                    )
                continue
            expanded = _expand_ip_range(host_token, remaining=max_devices - len(targets))
            if expanded is not None:
                for host in expanded:
                    add_host(
                        host,
                        requested_port=requested_port,
                        optional_probe=True,
                    )
                continue
            add_host(host_token, requested_port=requested_port)

        profile = cls(
            profile_id=str(payload.get("profile_id") or "bounded-default").strip() or "bounded-default",
            seeds=tuple(targets),
            allowed_networks=tuple(dict.fromkeys(allowed)),
            excluded_networks=excluded,
            max_depth=max_depth,
            max_devices=max_devices,
            concurrency=concurrency,
            scope_source=scope_source,
        )
        for target in profile.seeds:
            if not profile.is_allowed(target.host):
                raise DiscoveryProfileError(f"Seed {target.host} is outside the approved discovery scope.")
        return profile

    def is_allowed(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        if any(address in network for network in self.excluded_networks if network.version == address.version):
            return False
        return any(address in network for network in self.allowed_networks if network.version == address.version)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "seeds": [target.host for target in self.seeds],
            "allowed_cidrs": [str(network) for network in self.allowed_networks],
            "excluded_cidrs": [str(network) for network in self.excluded_networks],
            "depth": self.max_depth,
            "max_devices": self.max_devices,
            "concurrency": self.concurrency,
            "scope_source": self.scope_source,
        }

    def resolve_reference(self, value: Any, inventory: Inventory) -> DiscoveryTarget | None:
        reference = str(value or "").strip().strip("[]()")
        if not reference:
            return None
        known = inventory.find_device(reference)
        if known and self.is_allowed(known.host):
            return _target_for_device(known)
        host, port = _split_host_port(reference)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return None
        if not self.is_allowed(host):
            return None
        known = inventory.find_device(host)
        if known:
            return _target_for_device(known)
        return DiscoveryTarget(host=host, port=port or 22)


_NEIGHBOR_KEYS = (
    "neighbor_id",
    "neighbor",
    "hostname",
    "system_name",
    "device_id",
    "peer",
    "peer_ip",
    "neighbor_ip",
    "management_address",
    "management_ip",
    "mgmt_ip",
    "address",
    "ip",
)


def _neighbor_rows(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        rows: list[Any] = []
        for key, item in value.items():
            if isinstance(item, dict):
                rows.append({"_map_key": key, **item})
            else:
                rows.append(item if item not in (None, "") else key)
        return rows
    return []


def discovery_neighbor_targets(
    state: dict[str, Any],
    *,
    inventory: Inventory,
    profile: DiscoveryProfile,
) -> list[DiscoveryTarget]:
    """Resolve only device-authoritative neighbor references inside approved scope."""
    containers: list[Any] = [
        state.get("lldp_neighbors"),
        state.get("cdp_neighbors"),
        state.get("bgp_neighbors"),
        state.get("ospf_neighbors"),
    ]
    for section_name in ("layer2", "routing", "bgp", "ospf"):
        section = state.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("lldp_neighbors", "cdp_neighbors", "neighbors", "peers", "bgp_neighbors", "ospf_neighbors"):
            containers.append(section.get(key))

    resolved: dict[tuple[str, int], DiscoveryTarget] = {}
    for container in containers:
        for row in _neighbor_rows(container):
            references: list[Any]
            if isinstance(row, dict):
                references = [row.get(key) for key in _NEIGHBOR_KEYS]
                references.append(row.get("_map_key"))
            else:
                references = [row]
            for reference in references:
                target = profile.resolve_reference(reference, inventory)
                if target:
                    resolved[(target.host, target.port)] = target
                    break
    return sorted(resolved.values(), key=lambda item: (item.device_id or item.host).lower())
