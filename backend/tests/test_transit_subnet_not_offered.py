"""The control-plane transit subnet must not be offered as an egress uplink.

The egress gateway form lists VLAN-backed subnets for its External Network.
`cp-transit` is VLAN-backed, so it sat in that list next to `external` — and
picking it points a gateway's internet leg at the control plane (backlog U8).

Filtering on `purpose` does not work: both subnets are built by the underlay
flow, so both carry `purpose: infrastructure`. Using it here would hide the one
subnet the gateway actually needs.

Two signals identify a transit subnet without inventing a label:
  * the name in `TENANTS_CP_TRANSIT_SUBNET`, which the tenant flow already
    needs and this deployment already sets;
  * any subnet attached to a VPC as `extraExternalSubnets` — that is what being
    used as a transit plane looks like, and it covers brownfield clusters and
    more than one transit.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.network import transit_subnet_names


def _vpc(name: str, externals: list[str]) -> dict:
    return {"metadata": {"name": name}, "spec": {"extraExternalSubnets": externals}}


def _k8s(vpcs: list[dict]) -> MagicMock:
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": vpcs})
    return k8s


@pytest.mark.asyncio
async def test_the_configured_transit_subnet_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "cp-transit")

    assert "cp-transit" in await transit_subnet_names(_k8s([]))


@pytest.mark.asyncio
async def test_a_subnet_attached_as_an_external_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TENANTS_CP_TRANSIT_SUBNET", raising=False)
    k8s = _k8s([_vpc("team-a", ["cp-transit"]), _vpc("team-b", ["cp-transit"])])

    assert await transit_subnet_names(k8s) == {"cp-transit"}


@pytest.mark.asyncio
async def test_the_egress_underlay_is_not_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`external` is what the gateway needs; hiding it would be the worse bug."""
    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "cp-transit")
    k8s = _k8s([_vpc("team-a", ["cp-transit"])])

    assert "external" not in await transit_subnet_names(k8s)


@pytest.mark.asyncio
async def test_an_unreadable_cluster_hides_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing to look is not a reason to drop the only usable option."""
    from kubernetes_asyncio.client import ApiException

    monkeypatch.delenv("TENANTS_CP_TRANSIT_SUBNET", raising=False)
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=500, reason="boom"),
    )

    assert await transit_subnet_names(k8s) == set()


@pytest.mark.asyncio
async def test_the_subnets_route_still_belongs_to_list_subnets() -> None:
    """A helper inserted above a decorated function steals its route.

    Adding `transit_subnet_names` immediately before `list_subnets` put it
    between `@router.get("/subnets")` and the function it decorates, so the
    route resolved to the helper — which takes a bare `k8s` argument, so
    FastAPI demanded it as a query parameter and `/network/subnets` started
    answering 422 to everyone. The whole backend suite stayed green; only
    calling the endpoint showed it.
    """
    from app.api.v1.network import router

    handlers = {
        (m, r.path): r.endpoint.__name__
        for r in router.routes
        if getattr(r, "path", "") == "/subnets"
        for m in getattr(r, "methods", set())
    }
    assert handlers.get(("GET", "/subnets")) == "list_subnets"
