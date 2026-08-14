"""Deleting an egress gateway stranded it and its external address.

`shared-egress` sat on the lab cluster for 29 hours like this:

    $ kubectl -n kube-system get vpc-egress-gateways shared-egress
    PHASE      READY   EXTERNAL IPS
    Completed  true    ["10.198.190.207"]          # deletionTimestamp 29h ago

    $ kubectl get vpc egw-shared-egress
    Error from server (NotFound)

    E ... error syncing delete vpc egress gateway "kube-system/shared-egress":
        not found logical router "egw-shared-egress", requeuing

The teardown marked the VpcEgressGateway for deletion and then removed the VPC
it is finalized against, so the finalizer could never complete — the same
mistake `delete_vpc` made with subnets and NAT objects, on the gateway path.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException


def _env(gateway_present: bool, subnets: list[str]):
    k8s = MagicMock()

    async def _get(**kw):
        if gateway_present:
            return {"metadata": {"name": kw["name"]}}
        raise ApiException(status=404)

    async def _list(**kw):
        return {"items": [{"metadata": {"name": n}} for n in subnets]}

    k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=_get)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=_list)
    return k8s


@pytest.mark.asyncio
async def test_a_gateway_still_terminating_is_reported():
    from app.api.v1.egress_gateway import _await_gateway_gone

    k8s = _env(gateway_present=True, subnets=[])

    left = await _await_gateway_gone(k8s, "shared-egress", timeout=0)

    assert left == ["vpc-egress-gateway/shared-egress"]


@pytest.mark.asyncio
async def test_a_lingering_subnet_counts_too():
    from app.api.v1.egress_gateway import _await_gateway_gone

    k8s = _env(gateway_present=False, subnets=["egw-shared-egress-subnet"])

    left = await _await_gateway_gone(k8s, "shared-egress", timeout=0)

    assert left == ["subnet/egw-shared-egress-subnet"]


@pytest.mark.asyncio
async def test_nothing_left_means_the_vpc_may_go():
    from app.api.v1.egress_gateway import _await_gateway_gone

    k8s = _env(gateway_present=False, subnets=[])

    assert await _await_gateway_gone(k8s, "shared-egress", timeout=0) == []


@pytest.mark.asyncio
async def test_the_vpc_is_not_deleted_while_the_gateway_is(monkeypatch):
    """The whole point: the router has to outlive its finalizer."""
    from app.api.v1 import egress_gateway as mod

    k8s = _env(gateway_present=True, subnets=[])
    k8s.custom_api.delete_namespaced_custom_object = AsyncMock()
    k8s.custom_api.delete_cluster_custom_object = AsyncMock()

    monkeypatch.setattr(mod, "GATEWAY_DRAIN_TIMEOUT", 0.0)
    monkeypatch.setattr(mod, "_get_transit_allocator", AsyncMock(return_value=({}, None)))
    cleanup_vpc = AsyncMock()
    monkeypatch.setattr(mod, "_cleanup_gateway_vpc", cleanup_vpc)

    left = await mod._cleanup_gateway_resources(k8s, "shared-egress")

    assert left, "the caller has to learn the teardown did not finish"
    cleanup_vpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_endpoint_answers_409_rather_than_claiming_success(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import egress_gateway as mod

    request = MagicMock()
    request.app.state.k8s_client = MagicMock()

    monkeypatch.setattr(mod, "_get_gateway_config", AsyncMock(return_value={"name": "x"}))
    monkeypatch.setattr(mod, "_list_attached_vpcs", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        mod, "_cleanup_gateway_resources",
        AsyncMock(return_value=["vpc-egress-gateway/shared-egress"]),
    )

    with pytest.raises(HTTPException) as e:
        await mod.delete_egress_gateway(request, "shared-egress", user=MagicMock())

    assert e.value.status_code == 409
    assert "shared-egress" in e.value.detail
