"""A gateway created before pinning must adopt its addresses on read.

Measured on the lab while verifying this very change: a gateway whose
`spec.internalIPs` was empty had its pods replaced, the new pods took
`10.199.16.5/.7`, and the gateway VPC's default route was left pointing at
`10.199.16.3` — an address that no longer existed. Every indicator stayed
green: VpcEgressGateway Ready, BGP Established, the peering intact, and every
packet the tenant sent went nowhere (backlog U6, discrepancy Д12).

Pinning at creation only helps gateways created afterwards. Anything already
on the cluster keeps churning until someone notices, so reading a gateway
adopts whatever addresses it currently holds.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import adopt_gateway_pins

GW = "team-a"


def _k8s(spec: dict, status: dict) -> MagicMock:
    veg = {"metadata": {"name": GW, "namespace": "kube-system"}, "spec": spec, "status": status}
    k8s = MagicMock()
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock(return_value=veg)
    k8s._veg = veg
    return k8s


@pytest.mark.asyncio
async def test_an_unpinned_gateway_adopts_its_current_addresses() -> None:
    k8s = _k8s(
        spec={"replicas": 2},
        status={"internalIPs": ["10.199.16.5", "10.199.16.7"],
                "externalIPs": ["10.199.4.14", "10.199.4.16"]},
    )

    adopted = await adopt_gateway_pins(k8s, GW, k8s._veg)

    assert adopted is True
    body = k8s.custom_api.patch_namespaced_custom_object.call_args.kwargs["body"]
    assert body["spec"]["internalIPs"] == ["10.199.16.5", "10.199.16.7"]
    assert body["spec"]["externalIPs"] == ["10.199.4.14", "10.199.4.16"]


@pytest.mark.asyncio
async def test_an_already_pinned_gateway_is_left_alone() -> None:
    """Never overwrite a pin with whatever the pods drifted to."""
    k8s = _k8s(
        spec={"internalIPs": ["10.199.16.2"], "externalIPs": ["10.199.4.11"]},
        status={"internalIPs": ["10.199.16.5"], "externalIPs": ["10.199.4.14"]},
    )

    adopted = await adopt_gateway_pins(k8s, GW, k8s._veg)

    assert adopted is False
    k8s.custom_api.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_a_gateway_with_no_addresses_yet_is_not_pinned_to_nothing() -> None:
    k8s = _k8s(spec={}, status={})

    assert await adopt_gateway_pins(k8s, GW, k8s._veg) is False
    k8s.custom_api.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_a_half_pinned_gateway_gets_the_missing_half() -> None:
    k8s = _k8s(
        spec={"externalIPs": ["10.199.4.11"]},
        status={"internalIPs": ["10.199.16.5"], "externalIPs": ["10.199.4.11"]},
    )

    assert await adopt_gateway_pins(k8s, GW, k8s._veg) is True
    body = k8s.custom_api.patch_namespaced_custom_object.call_args.kwargs["body"]
    assert body["spec"]["internalIPs"] == ["10.199.16.5"]
    assert "externalIPs" not in body["spec"], "an existing pin must not be rewritten"


@pytest.mark.asyncio
async def test_a_partial_status_is_not_adopted() -> None:
    """Adopting mid-reconcile would pin fewer addresses than there are replicas.

    Seen live: clearing the pins made kube-ovn replace a pod, and for a few
    seconds `status.internalIPs` held one address for a two-replica gateway.
    Adopting that pins the gateway to a single address — the second replica
    then has nowhere to come up, which is a worse failure than the churn this
    is meant to stop.
    """
    k8s = _k8s(
        spec={"replicas": 2},
        status={"internalIPs": ["10.199.16.7"], "externalIPs": ["10.199.4.16"]},
    )

    assert await adopt_gateway_pins(k8s, GW, k8s._veg) is False
    k8s.custom_api.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_a_complete_status_is_adopted() -> None:
    k8s = _k8s(
        spec={"replicas": 2},
        status={"internalIPs": ["10.199.16.5", "10.199.16.7"],
                "externalIPs": ["10.199.4.14", "10.199.4.16"]},
    )

    assert await adopt_gateway_pins(k8s, GW, k8s._veg) is True


@pytest.mark.asyncio
async def test_more_addresses_than_replicas_is_still_adopted() -> None:
    """A scale-down leaves status ahead of spec; that is not a partial read."""
    k8s = _k8s(
        spec={"replicas": 1},
        status={"internalIPs": ["10.199.16.5", "10.199.16.7"], "externalIPs": []},
    )

    assert await adopt_gateway_pins(k8s, GW, k8s._veg) is True
