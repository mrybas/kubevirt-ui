"""Unit tests for VPC peering.

Without peering, a tenant reaching a shared VPC goes tenant -> egress gateway
-> lab gateway -> other egress gateway -> target. The trap is that a
VpcEgressGateway installs a reroute policy at priority 29100 catching
everything leaving the VPC, so it beats the peering static route and the
traffic hairpins anyway — the peering only works with a higher-priority allow
above it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

from app.api.v1 import vpcs
from app.api.v1.vpcs import (
    PEERING_POLICY_PRIORITY,
    _peering_side_patch,
    _remove_peering_side,
)
from app.core.allocators import peering_link_addresses
from app.models.vpc import VpcPeeringCreateRequest

T1 = "10.198.224.0/20"
T2 = "10.198.240.0/20"


class TestPeeringSidePatch:
    def _patch(self, spec: dict | None = None) -> dict:
        return _peering_side_patch(
            spec if spec is not None else {},
            remote_vpc="t2-vpc",
            local_connect_ip="169.254.101.1",
            link_cidr="169.254.101.0/30",
            remote_cidrs=[T2],
            remote_connect_ip="169.254.101.2",
        )

    def test_link_is_recorded_with_the_prefix_length(self) -> None:
        peering = self._patch()["vpcPeerings"][0]
        assert peering["remoteVpc"] == "t2-vpc"
        assert peering["localConnectIP"] == "169.254.101.1/30"

    def test_static_route_points_at_the_peer_over_the_link(self) -> None:
        route = self._patch()["staticRoutes"][0]
        assert route["cidr"] == T2
        assert route["nextHopIP"] == "169.254.101.2"
        assert route["policy"] == "policyDst"

    def test_policy_route_outranks_the_gateway_reroute(self) -> None:
        # The gateway's catch-all sits at 29100; anything lower hairpins.
        policy = self._patch()["policyRoutes"][0]
        assert policy["priority"] == PEERING_POLICY_PRIORITY
        assert policy["priority"] > 29100
        assert policy["action"] == "allow"
        assert policy["match"] == f"ip4.dst == {T2}"

    def test_existing_unrelated_entries_survive(self) -> None:
        spec = {
            "vpcPeerings": [{"remoteVpc": "t3-vpc", "localConnectIP": "169.254.101.5/30"}],
            "staticRoutes": [{"cidr": "0.0.0.0/0", "nextHopIP": "10.198.224.1"}],
            "policyRoutes": [{"priority": 30000, "action": "allow", "match": "ip4.dst == 10.0.0.0/8"}],
        }
        patch = self._patch(spec)
        assert any(p["remoteVpc"] == "t3-vpc" for p in patch["vpcPeerings"])
        assert any(r["cidr"] == "0.0.0.0/0" for r in patch["staticRoutes"])
        assert any(p["match"] == "ip4.dst == 10.0.0.0/8" for p in patch["policyRoutes"])

    def test_re_peering_converges_instead_of_duplicating(self) -> None:
        # Same pair peered twice must not stack two links or two routes.
        first = self._patch()
        second = _peering_side_patch(
            first, remote_vpc="t2-vpc", local_connect_ip="169.254.101.1",
            link_cidr="169.254.101.0/30", remote_cidrs=[T2],
            remote_connect_ip="169.254.101.2",
        )
        assert len(second["vpcPeerings"]) == 1
        assert len(second["staticRoutes"]) == 1
        assert len(second["policyRoutes"]) == 1

    def test_multiple_remote_subnets_each_get_a_route_and_policy(self) -> None:
        patch = _peering_side_patch(
            {}, remote_vpc="t2-vpc", local_connect_ip="169.254.101.1",
            link_cidr="169.254.101.0/30", remote_cidrs=[T2, "10.198.208.0/20"],
            remote_connect_ip="169.254.101.2",
        )
        assert len(patch["staticRoutes"]) == 2
        assert len(patch["policyRoutes"]) == 2


class TestPeeringLinkAllocation:
    def test_links_do_not_overlap(self) -> None:
        _, _, first = peering_link_addresses(0)
        _, _, second = peering_link_addresses(1)
        assert first == "169.254.101.0/30"
        assert second == "169.254.101.4/30"

    def test_addresses_are_link_local(self) -> None:
        # These exist only between two VPC routers and are never announced.
        local, remote, _ = peering_link_addresses(0)
        assert local.startswith("169.254.")
        assert remote.startswith("169.254.")
        assert local != remote


class TestCreatePeeringValidation:
    @pytest.mark.asyncio
    async def test_self_peering_rejected(self) -> None:
        request = MagicMock()
        request.app.state.k8s_client = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await vpcs.create_vpc_peering(
                request=request, name="t1-vpc",
                data=VpcPeeringCreateRequest(remote_vpc="t1-vpc"),
                user=MagicMock(),
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_subnetless_vpc_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Peering a VPC with no subnets would install routes to nothing.
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value={"spec": {}})
        request = MagicMock()
        request.app.state.k8s_client = k8s
        monkeypatch.setattr(vpcs, "_vpc_subnet_cidrs", AsyncMock(return_value=[]))

        with pytest.raises(HTTPException) as exc:
            await vpcs.create_vpc_peering(
                request=request, name="t1-vpc",
                data=VpcPeeringCreateRequest(remote_vpc="t2-vpc"),
                user=MagicMock(),
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_vpc_is_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"),
        )
        request = MagicMock()
        request.app.state.k8s_client = k8s

        with pytest.raises(HTTPException) as exc:
            await vpcs.create_vpc_peering(
                request=request, name="t1-vpc",
                data=VpcPeeringCreateRequest(remote_vpc="t2-vpc"),
                user=MagicMock(),
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestRemovePeeringSide:
    async def test_strips_link_routes_and_policies(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = _peering_side_patch(
            {}, remote_vpc="t2-vpc", local_connect_ip="169.254.101.1",
            link_cidr="169.254.101.0/30", remote_cidrs=[T2],
            remote_connect_ip="169.254.101.2",
        )
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            return_value={"spec": spec},
        )
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()
        monkeypatch.setattr(vpcs, "_vpc_subnet_cidrs", AsyncMock(return_value=[T2]))

        await _remove_peering_side(k8s, "t1-vpc", "t2-vpc")

        patched = k8s.custom_api.patch_cluster_custom_object.await_args.kwargs["body"]
        assert patched["spec"]["vpcPeerings"] == []
        assert patched["spec"]["staticRoutes"] == []
        assert patched["spec"]["policyRoutes"] == []

    async def test_keeps_routes_belonging_to_other_peers(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = {
            "vpcPeerings": [
                {"remoteVpc": "t2-vpc", "localConnectIP": "169.254.101.1/30"},
                {"remoteVpc": "t3-vpc", "localConnectIP": "169.254.101.5/30"},
            ],
            "staticRoutes": [
                {"cidr": T2, "nextHopIP": "169.254.101.2"},
                {"cidr": "10.198.208.0/20", "nextHopIP": "169.254.101.6"},
            ],
            "policyRoutes": [
                {"priority": PEERING_POLICY_PRIORITY, "action": "allow", "match": f"ip4.dst == {T2}"},
                {"priority": PEERING_POLICY_PRIORITY, "action": "allow", "match": "ip4.dst == 10.198.208.0/20"},
            ],
        }
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value={"spec": spec})
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()
        monkeypatch.setattr(vpcs, "_vpc_subnet_cidrs", AsyncMock(return_value=[T2]))

        await _remove_peering_side(k8s, "t1-vpc", "t2-vpc")

        patched = k8s.custom_api.patch_cluster_custom_object.await_args.kwargs["body"]["spec"]
        assert [p["remoteVpc"] for p in patched["vpcPeerings"]] == ["t3-vpc"]
        assert [r["cidr"] for r in patched["staticRoutes"]] == ["10.198.208.0/20"]
        assert len(patched["policyRoutes"]) == 1

    async def test_missing_vpc_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"),
        )
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        await _remove_peering_side(k8s, "gone", "t2-vpc")

        k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()
