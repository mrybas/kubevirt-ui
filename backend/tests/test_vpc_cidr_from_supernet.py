"""A tenant's CIDR must come out of the supernet everything else agrees on.

The allocator carried its own address plan: base 200 and a /24 mask, both
constants, and it never read `TENANT_SUPERNET` at all. That left three
contracts living apart, agreeing only by luck:

  * the allocator hands out `10.{200+N}.0.0/24`;
  * `TENANT_SUPERNET` is `10.200.0.0/14` — which covers exactly the first four
    of those, so the fifth VPC is allocated outside the range the isolation
    ACLs are scoped to;
  * the border router accepts `10.200.0.0/14{22,22}` — /22 only, so a /24 is
    rejected outright and a VPC created the normal way cannot be announced at
    all.

Measured on the lab: `bird.conf:39` is the {22,22} filter, and 10.204.0.0/24
(the fifth VPC) is outside 10.200.0.0/14.

One env now decides all three.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.allocators import _allocate_vpc_cidr_once, vpc_prefix_len, vpc_supernet


def _k8s(existing: list[tuple[str, str]], next_index: int = 0) -> MagicMock:
    k8s = MagicMock()

    cm = MagicMock()
    cm.data = {"next_index": str(next_index)}
    cm.metadata.resource_version = "1"
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.replace_namespaced_config_map = AsyncMock()
    k8s.core_api.create_namespaced_config_map = AsyncMock(return_value=cm)

    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={
        "items": [
            {"metadata": {"name": n}, "spec": {"cidrBlock": c}} for n, c in existing
        ],
    })
    return k8s


@pytest.mark.asyncio
async def test_the_first_vpc_starts_at_the_supernet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/14")
    monkeypatch.delenv("TENANT_VPC_PREFIX", raising=False)

    cidr, gateway = await _allocate_vpc_cidr_once(_k8s([]))

    assert cidr == "10.200.0.0/22"
    assert gateway == "10.200.0.1"


@pytest.mark.asyncio
async def test_the_prefix_matches_what_the_border_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lab's BIRD filter is `10.200.0.0/14{22,22}` — /24 is dropped."""
    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/14")
    monkeypatch.delenv("TENANT_VPC_PREFIX", raising=False)

    assert vpc_prefix_len() == 22


@pytest.mark.asyncio
async def test_every_allocation_stays_inside_the_supernet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old scheme left the range at the fifth VPC."""
    import ipaddress

    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/14")
    supernet = ipaddress.ip_network("10.200.0.0/14")

    taken: list[tuple[str, str]] = []
    for i in range(12):
        cidr, _ = await _allocate_vpc_cidr_once(_k8s(taken))
        assert ipaddress.ip_network(cidr).subnet_of(supernet), f"VPC #{i} escaped"
        taken.append((f"vpc-{i}", cidr))


@pytest.mark.asyncio
async def test_an_occupied_range_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/14")

    cidr, _ = await _allocate_vpc_cidr_once(_k8s([("someone-else", "10.200.0.0/22")]))

    assert cidr == "10.200.4.0/22"


@pytest.mark.asyncio
async def test_a_hand_written_overlap_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A range taken by hand is invisible to a counter but not to the scan."""
    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/14")

    cidr, _ = await _allocate_vpc_cidr_once(
        _k8s([("by-hand", "10.200.2.0/23")], next_index=0),
    )

    import ipaddress
    assert not ipaddress.ip_network(cidr).overlaps(ipaddress.ip_network("10.200.2.0/23"))


@pytest.mark.asyncio
async def test_a_full_supernet_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """"max 55 VPCs" was a number from the old address plan, not from config."""
    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/22")
    monkeypatch.setenv("TENANT_VPC_PREFIX", "22")

    with pytest.raises(HTTPException) as e:
        await _allocate_vpc_cidr_once(_k8s([("only-one", "10.200.0.0/22")]))

    assert e.value.status_code == 409
    assert "supernet" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_the_prefix_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/14")
    monkeypatch.setenv("TENANT_VPC_PREFIX", "24")

    cidr, _ = await _allocate_vpc_cidr_once(_k8s([]))

    assert cidr == "10.200.0.0/24"


def test_the_supernet_default_is_the_deployment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TENANT_SUPERNET", raising=False)
    assert vpc_supernet() == "10.200.0.0/14"
