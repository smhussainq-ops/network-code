from types import SimpleNamespace

from netcode.lab import (
    AristaEOSLabAdapter,
    _running_config_contains_fragment,
)
from netcode.models import CustomConfigIntent


def _intent(
    *,
    verify_contains: str,
    verify_absent: bool = False,
) -> CustomConfigIntent:
    return CustomConfigIntent.model_validate(
        {
            "site": "test",
            "targets": {"device_ids": ["device-1"]},
            "custom": {
                "config_lines": "description reviewed",
                "rollback_lines": "no description reviewed",
                "verify_contains": verify_contains,
                "verify_absent": verify_absent,
            },
        }
    )


def _verify(
    running_config: str,
    *,
    verify_contains: str,
    present: bool,
    verify_absent: bool = False,
):
    adapter = object.__new__(AristaEOSLabAdapter)
    adapter.device = SimpleNamespace(id="device-1")
    adapter.show = lambda _command: running_config
    return adapter._verify_custom(
        _intent(
            verify_contains=verify_contains,
            verify_absent=verify_absent,
        ),
        present,
    )


def test_positive_fragment_does_not_match_negated_configuration() -> None:
    running_config = """
router bgp 65000
   address-family ipv4
      no neighbor 10.99.1.2 activate
"""

    assert not _running_config_contains_fragment(
        running_config,
        "neighbor 10.99.1.2 activate",
    )


def test_fragment_preserves_contains_semantics_for_positive_lines() -> None:
    running_config = """
router bgp 65000
   address-family ipv4
      neighbor 10.99.1.2 remote-as 65100
"""

    assert _running_config_contains_fragment(
        running_config,
        "  neighbor 10.99.1.2  ",
    )


def test_multiline_fragment_requires_contiguous_same_polarity_lines() -> None:
    running_config = """
router bgp 65000
   address-family ipv4
      neighbor 10.99.1.2 activate
      neighbor 10.99.2.2 activate
"""

    assert _running_config_contains_fragment(
        running_config,
        """
        address-family ipv4
        neighbor 10.99.1.2
        """,
    )
    assert not _running_config_contains_fragment(
        running_config,
        """
        router bgp 65000
        neighbor 10.99.1.2 activate
        """,
    )


def test_custom_rollback_accepts_persisted_negated_line() -> None:
    result = _verify(
        """
router bgp 65000
   address-family ipv4
      no neighbor 10.99.1.2 activate
""",
        verify_contains="neighbor 10.99.1.2 activate",
        present=False,
    )

    assert result.status == "pass"
    assert result.evidence["found"] is False
    assert result.evidence["expected_found"] is False


def test_custom_rollback_does_not_false_pass_partial_positive_line() -> None:
    result = _verify(
        """
router bgp 65000
   neighbor 10.99.1.2 remote-as 65100
""",
        verify_contains="neighbor 10.99.1.2",
        present=False,
    )

    assert result.status == "fail"
    assert result.evidence["found"] is True
    assert result.evidence["expected_found"] is False


def test_verify_absent_apply_and_rollback_preserve_polarity() -> None:
    apply_result = _verify(
        "router bgp 65000\n",
        verify_contains="ip route 10.90.90.0/25 Null0",
        verify_absent=True,
        present=True,
    )
    rollback_result = _verify(
        "ip route 10.90.90.0/25 Null0 250\n",
        verify_contains="ip route 10.90.90.0/25 Null0",
        verify_absent=True,
        present=False,
    )

    assert apply_result.status == "pass"
    assert rollback_result.status == "pass"
