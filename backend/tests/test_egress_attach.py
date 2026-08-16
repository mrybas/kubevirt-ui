"""Unit tests for attaching a tenant VPC to an egress gateway.

Two failure modes are pinned here, both seen live:

  * the attach used to create a cluster-scoped `VpcPeering` object. kube-ovn
    has no such resource type, so the call 404'd and no tenant ever reached
    the gateway;
  * the transit-IP allocation was written *before* the peering and routes, so
    once that 404 happened every retry hit the "already attached" short-circuit
    and returned 200 with nothing wired.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import attach_tenant_to_gateway

GW = "shared-egress"
GW_VPC = "egw-shared-egress"
TENANT_VPC = "t1"
TENANT_SUBNET = "t1-default"
TENANT_CIDR = "10.100.0.0/24"
TRANSIT_CIDR = "10.255.0.0/24"


def _k8s(alloc: dict[str, str] | None = None) -> MagicMock:
    """A k8s client whose VPCs start with no peerings and no routes."""
    vpcs = {
        GW_VPC: {
            "metadata": {
                "name": GW_VPC,
                "annotations": {
                    "kubevirt-ui.io/transit-cidr": TRANSIT_CIDR,
                    "kubevirt-ui.io/gw-vpc-cidr": "10.199.0.0/24",
                },
            },
            "spec": {},
        },
        TENANT_VPC: {"metadata": {"name": TENANT_VPC}, "spec": {}},
    }
    subnets = {
        TENANT_SUBNET: {
            "metadata": {"name": TENANT_SUBNET},
            "spec": {"cidrBlock": TENANT_CIDR, "vpc": TENANT_VPC},
        },
    }
    veg = {
        "metadata": {"name": GW},
        "spec": {"policies": [{"snat": True, "subnets": ["egw-shared-egress-subnet"]}]},
        "status": {"internalIPs": ["10.199.0.3"]},
    }

    k8s = MagicMock()
    k8s.patches = []

    async def get_obj(**kw):
        plural, name = kw["plural"], kw["name"]
        if plural == "vpcs":
            obj = vpcs[name]
        elif plural == "subnets":
            obj = subnets[name]
        else:
            raise AssertionError(f"unexpected get of {plural}/{name}")
        # The API server always stamps one, and the compare-and-set writes in
        # `core.cas` echo it back in the patch body.
        obj["metadata"].setdefault("resourceVersion", "1")
        return obj

    async def patch_obj(**kw):
        name, body, plural = kw["name"], kw["body"], kw["plural"]
        target = subnets if plural == "subnets" else vpcs
        target[name].setdefault("spec", {}).update(body["spec"])
        k8s.patches.append((name, body))
        return target[name]

    async def get_ns_obj(**kw):
        return veg

    async def patch_ns_obj(**kw):
        veg["spec"].update(kw["body"]["spec"])
        return veg

    async def list_obj(**kw):
        return {"items": list(vpcs.values())}

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get_ns_obj)
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock(side_effect=patch_ns_obj)
    k8s.custom_api.create_cluster_custom_object = AsyncMock(
        side_effect=AssertionError("no cluster-scoped peering object may be created"),
    )

    cm = MagicMock()
    cm.data = dict(alloc or {})
    cm.metadata.resource_version = "1"
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.replace_namespaced_config_map = AsyncMock()
    k8s.core_api.create_namespaced_config_map = AsyncMock()

    k8s.vpcs = vpcs
    k8s.subnets = subnets
    k8s.veg = veg
    return k8s


async def _attach(k8s):
    return await attach_tenant_to_gateway(
        k8s, GW, TENANT_VPC, TENANT_SUBNET, TENANT_CIDR,
    )


class TestPeeringShape:
    @pytest.mark.asyncio
    async def test_peering_is_declared_on_both_vpcs(self) -> None:
        k8s = _k8s()
        await _attach(k8s)

        gw_side = k8s.vpcs[GW_VPC]["spec"]["vpcPeerings"]
        tenant_side = k8s.vpcs[TENANT_VPC]["spec"]["vpcPeerings"]

        assert [p["remoteVpc"] for p in gw_side] == [TENANT_VPC]
        assert [p["remoteVpc"] for p in tenant_side] == [GW_VPC]

    @pytest.mark.asyncio
    async def test_each_side_carries_its_own_transit_address(self) -> None:
        k8s = _k8s()
        result = await _attach(k8s)

        gw_ip = k8s.vpcs[GW_VPC]["spec"]["vpcPeerings"][0]["localConnectIP"]
        tenant_ip = k8s.vpcs[TENANT_VPC]["spec"]["vpcPeerings"][0]["localConnectIP"]

        # Gateway always takes .1; the tenant gets an allocation out of the same /24.
        assert gw_ip == "10.255.0.1/24"
        assert tenant_ip == f"{result.transit_ip}/24"
        assert gw_ip != tenant_ip

    @pytest.mark.asyncio
    async def test_prefix_length_follows_the_transit_cidr(self) -> None:
        k8s = _k8s()
        k8s.vpcs[GW_VPC]["metadata"]["annotations"]["kubevirt-ui.io/transit-cidr"] = "10.255.0.0/29"
        await _attach(k8s)

        assert k8s.vpcs[GW_VPC]["spec"]["vpcPeerings"][0]["localConnectIP"].endswith("/29")

    @pytest.mark.asyncio
    async def test_default_route_points_at_the_gateway_over_the_link(self) -> None:
        k8s = _k8s()
        await _attach(k8s)

        tenant_routes = k8s.vpcs[TENANT_VPC]["spec"]["staticRoutes"]
        assert {"cidr": "0.0.0.0/0", "nextHopIP": "10.255.0.1", "policy": "policyDst"} in tenant_routes

    @pytest.mark.asyncio
    async def test_return_route_points_back_at_the_tenant(self) -> None:
        k8s = _k8s()
        result = await _attach(k8s)

        gw_routes = k8s.vpcs[GW_VPC]["spec"]["staticRoutes"]
        assert any(
            r["cidr"] == TENANT_CIDR and r["nextHopIP"] == result.transit_ip
            for r in gw_routes
        )


class TestRetryAfterPartialAttach:
    @pytest.mark.asyncio
    async def test_recorded_allocation_alone_does_not_count_as_attached(self) -> None:
        # Exactly the state a failed attach used to leave behind: the transit IP
        # is booked, but no peering and no routes exist.
        k8s = _k8s({f"vpc.{TENANT_VPC}": f"10.255.0.2,{TENANT_SUBNET},{TENANT_CIDR}"})
        await _attach(k8s)

        assert k8s.vpcs[TENANT_VPC]["spec"]["vpcPeerings"][0]["remoteVpc"] == GW_VPC
        assert k8s.vpcs[GW_VPC]["spec"]["vpcPeerings"][0]["remoteVpc"] == TENANT_VPC

    @pytest.mark.asyncio
    async def test_retry_reuses_the_booked_transit_ip(self) -> None:
        k8s = _k8s({f"vpc.{TENANT_VPC}": f"10.255.0.7,{TENANT_SUBNET},{TENANT_CIDR}"})
        result = await _attach(k8s)

        assert result.transit_ip == "10.255.0.7"
        assert k8s.vpcs[TENANT_VPC]["spec"]["vpcPeerings"][0]["localConnectIP"] == "10.255.0.7/24"
        # A resumed attach must not re-book the address.
        k8s.core_api.replace_namespaced_config_map.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allocation_is_recorded_only_after_the_wiring(self) -> None:
        k8s = _k8s()
        await _attach(k8s)

        k8s.core_api.replace_namespaced_config_map.assert_awaited_once()
        # Both VPCs were already patched by the time the booking was written.
        assert len(k8s.patches) >= 2

    @pytest.mark.asyncio
    async def test_attaching_twice_does_not_duplicate_anything(self) -> None:
        k8s = _k8s()
        first = await _attach(k8s)
        k8s.core_api.read_namespaced_config_map.return_value.data = {
            f"vpc.{TENANT_VPC}": f"{first.transit_ip},{TENANT_SUBNET},{TENANT_CIDR}",
        }
        await _attach(k8s)

        assert len(k8s.vpcs[TENANT_VPC]["spec"]["vpcPeerings"]) == 1
        assert len(k8s.vpcs[GW_VPC]["spec"]["vpcPeerings"]) == 1
        assert len(k8s.vpcs[TENANT_VPC]["spec"]["staticRoutes"]) == 1


class TestGatewayPolicies:
    @pytest.mark.asyncio
    async def test_tenant_cidr_is_added_to_the_gateway_policies(self) -> None:
        k8s = _k8s()
        await _attach(k8s)

        blocks = [b for p in k8s.veg["spec"]["policies"] for b in p.get("ipBlocks", [])]
        assert TENANT_CIDR in blocks

    @pytest.mark.asyncio
    async def test_existing_subnet_policy_is_left_alone(self) -> None:
        k8s = _k8s()
        await _attach(k8s)

        assert any(
            p.get("subnets") == ["egw-shared-egress-subnet"]
            for p in k8s.veg["spec"]["policies"]
        )


class TestGatewayDefaultRoute:
    """OVN routes (stage 15) before it applies policies (stage 17), so a packet
    with no matching route on the gateway VPC dies before the gateway's own
    reroute policy at 29100 is reached."""

    @pytest.mark.asyncio
    async def test_gateway_vpc_gets_a_default_route_to_the_gateway_pod(self) -> None:
        k8s = _k8s()
        await _attach(k8s)

        routes = k8s.vpcs[GW_VPC]["spec"]["staticRoutes"]
        assert {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.0.3", "policy": "policyDst"} in routes

    @pytest.mark.asyncio
    async def test_prefix_is_stripped_from_the_reported_internal_ip(self) -> None:
        k8s = _k8s()
        k8s.veg["status"]["internalIPs"] = ["10.199.0.3/24"]
        await _attach(k8s)

        routes = k8s.vpcs[GW_VPC]["spec"]["staticRoutes"]
        assert any(r["cidr"] == "0.0.0.0/0" and r["nextHopIP"] == "10.199.0.3" for r in routes)

    @pytest.mark.asyncio
    async def test_attach_still_completes_when_the_gateway_has_no_ip_yet(self) -> None:
        # A VpcEgressGateway that has not reconciled yet must not fail the
        # attach — the peering and tenant routes are still worth writing.
        k8s = _k8s()
        k8s.veg["status"] = {}
        result = await _attach(k8s)

        assert result.vpc_name == TENANT_VPC
        assert not any(
            r["cidr"] == "0.0.0.0/0"
            for r in k8s.vpcs[GW_VPC]["spec"].get("staticRoutes", [])
        )
