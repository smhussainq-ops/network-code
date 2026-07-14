"""Intent rendering through Jinja templates."""

from __future__ import annotations

from ipaddress import ip_network
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from netcode.intent_utils import config_filename, template_for_intent
from netcode.adapters.registry import AdapterRegistry
from netcode.models import Intent, RenderResult
from netcode.paths import WorkspacePaths
from netcode.ui_config import configured_template_dir


def _variables(intent: Intent) -> dict[str, Any]:
    data = intent.model_dump()
    if intent.change_type == "add_vlan":
        network = ip_network(intent.vlan.subnet, strict=False)
        data["vlan"]["network"] = str(network.network_address)
        data["vlan"]["prefixlen"] = network.prefixlen
        data["vlan"]["netmask"] = str(network.netmask)
        if data["vlan"].get("svi", {}).get("enabled") and not data["vlan"]["svi"].get("gateway_ip"):
            hosts = network.hosts()
            data["vlan"]["svi"]["gateway_ip"] = str(next(hosts))
    return data


def render_intent(
    intent: Intent,
    paths: WorkspacePaths,
    *,
    platform: str = "arista_eos",
) -> RenderResult:
    template_name = template_for_intent(intent)
    normalized_platform = AdapterRegistry.normalize_execution_platform(platform)
    template_family = "arista" if normalized_platform == "arista_eos" else normalized_platform
    template_path = configured_template_dir(paths) / template_family / template_name
    if not template_path.exists():
        raise ValueError(
            f"No {normalized_platform} template is available for governed "
            f"'{intent.change_type}' execution."
        )
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
    path = paths.rendered / config_filename(intent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.config, encoding="utf-8")
    return path
