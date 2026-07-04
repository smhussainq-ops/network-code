"""NetBox source-of-truth reader (read-only).

NetBox is the de-facto network source of truth. This module reads devices from a
NetBox instance over its REST API and maps them to the platform's device shape so
they can be synced into the local inventory. It is READ-ONLY and dependency-light
(stdlib urllib), and fails closed: any connection/parse error returns a structured
error rather than raising into the request path.

SaaS note: in runner mode NetBox typically lives inside the customer network, so
this read would route through the on-prem runner (like device reads). Today it is
control-plane-side, matching how local-YAML source of truth is read.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# NetBox platform slug -> netcode platform key. Unknown slugs fall back to
# slug.replace('-', '_'), so new vendors still map sensibly.
_PLATFORM_ALIASES = {
    "arista-eos": "arista_eos", "eos": "arista_eos",
    "cisco-ios": "cisco_ios", "ios": "cisco_ios", "cisco-iosxe": "cisco_ios", "iosxe": "cisco_ios",
    "cisco-nxos": "cisco_nxos", "nxos": "cisco_nxos",
    "cisco-asa": "cisco_asa",
    "juniper-junos": "juniper_junos", "junos": "juniper_junos",
    "paloalto-panos": "palo_alto", "panos": "palo_alto",
    "fortinet-fortios": "fortinet", "fortios": "fortinet",
    "aruba-aoscx": "aruba_aoscx", "nokia-srl": "nokia_srl",
}


class NetBoxError(Exception):
    pass


def normalize_platform(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not slug:
        return ""
    return _PLATFORM_ALIASES.get(slug, slug.replace("-", "_"))


def _default_get_json(url: str, token: str, timeout: float = 15.0) -> dict[str, Any]:
    """Real HTTP GET returning parsed JSON. Isolated so tests can inject a fake."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Token {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise NetBoxError(f"NetBox returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise NetBoxError(f"Could not reach NetBox: {exc}") from exc


class NetBoxClient:
    def __init__(self, url: str, token: str = "", *, get_json: Callable[..., dict[str, Any]] | None = None, timeout: float = 15.0):
        self.base = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._get_json = get_json or _default_get_json

    def _api(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base:
            raise NetBoxError("NetBox URL is not configured.")
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return self._get_json(f"{self.base}/api/{path.lstrip('/')}{qs}", self.token, self.timeout)

    def test_connection(self) -> dict[str, Any]:
        try:
            status = self._api("status/")
            probe = self._api("dcim/devices/", {"limit": 1})
        except NetBoxError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "netbox_version": str(status.get("netbox-version") or status.get("netbox_version") or "unknown"),
            "device_count": int(probe.get("count") or 0),
        }

    def list_devices(self, max_devices: int = 1000) -> list[dict[str, Any]]:
        """Return normalized device candidates, following NetBox pagination."""
        candidates: list[dict[str, Any]] = []
        page = self._api("dcim/devices/", {"limit": 100})
        while True:
            for raw in page.get("results", []):
                mapped = map_device(raw)
                if mapped:
                    candidates.append(mapped)
                if len(candidates) >= max_devices:
                    return candidates
            nxt = page.get("next")
            if not nxt:
                return candidates
            page = self._get_json(nxt, self.token, self.timeout)


def _slug_or_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("slug") or value.get("name") or "")
    return str(value or "")


def _primary_host(raw: dict[str, Any]) -> str:
    for key in ("primary_ip", "primary_ip4", "primary_ip6", "oob_ip"):
        entry = raw.get(key)
        if isinstance(entry, dict) and entry.get("address"):
            return str(entry["address"]).split("/")[0]
    return ""


def map_device(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a NetBox device object to a platform source-of-truth candidate.
    Returns None for devices with no usable name (can't be an inventory id)."""
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    site = _slug_or_name(raw.get("site")) or "unassigned"
    role = _slug_or_name(raw.get("role") or raw.get("device_role"))
    tags = [_slug_or_name(tag) for tag in (raw.get("tags") or []) if _slug_or_name(tag)]
    groups = [g for g in ([role] + tags + ["netbox"]) if g]
    host = _primary_host(raw) or name
    device_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or name
    return {
        "id": device_id,
        "hostname": name,
        "host": host,
        "platform": normalize_platform(_slug_or_name(raw.get("platform"))) or "arista_eos",
        "site": site,
        "groups": sorted(set(groups)),
        "port": 22,
        "source": "netbox",
    }
