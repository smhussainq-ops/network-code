"""Netcode command line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from netcode.adapters.registry import AdapterRegistry
from netcode.bootstrap import init_workspace
from netcode.inventory import Inventory
from netcode.jobs import JobRunner
from netcode.lab import lab_status, run_arista_end_to_end, run_lab_action
from netcode.orchestrator import create_add_vlan_intent, run_static_pipeline
from netcode.paths import paths
from netcode.store import PlatformStore, record_to_dict

app = typer.Typer(help="Network-as-code training and validation platform.")
lab_app = typer.Typer(help="Arista lab operations.")
wizard_app = typer.Typer(help="Guided intent wizards.")
adapters_app = typer.Typer(help="Adapter registry and Rez bridge operations.")
changes_app = typer.Typer(help="Durable change/job store.")
app.add_typer(lab_app, name="lab")
app.add_typer(wizard_app, name="wizard")
app.add_typer(adapters_app, name="adapters")
app.add_typer(changes_app, name="changes")
console = Console()


@app.command()
def init(force: bool = typer.Option(False, help="Overwrite default seed files.")) -> None:
    """Create the platform workspace structure and seed files."""
    p = paths()
    written = init_workspace(p, force=force)
    console.print(f"Workspace: {p.root}")
    for path in written:
        console.print(f"created {path}")
    if not written:
        console.print("Workspace already initialized.")


@wizard_app.command("add-vlan")
def wizard_add_vlan(
    site: str = typer.Option("store-1842", prompt=True),
    device_id: str = typer.Option("v2-store1", prompt=True),
    vlan_id: int = typer.Option(90, prompt=True),
    name: str = typer.Option("GUEST_WIFI", prompt=True),
    subnet: str = typer.Option("10.42.90.0/24", prompt=True),
    purpose: str = typer.Option("guest", prompt=True),
    pci_reachable: bool = typer.Option(False, help="Whether the VLAN may reach PCI/POS networks."),
    requested_by: str = typer.Option("lab-engineer", prompt=True),
) -> None:
    """Create an add_vlan intent through prompts."""
    p = paths()
    intent_path = create_add_vlan_intent(p, site, device_id, vlan_id, name, subnet, purpose, pci_reachable, requested_by)
    console.print(f"Intent written: {intent_path}")
    result = run_static_pipeline(p, intent_path)
    _print_pipeline(result.model_dump())


@app.command()
def pipeline(intent_path: Path) -> None:
    """Render, validate, and report on an intent."""
    result = run_static_pipeline(paths(), intent_path)
    _print_pipeline(result.model_dump())


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8088),
    reload: bool = typer.Option(False),
) -> None:
    """Run the web UI and API."""
    import uvicorn

    init_workspace(paths())
    uvicorn.run("netcode.api:app", host=host, port=port, reload=reload)


@lab_app.command("status")
def lab_status_cmd() -> None:
    """Show local containerlab status."""
    console.print_json(json.dumps(lab_status()))


@lab_app.command("dry-run")
def lab_dry_run(intent_path: Path, device: Optional[str] = typer.Option(None, "--device")) -> None:
    """Load candidate config into an EOS config session and abort it."""
    result = JobRunner(paths()).run_lab_action(intent_path, "dry-run", device)
    console.print_json(json.dumps(result, indent=2))


@lab_app.command("apply")
def lab_apply(intent_path: Path, device: Optional[str] = typer.Option(None, "--device")) -> None:
    """Apply a validated change to the lab and verify it."""
    result = JobRunner(paths()).run_lab_action(intent_path, "apply", device)
    console.print_json(json.dumps(result, indent=2))


@lab_app.command("rollback")
def lab_rollback(intent_path: Path, device: Optional[str] = typer.Option(None, "--device")) -> None:
    """Remove the VLAN created by the add_vlan workflow from the lab."""
    result = JobRunner(paths()).run_lab_action(intent_path, "rollback", device)
    console.print_json(json.dumps(result, indent=2))


@lab_app.command("full-run")
def lab_full_run(
    intent_path: Path,
    device: Optional[str] = typer.Option(None, "--device"),
    apply: bool = typer.Option(True, "--apply/--dry-run-only", help="Commit to the lab after dry-run passes."),
) -> None:
    """Run static validation, Arista dry-run, optional lab apply, verify, and write reports."""
    result = JobRunner(paths()).run_full_arista(intent_path, device, apply=apply)
    console.print_json(json.dumps(result, indent=2))


@adapters_app.command("list")
def adapters_list() -> None:
    """List execution adapters and Rez-provided state adapters."""
    console.print_json(json.dumps(AdapterRegistry().summary(), indent=2))


@adapters_app.command("device")
def adapters_device(device_id: str) -> None:
    """Show adapter capabilities for a device."""
    p = paths()
    inventory = Inventory(p.inventories / "lab.yaml")
    device = inventory.by_id.get(device_id)
    if not device:
        raise typer.BadParameter(f"Unknown device {device_id}")
    console.print_json(json.dumps(AdapterRegistry().device_capabilities(device), indent=2))


@adapters_app.command("collect-state")
def adapters_collect_state(device_id: str) -> None:
    """Collect live state through the Rez driver bridge."""
    p = paths()
    inventory = Inventory(p.inventories / "lab.yaml")
    device = inventory.by_id.get(device_id)
    if not device:
        raise typer.BadParameter(f"Unknown device {device_id}")
    console.print_json(json.dumps(AdapterRegistry().rez.collect_device_state(device), indent=2, default=str))


@changes_app.command("list")
def changes_list() -> None:
    """List persisted changes."""
    store = PlatformStore(paths())
    console.print_json(json.dumps([record_to_dict(record) for record in store.list_changes()], indent=2))


@changes_app.command("jobs")
def jobs_list() -> None:
    """List persisted jobs."""
    store = PlatformStore(paths())
    console.print_json(json.dumps([record_to_dict(record) for record in store.list_jobs()], indent=2))


def _print_pipeline(result: dict[str, object]) -> None:
    status = str(result["status"]).upper()
    console.print(f"Pipeline verdict: {status}")
    validation = result["validation"]
    assert isinstance(validation, dict)
    checks = validation["checks"]
    table = Table("Check", "Status", "Message")
    for check in checks:
        table.add_row(check["title"], check["status"].upper(), check["message"])
    console.print(table)
    artifacts = result.get("artifacts")
    if artifacts:
        console.print_json(json.dumps(artifacts, indent=2))


if __name__ == "__main__":
    app()
