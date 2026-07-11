"""Runner-local FortiManager and Panorama transaction execution.

All calls are generated from typed intent. The control plane cannot provide an
arbitrary manager URL, API method, XPath, command, or credential.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol
from ipaddress import ip_network
from urllib.parse import urlencode
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, Field

from netcode.firewall_managers import (
    ManagerCapabilities,
    ManagerJobRequest,
    ManagerType,
    WRITE_ACTIONS,
    assert_no_secrets,
    capabilities_from_probe,
)
from netcode.inventory import Device, Inventory


class ManagerApiCall(BaseModel):
    name: str
    protocol: str
    method: str
    path: str
    params: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | str | None = None
    write: bool = False


class ManagerAdapter(Protocol):
    def probe(self) -> tuple[ManagerCapabilities, dict[str, Any]]: ...

    def execute(self, call: ManagerApiCall) -> dict[str, Any]: ...

    def candidate_scope(self, request: ManagerJobRequest) -> dict[str, Any]: ...


class OperationLedger:
    """Durable runner-local replay protection for manager operations."""

    def __init__(self, path: Path):
        self.path = path

    def lookup(self, operation_id: str, idempotency_key: str) -> dict[str, Any] | None:
        data = self._read()
        existing = data.get(operation_id)
        if not existing:
            return None
        if existing.get("idempotency_key") != idempotency_key:
            raise ValueError(f"operation_id {operation_id} was already used for a different manager request")
        return dict(existing.get("result") or {})

    def store(self, operation_id: str, idempotency_key: str, result: dict[str, Any]) -> None:
        data = self._read()
        data[operation_id] = {"idempotency_key": idempotency_key, "result": result}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            temp_name = handle.name
        os.chmod(temp_name, 0o600)
        Path(temp_name).replace(self.path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def _policy_payload(request: ManagerJobRequest) -> dict[str, Any]:
    policy = request.policy_change
    if not policy:
        raise ValueError("typed firewall policy is required")
    return {
        "name": policy.name,
        "srcintf": policy.source_zones,
        "dstintf": policy.destination_zones,
        "srcaddr": [item.name for item in policy.source_objects],
        "dstaddr": [item.name for item in policy.destination_objects],
        "service": [item.name for item in policy.services],
        "application": [item.name for item in policy.applications],
        "action": "accept" if policy.action == "allow" else "deny",
        "logtraffic": "all" if policy.log else "disable",
        "utm-status": bool(policy.security_profiles),
        "comments": f"{policy.ticket_id} via Rezonance {request.change_id}",
    }


def _fortimanager_address_payload(name: str, value: str) -> dict[str, Any]:
    network = ip_network(value, strict=False)
    return {"name": name, "type": 0, "subnet": [str(network.network_address), str(network.netmask)]}


def _fortimanager_service_payload(name: str, value: str) -> dict[str, Any]:
    protocol, _, ports = value.partition("/")
    if protocol not in {"tcp", "udp"} or not ports:
        raise ValueError(f"manager service {name} must use tcp/<port> or udp/<port>")
    field = "tcp-portrange" if protocol == "tcp" else "udp-portrange"
    return {"name": name, "protocol": protocol.upper(), field: ports}


def _fortimanager_nat_payload(request: ManagerJobRequest) -> dict[str, Any]:
    nat = request.nat_change
    if not nat:
        raise ValueError("typed firewall NAT is required")
    payload = {
        "name": nat.name,
        "orig-addr": [nat.original_source],
        "dst-addr": [nat.original_destination],
        "service": [nat.service],
        "comments": f"{nat.ticket_id} via Rezonance {request.change_id}",
    }
    if nat.nat_type == "snat":
        payload.update({"nat": "enable", "nat-ippool": [nat.translated_source]})
    else:
        payload.update({"nat": "enable", "dst-addr": [nat.translated_destination]})
    return payload


def _panorama_xpath(request: ManagerJobRequest) -> str:
    ownership = request.ownership
    scope = ownership.scope
    rulebase = "pre-rulebase" if scope.rulebase == "pre" else "post-rulebase"
    return (
        "/config/devices/entry[@name='localhost.localdomain']"
        f"/device-group/entry[@name='{scope.device_group}']"
        f"/{rulebase}/security/rules"
    )


def _panorama_rule_element(request: ManagerJobRequest) -> str:
    policy = request.policy_change
    if not policy:
        raise ValueError("typed firewall policy is required")

    def members(values: list[str]) -> str:
        return "".join(f"<member>{escape(value)}</member>" for value in values)

    action = "allow" if policy.action == "allow" else "deny"
    return (
        f"<entry name=\"{escape(policy.name)}\">"
        f"<from>{members(policy.source_zones)}</from>"
        f"<to>{members(policy.destination_zones)}</to>"
        f"<source>{members([item.name for item in policy.source_objects])}</source>"
        f"<destination>{members([item.name for item in policy.destination_objects])}</destination>"
        f"<service>{members([item.name for item in policy.services])}</service>"
        f"<application>{members([item.name for item in policy.applications] or ['any'])}</application>"
        f"<action>{action}</action>"
        f"<log-end>{'yes' if policy.log else 'no'}</log-end>"
        f"<description>{escape(policy.ticket_id)} via Rezonance {escape(request.change_id)}</description>"
        "</entry>"
    )


def _panorama_object_xpath(request: ManagerJobRequest, kind: str) -> str:
    scope = request.ownership.scope
    base = (
        "/config/devices/entry[@name='localhost.localdomain']"
        f"/device-group/entry[@name='{scope.device_group}']"
    )
    return f"{base}/{'address' if kind == 'address' else 'service'}"


def _panorama_address_element(name: str, value: str) -> str:
    return f'<entry name="{escape(name)}"><ip-netmask>{escape(value)}</ip-netmask></entry>'


def _panorama_service_element(name: str, value: str) -> str:
    protocol, _, ports = value.partition("/")
    if protocol not in {"tcp", "udp"} or not ports:
        raise ValueError(f"manager service {name} must use tcp/<port> or udp/<port>")
    return (
        f'<entry name="{escape(name)}"><protocol><{protocol}><port>{escape(ports)}</port>'
        f"</{protocol}></protocol></entry>"
    )


def _panorama_nat_xpath(request: ManagerJobRequest) -> str:
    scope = request.ownership.scope
    rulebase = "pre-rulebase" if scope.rulebase == "pre" else "post-rulebase"
    return (
        "/config/devices/entry[@name='localhost.localdomain']"
        f"/device-group/entry[@name='{scope.device_group}']/{rulebase}/nat/rules"
    )


def _panorama_nat_element(request: ManagerJobRequest) -> str:
    nat = request.nat_change
    if not nat:
        raise ValueError("typed firewall NAT is required")
    source_translation = (
        f"<source-translation><dynamic-ip-and-port><translated-address>"
        f"<member>{escape(nat.translated_source or '')}</member></translated-address>"
        "</dynamic-ip-and-port></source-translation>"
        if nat.nat_type == "snat"
        else (
            f"<destination-translation><translated-address>{escape(nat.translated_destination or '')}"
            "</translated-address></destination-translation>"
        )
    )
    return (
        f'<entry name="{escape(nat.name)}">'
        f"<source><member>{escape(nat.original_source)}</member></source>"
        f"<destination><member>{escape(nat.original_destination)}</member></destination>"
        f"<service>{escape(nat.service)}</service>{source_translation}"
        f"<description>{escape(nat.ticket_id)} via Rezonance {escape(request.change_id)}</description>"
        "</entry>"
    )


def build_manager_calls(request: ManagerJobRequest) -> list[ManagerApiCall]:
    """Build an inspectable, deterministic call sequence for one lifecycle action."""
    if request.ownership.manager_type == "fortimanager":
        return _fortimanager_calls(request)
    return _panorama_calls(request)


def _fortimanager_calls(request: ManagerJobRequest) -> list[ManagerApiCall]:
    scope = request.ownership.scope
    base = f"/pm/config/adom/{scope.adom}/pkg/{scope.policy_package}"
    target = [{"name": scope.install_target, "vdom": scope.vdom}]
    stage_calls: list[ManagerApiCall] = []
    if request.policy_change:
        objects = [*request.policy_change.source_objects, *request.policy_change.destination_objects]
        for item in objects:
            if item.create_if_missing:
                stage_calls.append(ManagerApiCall(
                    name=f"stage-address:{item.name}", protocol="jsonrpc", method="add",
                    path=f"/pm/config/adom/{scope.adom}/obj/firewall/address",
                    body=_fortimanager_address_payload(item.name, item.value), write=True,
                ))
        for item in request.policy_change.services:
            if item.create_if_missing:
                stage_calls.append(ManagerApiCall(
                    name=f"stage-service:{item.name}", protocol="jsonrpc", method="add",
                    path=f"/pm/config/adom/{scope.adom}/obj/firewall/service/custom",
                    body=_fortimanager_service_payload(item.name, item.value), write=True,
                ))
        stage_calls.extend([
            ManagerApiCall(
                name="stage-policy", protocol="jsonrpc", method="add", path=f"{base}/firewall/policy",
                body=_policy_payload(request), write=True,
            ),
            ManagerApiCall(
                name="position-policy", protocol="jsonrpc", method="move", path=f"{base}/firewall/policy",
                body={
                    "name": request.policy_change.name,
                    "option": request.policy_change.insertion["position"],
                    "target": request.policy_change.insertion["reference_rule"],
                },
                write=True,
            ),
        ])
    if request.nat_change:
        stage_calls.append(ManagerApiCall(
            name="stage-nat", protocol="jsonrpc", method="add",
            path=f"{base}/firewall/central-snat-map", body=_fortimanager_nat_payload(request), write=True,
        ))
    calls: dict[str, list[ManagerApiCall]] = {
        "probe": [ManagerApiCall(name="system-status", protocol="jsonrpc", method="get", path="/sys/status")],
        "snapshot": [ManagerApiCall(name="policy-snapshot", protocol="jsonrpc", method="get", path=f"{base}/firewall/policy")],
        "preview": [ManagerApiCall(
            name="install-preview", protocol="jsonrpc", method="exec", path="/securityconsole/preview",
            body={"adom": scope.adom, "pkg": scope.policy_package, "scope": target},
        )],
        "validate": [ManagerApiCall(
            name="package-validate", protocol="jsonrpc", method="exec", path="/securityconsole/validate",
            body={"adom": scope.adom, "pkg": scope.policy_package, "scope": target},
        )],
        "lock": [ManagerApiCall(
            name="workspace-lock", protocol="jsonrpc", method="exec",
            path=f"/dvmdb/adom/{scope.adom}/workspace/lock", write=True,
        )],
        "stage": stage_calls,
        "deploy": [ManagerApiCall(
            name="install-package", protocol="jsonrpc", method="exec", path="/securityconsole/install/package",
            body={"adom": scope.adom, "pkg": scope.policy_package, "scope": target}, write=True,
        )],
        "poll": [ManagerApiCall(
            name="task-status", protocol="jsonrpc", method="get",
            path=f"/task/task/{request.manager_task_id or 'missing'}",
        )],
        "verify": [ManagerApiCall(name="installed-policy", protocol="jsonrpc", method="get", path=f"{base}/firewall/policy")],
        "discard": [ManagerApiCall(
            name="workspace-discard", protocol="jsonrpc", method="exec",
            path=f"/dvmdb/adom/{scope.adom}/workspace/discard", write=True,
        )],
        "unlock": [ManagerApiCall(
            name="workspace-unlock", protocol="jsonrpc", method="exec",
            path=f"/dvmdb/adom/{scope.adom}/workspace/unlock", write=True,
        )],
        "rollback": [ManagerApiCall(
            name="restore-revision", protocol="jsonrpc", method="exec", path="/securityconsole/install/package",
            body={
                "adom": scope.adom,
                "pkg": scope.policy_package,
                "scope": target,
                "revision": request.pre_change_revision,
                "rollback": True,
            },
            write=True,
        )],
    }
    return calls[request.action]


def _panorama_calls(request: ManagerJobRequest) -> list[ManagerApiCall]:
    scope = request.ownership.scope
    xpath = _panorama_xpath(request)
    target = escape(request.ownership.managed_serial)
    stage_calls: list[ManagerApiCall] = []
    if request.policy_change:
        objects = [*request.policy_change.source_objects, *request.policy_change.destination_objects]
        for item in objects:
            if item.create_if_missing:
                stage_calls.append(ManagerApiCall(
                    name=f"stage-address:{item.name}", protocol="xml-api", method="config", path="/api/",
                    params={"action": "set", "xpath": _panorama_object_xpath(request, "address"), "element": _panorama_address_element(item.name, item.value)},
                    write=True,
                ))
        for item in request.policy_change.services:
            if item.create_if_missing:
                stage_calls.append(ManagerApiCall(
                    name=f"stage-service:{item.name}", protocol="xml-api", method="config", path="/api/",
                    params={"action": "set", "xpath": _panorama_object_xpath(request, "service"), "element": _panorama_service_element(item.name, item.value)},
                    write=True,
                ))
        stage_calls.extend([
            ManagerApiCall(
                name="stage-policy", protocol="xml-api", method="config", path="/api/",
                params={"action": "set", "xpath": xpath, "element": _panorama_rule_element(request)},
                write=True,
            ),
            ManagerApiCall(
                name="position-policy", protocol="xml-api", method="config", path="/api/",
                params={
                    "action": "move",
                    "xpath": f"{xpath}/entry[@name='{request.policy_change.name}']",
                    "where": request.policy_change.insertion["position"],
                    "dst": request.policy_change.insertion["reference_rule"],
                },
                write=True,
            ),
        ])
    if request.nat_change:
        stage_calls.append(ManagerApiCall(
            name="stage-nat", protocol="xml-api", method="config", path="/api/",
            params={"action": "set", "xpath": _panorama_nat_xpath(request), "element": _panorama_nat_element(request)},
            write=True,
        ))
    calls: dict[str, list[ManagerApiCall]] = {
        "probe": [ManagerApiCall(
            name="system-info", protocol="xml-api", method="op", path="/api/",
            params={"cmd": "<show><system><info/></system></show>"},
        )],
        "snapshot": [ManagerApiCall(
            name="config-snapshot", protocol="xml-api", method="export", path="/api/",
            params={"category": "configuration"},
        )],
        "preview": [ManagerApiCall(
            name="candidate-diff", protocol="xml-api", method="op", path="/api/",
            params={"cmd": "<show><config><diffs/></config></show>"},
        )],
        "validate": [ManagerApiCall(
            name="validate-candidate", protocol="xml-api", method="commit", path="/api/",
            params={"action": "validate", "cmd": "<commit><description>Rezonance validation</description></commit>"},
        )],
        "lock": [ManagerApiCall(
            name="config-lock", protocol="xml-api", method="op", path="/api/",
            params={"cmd": "<request><config-lock><add><comment>Rezonance governed change</comment></add></config-lock></request>"},
            write=True,
        )],
        "stage": stage_calls,
        "deploy": [
            ManagerApiCall(
                name="commit-panorama", protocol="xml-api", method="commit", path="/api/",
                params={"cmd": f"<commit><description>{escape(request.change_id)}</description></commit>"},
                write=True,
            ),
            ManagerApiCall(
                name="push-device-group", protocol="xml-api", method="commit", path="/api/",
                params={
                    "action": "all",
                    "cmd": (
                        "<commit-all><shared-policy><device-group>"
                        f"<entry name=\"{escape(scope.device_group or '')}\"><devices><entry name=\"{target}\"/></devices></entry>"
                        "</device-group></shared-policy></commit-all>"
                    ),
                },
                write=True,
            ),
        ],
        "poll": [ManagerApiCall(
            name="job-status", protocol="xml-api", method="op", path="/api/",
            params={"cmd": f"<show><jobs><id>{escape(request.manager_task_id or 'missing')}</id></jobs></show>"},
        )],
        "verify": [ManagerApiCall(
            name="effective-rule", protocol="xml-api", method="config", path="/api/",
            params={"action": "get", "xpath": xpath},
        )],
        "discard": [ManagerApiCall(
            name="revert-candidate", protocol="xml-api", method="op", path="/api/",
            params={"cmd": "<request><config><revert><changes/></revert></config></request>"},
            write=True,
        )],
        "unlock": [ManagerApiCall(
            name="config-unlock", protocol="xml-api", method="op", path="/api/",
            params={"cmd": "<request><config-lock><remove/></config-lock></request>"},
            write=True,
        )],
        "rollback": [
            ManagerApiCall(
                name="load-pre-change-version", protocol="xml-api", method="op", path="/api/",
                params={"cmd": f"<load><config><version>{escape(request.pre_change_revision or '')}</version></config></load>"},
                write=True,
            ),
            ManagerApiCall(
                name="commit-rollback", protocol="xml-api", method="commit", path="/api/",
                params={"cmd": f"<commit><description>Rollback {escape(request.change_id)}</description></commit>"},
                write=True,
            ),
            ManagerApiCall(
                name="push-rollback", protocol="xml-api", method="commit", path="/api/",
                params={
                    "action": "all",
                    "cmd": (
                        "<commit-all><shared-policy><device-group>"
                        f"<entry name=\"{escape(scope.device_group or '')}\"><devices><entry name=\"{target}\"/></devices></entry>"
                        "</device-group></shared-policy></commit-all>"
                    ),
                },
                write=True,
            ),
        ],
    }
    return calls[request.action]


class LiveManagerAdapter:
    """Minimal HTTP adapter. Compatibility remains fail-closed and lab-gated."""

    def __init__(self, device: Device):
        self.device = device
        options = device.connection_options
        self.manager_type: ManagerType = str(options.get("manager_type") or device.platform).strip().lower()  # type: ignore[assignment]
        if self.manager_type not in {"fortimanager", "panorama"}:
            raise ValueError(f"device {device.id} is not a supported firewall manager")
        port = int(options.get("api_port") or (443 if self.manager_type == "panorama" else 443))
        self.base_url = f"https://{device.host}:{port}"
        self.verify_ssl = bool(options.get("verify_ssl", True))
        self.capability_contract = dict(options.get("manager_capabilities") or {})
        self.client = httpx.Client(base_url=self.base_url, verify=self.verify_ssl, timeout=60.0)
        self.session: str | None = None

    def probe(self) -> tuple[ManagerCapabilities, dict[str, Any]]:
        if self.manager_type == "fortimanager":
            response = self._fortimanager_rpc("get", "/sys/status", None)
            version = _find_value(response, ("version", "Version")) or "unknown"
        else:
            response = self._panorama_request("op", {"cmd": "<show><system><info/></system></show>"})
            version = _xml_text(str(response.get("raw") or ""), ".//sw-version") or "unknown"
        advertised = {"read": True, **self.capability_contract}
        capabilities = capabilities_from_probe(self.manager_type, str(version), advertised)
        return capabilities, response

    def execute(self, call: ManagerApiCall) -> dict[str, Any]:
        if call.protocol == "jsonrpc":
            return self._fortimanager_rpc(call.method, call.path, call.body)
        params = dict(call.params)
        return self._panorama_request(call.method, params)

    def candidate_scope(self, request: ManagerJobRequest) -> dict[str, Any]:
        """Fail closed until the manager-specific candidate parser is lab-certified.

        Raw diffs are not enough: the runner must prove every pending change is
        owned by this Rezonance transaction and is inside the reviewed scope.
        """
        preview_request = request.model_copy(update={"action": "preview"})
        calls = build_manager_calls(preview_request)
        evidence = [self.execute(call) for call in calls]
        return {
            "proven_isolated": False,
            "changes": [],
            "evidence": evidence,
            "message": "Candidate isolation parser is not live-certified for this manager/version; write blocked.",
        }

    def _fortimanager_rpc(self, method: str, path: str, body: Any) -> dict[str, Any]:
        token = str(self.device.connection_options.get("api_token") or "").strip()
        if not token and not self.session:
            login = {
                "id": 1,
                "method": "exec",
                "params": [{"url": "/sys/login/user", "data": {"user": self.device.username, "passwd": self.device.password}}],
            }
            response = self.client.post("/jsonrpc", json=login)
            response.raise_for_status()
            payload = response.json()
            self.session = str(payload.get("session") or "")
            if not self.session:
                raise RuntimeError("FortiManager login did not return a session")
        param: dict[str, Any] = {"url": path}
        if body is not None:
            param["data"] = body
        request: dict[str, Any] = {"id": 1, "method": method, "params": [param]}
        if self.session:
            request["session"] = self.session
        query = f"?{urlencode({'access_token': token})}" if token else ""
        response = self.client.post(f"/jsonrpc{query}", json=request)
        response.raise_for_status()
        payload = response.json()
        status = _find_value(payload, ("code",))
        ok = status in (None, 0, "0")
        return {"ok": ok, "status_code": response.status_code, "payload": payload}

    def _panorama_request(self, request_type: str, params: dict[str, Any]) -> dict[str, Any]:
        api_key = str(self.device.connection_options.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("Panorama api_key is missing from runner-local inventory")
        data = {"type": request_type, **params}
        response = self.client.post("/api/", data=data, headers={"X-PAN-KEY": api_key})
        response.raise_for_status()
        raw = response.text
        status = ""
        try:
            status = ElementTree.fromstring(raw).attrib.get("status", "")
        except ElementTree.ParseError:
            pass
        return {"ok": status == "success", "status_code": response.status_code, "raw": raw}


def _find_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _find_value(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_value(child, keys)
            if found is not None:
                return found
    return None


def _xml_text(raw: str, path: str) -> str | None:
    try:
        node = ElementTree.fromstring(raw).find(path)
    except ElementTree.ParseError:
        return None
    return node.text.strip() if node is not None and node.text else None


def _ownership_matches(local: dict[str, Any], request: ManagerJobRequest) -> bool:
    return json.dumps(local, sort_keys=True, separators=(",", ":")) == json.dumps(
        request.ownership.public_dict(), sort_keys=True, separators=(",", ":")
    )


def execute_manager_job(
    payload: dict[str, Any],
    *,
    inventory_path: Path,
    ledger_path: Path,
    adapter: ManagerAdapter | None = None,
) -> dict[str, Any]:
    """Execute one reviewed manager lifecycle action on the local runner."""
    request = ManagerJobRequest.model_validate(payload)
    inventory = Inventory(inventory_path)
    manager = inventory.find_device(request.manager_id)
    if manager is None:
        raise ValueError(f"manager {request.manager_id} is not in runner-local inventory")
    target = inventory.find_device(request.ownership.device_id)
    if target is None:
        raise ValueError(f"managed firewall {request.ownership.device_id} is not in runner-local inventory")
    if not target.management or not _ownership_matches(target.management, request):
        raise ValueError("runner-local manager ownership does not match the reviewed control-plane plan")
    local_type = str(manager.connection_options.get("manager_type") or manager.platform).strip().lower()
    if local_type != request.ownership.manager_type:
        raise ValueError("runner-local manager type does not match reviewed ownership")

    ledger = OperationLedger(ledger_path)
    replay = ledger.lookup(request.operation_id, request.idempotency_key)
    if replay is not None:
        return {**replay, "replayed": True}

    live_adapter = adapter or LiveManagerAdapter(manager)
    live_capabilities, probe_evidence = live_adapter.probe()
    live_capabilities.require(request.action)
    if live_capabilities.manager_type != request.capabilities.manager_type:
        raise ValueError("runner live capability probe does not match control-plane manager type")

    candidate_scope: dict[str, Any] | None = None
    if request.action in {"stage", "deploy", "discard", "rollback"}:
        candidate_scope = live_adapter.candidate_scope(request)
        if not candidate_scope.get("proven_isolated"):
            raise ValueError(str(candidate_scope.get("message") or "manager candidate isolation could not be proven"))
        changes = candidate_scope.get("changes") or []
        for change in changes:
            owner = str(change.get("owner") or "")
            location = str(change.get("location") or "")
            if owner != request.expected_candidate_owner or location != request.expected_candidate_location:
                raise ValueError("manager candidate includes a change outside the reviewed owner/location scope")

    calls = build_manager_calls(request)
    results: list[dict[str, Any]] = []
    for call in calls:
        if call.write and request.action not in WRITE_ACTIONS:
            raise ValueError(f"read action {request.action} attempted to generate manager write {call.name}")
        if call.write:
            request.approval.require_for_write()
        outcome = live_adapter.execute(call)
        results.append({"call": call.model_dump(), "result": outcome})
        if not bool(outcome.get("ok")):
            result = {
                "ok": False,
                "status": "fail",
                "action": request.action,
                "operation_id": request.operation_id,
                "change_id": request.change_id,
                "manager_id": request.manager_id,
                "message": f"Manager operation stopped at {call.name}.",
                "calls": results,
                "live_capabilities": live_capabilities.model_dump(),
                "credentials_leave_runner": False,
            }
            assert_no_secrets(result)
            ledger.store(request.operation_id, request.idempotency_key, result)
            return result
    result = {
        "ok": True,
        "status": "pass",
        "action": request.action,
        "operation_id": request.operation_id,
        "change_id": request.change_id,
        "manager_id": request.manager_id,
        "message": f"Manager {request.action} completed through the local runner.",
        "calls": results,
        "live_capabilities": live_capabilities.model_dump(),
        "probe_evidence": probe_evidence,
        "candidate_scope": candidate_scope,
        "credentials_leave_runner": False,
    }
    assert_no_secrets(result)
    ledger.store(request.operation_id, request.idempotency_key, result)
    return result
