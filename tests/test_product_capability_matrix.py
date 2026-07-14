from __future__ import annotations

from netcode.adapters.registry import AdapterRegistry
from netcode.adapters.rez import READ_TRANSPORTS
from netcode.product_capabilities import (
    FEATURES,
    STATUS_VALUES,
    product_support_matrix,
    unsupported_platform_row,
)


def test_matrix_covers_every_registered_read_platform_and_feature() -> None:
    matrix = product_support_matrix()
    rows = {row["platform"]: row for row in matrix["rows"]}

    assert set(rows) == set(READ_TRANSPORTS)
    for platform, row in rows.items():
        assert set(row["capabilities"]) == set(FEATURES), platform
        assert all(item["status"] in STATUS_VALUES for item in row["capabilities"].values())


def test_adapter_registration_never_implies_write_support() -> None:
    rows = {row["platform"]: row for row in product_support_matrix()["rows"]}
    live_write_statuses = {"GA", "pilot-certified"}

    for platform, row in rows.items():
        status = row["capabilities"]["write"]["status"]
        adapter = AdapterRegistry.EXECUTION_ADAPTERS.get(platform, {})
        if status in live_write_statuses:
            assert adapter.get("write_supported") is True
            assert {"dry_run", "apply", "verify", "rollback"}.issubset(set(adapter.get("capabilities", [])))


def test_pilot_certified_rows_have_named_evidence() -> None:
    for row in product_support_matrix()["rows"]:
        has_pilot_status = any(
            capability["status"] == "pilot-certified"
            for capability in row["capabilities"].values()
        )
        if has_pilot_status:
            assert row["evidence"], row["platform"]


def test_unknown_platform_fails_closed_for_every_feature() -> None:
    row = unsupported_platform_row("future-router-os")

    assert row["runtime_adapter_available"] is False
    assert set(row["capabilities"]) == set(FEATURES)
    assert {item["status"] for item in row["capabilities"].values()} == {"unsupported"}


def test_manager_paths_remain_hardware_blocked() -> None:
    rows = {row["platform"]: row for row in product_support_matrix()["rows"]}

    assert rows["fortimanager"]["capabilities"]["manager_execution"]["status"] == "hardware-blocked"
    assert rows["panorama"]["capabilities"]["manager_execution"]["status"] == "hardware-blocked"
