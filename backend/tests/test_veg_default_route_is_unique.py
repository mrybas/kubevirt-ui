"""A VPC must end up with exactly one route per destination prefix.

kube-ovn writes its own `0.0.0.0/0` on a VPC created with a NAT gateway.
Attaching an egress gateway used to append a second default instead of
replacing it, leaving the tenant VPC with two:

    {0.0.0.0/0 -> 10.199.0.1}      # kube-ovn, via the transit subnet gateway
    {0.0.0.0/0 -> 10.199.129.1}    # attach, via the gateway VPC

`EnableEcmp` is false, so OVN programs one of them and the choice is not
ours. Measured on the lab: attach picked the working hop, a later reconcile
flipped to the stale one, and tenant egress died silently — `lr-route-list`
showed `0.0.0.0/0 10.199.0.1` while the spec still looked "correct" because
it listed both.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import _add_static_route


def _k8s(routes: list[dict]) -> tuple[MagicMock, dict]:
    captured: dict = {}

    async def get_obj(**kw):
        return {"metadata": {"name": kw["name"]}, "spec": {"staticRoutes": routes}}

    async def patch_obj(**kw):
        captured["body"] = kw["body"]
        return {}

    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    return k8s, captured


@pytest.mark.asyncio
async def test_a_default_route_replaces_the_one_kube_ovn_wrote() -> None:
    k8s, captured = _k8s([
        {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.0.1", "policy": "policyDst"},
    ])

    await _add_static_route(k8s, "team-a", "0.0.0.0/0", "10.199.129.1")

    routes = captured["body"]["spec"]["staticRoutes"]
    defaults = [r for r in routes if r["cidr"] == "0.0.0.0/0"]
    assert len(defaults) == 1, f"two defaults compete and OVN picks one: {routes}"
    assert defaults[0]["nextHopIP"] == "10.199.129.1"


@pytest.mark.asyncio
async def test_routes_for_other_prefixes_are_left_alone() -> None:
    k8s, captured = _k8s([
        {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.0.1", "policy": "policyDst"},
        {"cidr": "10.200.0.0/22", "nextHopIP": "10.199.129.2", "policy": "policyDst"},
    ])

    await _add_static_route(k8s, "gw", "0.0.0.0/0", "10.199.128.2")

    routes = captured["body"]["spec"]["staticRoutes"]
    assert {"cidr": "10.200.0.0/22", "nextHopIP": "10.199.129.2",
            "policy": "policyDst"} in routes
    assert len(routes) == 2


@pytest.mark.asyncio
async def test_writing_the_same_route_twice_does_not_patch() -> None:
    k8s, captured = _k8s([
        {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.129.1", "policy": "policyDst"},
    ])

    await _add_static_route(k8s, "team-a", "0.0.0.0/0", "10.199.129.1")

    assert "body" not in captured, "an unchanged route must not cause a write"


@pytest.mark.asyncio
async def test_a_second_next_hop_for_a_tenant_prefix_replaces_the_first() -> None:
    """Re-attaching a tenant after its transit IP changed must not leave both."""
    k8s, captured = _k8s([
        {"cidr": "10.200.0.0/22", "nextHopIP": "10.199.129.2", "policy": "policyDst"},
    ])

    await _add_static_route(k8s, "gw", "10.200.0.0/22", "10.199.129.5")

    routes = captured["body"]["spec"]["staticRoutes"]
    assert routes == [
        {"cidr": "10.200.0.0/22", "nextHopIP": "10.199.129.5", "policy": "policyDst"},
    ]
