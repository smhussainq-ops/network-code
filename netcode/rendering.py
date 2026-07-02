"""Intent rendering through Jinja templates."""

from __future__ import annotations

from ipaddress import ip_network
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from netcode.models import Intent, RenderResult
from netcode.paths import WorkspacePaths


def _variables(intent: Intent) -> dict[str, Any]:
    data = intent.model_dump()
    network = ip_network(intent.vlan.subnet, strict=False)
    data["vlan"]["network"] = str(network.network_address)
    data["vlan"]["prefixlen"] = network.prefixlen
    data["vlan"]["netmask"] = str(network.netmask)
    if data["vlan"].get("svi", {}).get("enabled") and not data["vlan"]["svi"].get("gateway_ip"):
        hosts = network.hosts()
        data["vlan"]["svi"]["gateway_ip"] = str(next(hosts))
    return data


def render_intent(intent: Intent, paths: WorkspacePaths) -> RenderResult:
    template_name = "add_vlan.j2"
    template_path = paths.templates / "arista" / template_name
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_name)
    variables = _variables(intent)
    config = template.render(**variables).strip() + "\n"
    return RenderResult(
        template_path=str(template_path),
        config=config,
        variables=variables,
    )


def write_rendered_config(paths: WorkspacePaths, intent: Intent, result: RenderResult) -> Path:
    filename = f"{intent.site}-{intent.change_type}-vlan-{intent.vlan.id}.eos"
    path = paths.rendered / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.config, encoding="utf-8")
    return path
