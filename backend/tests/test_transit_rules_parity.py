"""The transit plane's rules, asserted by both implementations.

The table was taken once from this module — it is the reference — and is now
read by the operator's suite too. Byte for byte on purpose: an allow is keyed on
an address and the deny is what it punches through, so a difference of one
prefix is a tenant that cannot reach its control plane, or one that can reach
somebody else's.

If this file changes the operator's copy goes red, which is the point: while
both write these rules, the two must not drift.
"""

import json
from pathlib import Path

import pytest

_MOUNTED = Path("/test/parity/transit-rules.json")
_IN_REPO = Path(__file__).resolve().parents[2] / "test" / "parity" / "transit-rules.json"
TABLE = _MOUNTED if _MOUNTED.exists() else _IN_REPO


def _table():
    return json.loads(TABLE.read_text())


def test_the_allows_are_what_the_table_says() -> None:
    from app.core.tenant_transit import build_transit_acls

    example = _table()["allowsExample"]
    assert build_transit_acls(
        example["eip"], example["vip"], example["tcp"], example["udp"],
    ) == example["rules"]


@pytest.mark.parametrize("case", [
    pytest.param(c, id=c["name"]) for c in _table()["cases"]
])
def test_the_ranges_and_the_deny_are_what_the_table_says(case) -> None:
    from app.core.tenant_transit import _allocatable_ranges, build_transit_deny

    assert _allocatable_ranges(case["cidr"], case["excludeIps"]) == case["ranges"]
    assert build_transit_deny(case["cidr"], case["excludeIps"]) == case["deny"]
