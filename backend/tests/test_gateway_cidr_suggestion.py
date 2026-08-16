"""The gateway form must not propose a range the cluster already uses.

The wizard's default was the constant `10.199.0.0/24`, which on this
deployment sits inside `cp-transit` (10.199.0.0/22). The overlap check then
reported a cheerful "No CIDR overlaps detected" for the pair it had just
suggested (backlog U7).

The allocator already walks past occupied ranges before handing one out — the
only thing it does that a suggestion must not is persist its counter. So the
suggestion is that walk, without the write.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import suggest_gateway_cidrs


def _k8s(cidrs: list[tuple[str, str]]) -> MagicMock:
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={
        "items": [
            {"metadata": {"name": n}, "spec": {"cidrBlock": c}} for n, c in cidrs
        ],
    })
    return k8s


@pytest.mark.asyncio
async def test_the_suggestion_avoids_the_transit_subnet() -> None:
    """The exact collision the hardcoded default walked into."""
    k8s = _k8s([("cp-transit", "10.199.0.0/22"), ("external", "10.199.4.0/22")])

    out = await suggest_gateway_cidrs(k8s)

    import ipaddress
    gw = ipaddress.ip_network(out["gw_vpc_cidr"])
    transit = ipaddress.ip_network(out["transit_cidr"])
    for taken in ("10.199.0.0/22", "10.199.4.0/22"):
        assert not gw.overlaps(ipaddress.ip_network(taken))
        assert not transit.overlaps(ipaddress.ip_network(taken))


@pytest.mark.asyncio
async def test_the_two_suggestions_do_not_overlap_each_other() -> None:
    import ipaddress

    out = await suggest_gateway_cidrs(_k8s([]))

    assert not ipaddress.ip_network(out["gw_vpc_cidr"]).overlaps(
        ipaddress.ip_network(out["transit_cidr"])
    )


@pytest.mark.asyncio
async def test_nothing_is_written_while_suggesting() -> None:
    """A suggestion that consumed a range would burn one per page load."""
    k8s = _k8s([])

    await suggest_gateway_cidrs(k8s)

    k8s.core_api.create_namespaced_config_map.assert_not_called()
    k8s.core_api.replace_namespaced_config_map.assert_not_called()
    k8s.custom_api.create_cluster_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_calls_are_stable() -> None:
    k8s = _k8s([("cp-transit", "10.199.0.0/22")])

    first = await suggest_gateway_cidrs(k8s)
    second = await suggest_gateway_cidrs(k8s)

    assert first == second, "the same cluster state must suggest the same ranges"


@pytest.mark.asyncio
async def test_a_full_pool_reports_rather_than_guesses() -> None:
    """Better an empty answer the form can handle than a colliding one."""
    k8s = _k8s([("everything", "10.0.0.0/8")])

    out = await suggest_gateway_cidrs(k8s, pool="10.199.0.0/16")

    assert out["gw_vpc_cidr"] == ""
    assert out["transit_cidr"] == ""
