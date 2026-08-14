"""Detaching a VPC from a gateway deleted a default route it never added.

`acme-net` was created with `0.0.0.0/0 -> 10.198.191.254` (the lab router).
Attaching it to an egress gateway adds a second default via the transit IP;
detaching removed **every** route with that CIDR, so the VPC came out with no
default at all — measured on the cluster, four static routes down to three,
and the one that survived nothing. The tenant lost its way out entirely, and
nothing in the response said so.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _vpc(routes):
    return {"metadata": {"name": "acme-net"}, "spec": {"staticRoutes": routes}}


def _k8s(routes):
    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value=_vpc(routes))
    k8s.custom_api.patch_cluster_custom_object = AsyncMock()
    return k8s


def _written(k8s):
    call = k8s.custom_api.patch_cluster_custom_object.await_args
    return call.kwargs["body"]["spec"]["staticRoutes"] if call else None


ROUTES = [
    {"cidr": "10.16.0.0/16", "nextHopIP": "169.254.101.26", "policy": "policyDst"},
    {"cidr": "0.0.0.0/0", "nextHopIP": "10.198.191.254", "policy": "policyDst"},
    {"cidr": "0.0.0.0/0", "nextHopIP": "10.255.0.1", "policy": "policyDst"},
]


@pytest.mark.asyncio
async def test_only_the_gateways_own_default_goes():
    from app.api.v1.egress_gateway import _remove_static_route

    k8s = _k8s(list(ROUTES))

    await _remove_static_route(k8s, "acme-net", "0.0.0.0/0", next_hop="10.255.0.1")

    left = _written(k8s)
    assert {"cidr": "0.0.0.0/0", "nextHopIP": "10.198.191.254", "policy": "policyDst"} in left, \
        "the route the VPC was created with must survive a detach"
    assert not any(r["nextHopIP"] == "10.255.0.1" for r in left)


@pytest.mark.asyncio
async def test_without_a_next_hop_it_still_matches_by_cidr():
    """The gateway VPC's return route is unique by CIDR; that call stays."""
    from app.api.v1.egress_gateway import _remove_static_route

    k8s = _k8s([{"cidr": "10.100.0.0/24", "nextHopIP": "10.255.0.2"}])

    await _remove_static_route(k8s, "egw-labgw", "10.100.0.0/24")

    assert _written(k8s) == []


@pytest.mark.asyncio
async def test_nothing_matching_means_no_write():
    from app.api.v1.egress_gateway import _remove_static_route

    k8s = _k8s(list(ROUTES))

    await _remove_static_route(k8s, "acme-net", "0.0.0.0/0", next_hop="10.255.9.9")

    k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


def test_the_detach_passes_the_transit_ip() -> None:
    from pathlib import Path

    src = Path("app/api/v1/egress_gateway.py").read_text()
    body = src[src.index("# 3. Remove the default route"):]
    body = body[:body.index("# 4.")]

    assert "next_hop=gw_transit_ip" in body
