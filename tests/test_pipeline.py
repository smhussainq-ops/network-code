from pathlib import Path

from netcode.bootstrap import init_workspace
from netcode.orchestrator import run_static_pipeline
from netcode.paths import WorkspacePaths
from netcode.yamlio import write_yaml


def test_example_pipeline_passes(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)

    result = run_static_pipeline(paths, paths.intents / "examples" / "add_guest_vlan.yaml")

    assert result.status == "pass"
    assert "vlan 90" in result.render.config
    assert "name GUEST_WIFI" in result.render.config
    target_check = next(check for check in result.validation.checks if check.id == "targets")
    assert target_check.evidence["devices"] == ["v2-store1"]
    assert result.artifacts is not None
    assert Path(result.artifacts.report_markdown_path).exists()


def test_guest_vlan_cannot_be_pci_reachable(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    bad_intent = {
        "change_type": "add_vlan",
        "site": "store-1842",
        "targets": {"device_ids": ["v2-store1"]},
        "vlan": {
            "id": 91,
            "name": "GUEST_BAD",
            "subnet": "10.42.91.0/24",
            "purpose": "guest",
            "svi": {"enabled": False},
        },
        "policy": {"pci_reachable": True},
    }
    path = paths.intents / "bad.yaml"
    write_yaml(path, bad_intent)

    result = run_static_pipeline(paths, path)

    assert result.status == "fail"
    failed_ids = {check.id for check in result.validation.checks if check.status == "fail"}
    assert "segmentation" in failed_ids


def test_subnet_overlap_fails(tmp_path: Path):
    paths = WorkspacePaths(tmp_path)
    init_workspace(paths)
    bad_intent = {
        "change_type": "add_vlan",
        "site": "store-1842",
        "targets": {"device_ids": ["v2-store1"]},
        "vlan": {
            "id": 92,
            "name": "OPS_NET",
            "subnet": "10.42.30.0/24",
            "purpose": "ops",
            "svi": {"enabled": False},
        },
        "policy": {"pci_reachable": False},
    }
    path = paths.intents / "overlap.yaml"
    write_yaml(path, bad_intent)

    result = run_static_pipeline(paths, path)

    assert result.status == "fail"
    failed_ids = {check.id for check in result.validation.checks if check.status == "fail"}
    assert "subnet_overlap" in failed_ids
