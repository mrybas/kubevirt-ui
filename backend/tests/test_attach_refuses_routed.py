"""The hazard moved one screen across rather than going away.

The VPC page stopped offering "Configure egress gateway" for a VPC on the
routed plane, because the hub rewrites its default route to the gateway's
transit address and takes it off its own leg — traffic that works stops. The
Egress Gateways page kept offering the same attach, from the other direction,
with no check at all.

Predicate as everywhere else on B3: the default route's next hop is inside the
external subnet. The datapath, not a label someone has to remember to set.

Live: attaching `b3v` returned 422 with the sentence above, and its default
route `0.0.0.0/0 -> 10.199.4.254` was untouched; attaching `t1-vpc`, a hub
tenant, still returned its transit IP unchanged.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import _reject_routed_vpc
from fastapi import HTTPException


def _k8s(vpcs, subnets):
    async def list_obj(**kw):
        return {"items": subnets if kw["plural"] == "subnets" else vpcs}

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    return k8s


EXTERNAL = [{"metadata": {"name": "external"},
             "spec": {"vpc": "ovn-cluster", "cidrBlock": "10.199.4.0/22"}}]


class TestARoutedVpcIsRefused:
    @pytest.mark.asyncio
    async def test_refused_with_the_consequence_stated(self, monkeypatch) -> None:
        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setenv("B3_EXTERNAL_SUBNET", "external")

        k8s = _k8s([{"metadata": {"name": "b3v"}, "spec": {"staticRoutes": [
            {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.4.254",
             "policy": "policyDst"}]}}], EXTERNAL)

        with pytest.raises(HTTPException) as e:
            await _reject_routed_vpc(k8s, "b3v")

        assert e.value.status_code == 422
        assert "own router leg" in e.value.detail
        assert "would stop" in e.value.detail

    @pytest.mark.asyncio
    async def test_a_hub_tenant_is_not_refused(self, monkeypatch) -> None:
        """Its default route points at a gateway transit, not the external
        plane — attaching is exactly what it is for."""
        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setenv("B3_EXTERNAL_SUBNET", "external")

        k8s = _k8s([{"metadata": {"name": "t1-vpc"}, "spec": {"staticRoutes": [
            {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.129.1",
             "policy": "policyDst"}]}}], EXTERNAL)

        await _reject_routed_vpc(k8s, "t1-vpc")     # no raise

    @pytest.mark.asyncio
    async def test_a_vpc_with_no_default_route_is_not_refused(self, monkeypatch) -> None:
        """A brand-new VPC has nowhere to egress yet; attaching is how it gets
        one, and refusing here would block the ordinary path."""
        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setenv("B3_EXTERNAL_SUBNET", "external")

        k8s = _k8s([{"metadata": {"name": "fresh"}, "spec": {}}], EXTERNAL)

        await _reject_routed_vpc(k8s, "fresh")


class TestWhereTheCheckMustNotFire:
    @pytest.mark.asyncio
    async def test_without_b3_there_is_no_routed_plane_to_protect(
        self, monkeypatch,
    ) -> None:
        """On a deployment with no external plane every VPC is a hub tenant,
        and a check that refuses them all would break the only egress there is."""
        monkeypatch.delenv("B3_BGP_PEER", raising=False)
        monkeypatch.delenv("B3_VPC_GATEWAY", raising=False)

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(
            side_effect=AssertionError("must not even look"))

        await _reject_routed_vpc(k8s, "anything")

    @pytest.mark.asyncio
    async def test_a_missing_external_subnet_does_not_block_attaching(
        self, monkeypatch,
    ) -> None:
        """B3 configured but the subnet absent is a broken deployment, not a
        reason to refuse the fallback path."""
        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setenv("B3_EXTERNAL_SUBNET", "external")

        k8s = _k8s([], [])

        await _reject_routed_vpc(k8s, "anything")


class TestItIsWiredIntoTheOnlyPathThatAttaches:
    def test_the_shared_function_calls_it(self) -> None:
        """Both the gateway page and the tenant flow go through
        `attach_tenant_to_gateway`; guarding the endpoint alone would leave the
        other caller open."""
        from pathlib import Path

        src = Path("app/api/v1/egress_gateway.py").read_text()
        body = src[src.index("async def attach_tenant_to_gateway("):]
        body = body[:body.index("\nasync def ", 10)]

        assert "await _reject_routed_vpc(k8s, tenant_vpc_name)" in body
