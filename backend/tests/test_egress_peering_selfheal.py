"""A gateway whose tenant lost its peering leg must say so, and repair it.

kube-ovn builds a VPC peering only when *both* routers declare it. The attach
flow writes both sides, but `Vpc.spec.vpcPeerings` is a plain list on a shared
object: anything that rewrites it — another flow, a GitOps reconcile, an
operator running `kubectl patch --type merge` with a one-element array — takes
the other side with it.

That is exactly what happened on the lab in run #2. The tenant kept a default
route to a next hop it no longer had an interface for, so all of its egress was
dropped, while the console showed `Ready`, two BGP sessions Established, ECMP
programmed on the border router and the VPC listed as attached. Nothing in the
product disagreed with a completely severed data path.

So the read path checks the invariant rather than trusting the attach:
every attached VPC must carry a peering back to the gateway VPC.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

GW = "team-a"
GW_VPC = "egw-team-a"
TENANT = "team-a-tenant"
TRANSIT = "10.255.0.0/24"


def _k8s(tenant_peerings: list[dict]) -> MagicMock:
    gw_vpc = {
        "metadata": {
            "name": GW_VPC,
            "labels": {"kubevirt-ui.io/egress-gateway": GW},
            "annotations": {
                "kubevirt-ui.io/transit-cidr": TRANSIT,
                "kubevirt-ui.io/gw-vpc-cidr": "10.199.16.0/24",
            },
            "resourceVersion": "1",
        },
        "spec": {"vpcPeerings": [{"remoteVpc": TENANT, "localConnectIP": "10.255.0.1/24"}]},
        "status": {},
    }
    tenant_vpc = {
        "metadata": {"name": TENANT, "resourceVersion": "1"},
        "spec": {"vpcPeerings": list(tenant_peerings)},
        "status": {},
    }
    store = {GW_VPC: gw_vpc, TENANT: tenant_vpc}

    k8s = MagicMock()

    async def get_obj(**kw):
        return store[kw["name"]]

    async def patch_obj(**kw):
        store[kw["name"]].setdefault("spec", {}).update(kw["body"]["spec"])
        return store[kw["name"]]

    async def list_obj(**kw):
        return {"items": [gw_vpc]}

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(
        return_value={"spec": {"replicas": 2}, "status": {"ready": True}},
    )

    cm = MagicMock()
    cm.data = {f"vpc.{TENANT}": f"10.255.0.2,{TENANT}-default,10.200.4.0/22"}
    cm.metadata.resource_version = "1"
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.replace_namespaced_config_map = AsyncMock()
    k8s.core_api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[]))

    k8s._store = store
    return k8s


def _request(k8s: MagicMock) -> MagicMock:
    r = MagicMock()
    r.app.state.k8s_client = k8s
    return r


@pytest.mark.asyncio
async def test_a_healthy_gateway_stays_ready() -> None:
    from app.api.v1.egress_gateway import get_egress_gateway

    k8s = _k8s([{"remoteVpc": GW_VPC, "localConnectIP": "10.255.0.2/24"}])
    out = await get_egress_gateway(request=_request(k8s), name=GW)

    assert out.ready is True
    assert out.degraded_reason is None
    assert out.attached_vpcs[0].peering_ok is True


@pytest.mark.asyncio
async def test_a_missing_tenant_leg_is_reported_not_hidden() -> None:
    from app.api.v1.egress_gateway import get_egress_gateway

    # The tenant only peers with ovn-cluster — the gateway leg was clobbered.
    k8s = _k8s([{"remoteVpc": "ovn-cluster", "localConnectIP": "10.255.1.2/24"}])
    out = await get_egress_gateway(request=_request(k8s), name=GW)

    assert out.ready is False, "a gateway with a severed tenant is not Ready"
    assert out.degraded_reason and TENANT in out.degraded_reason
    assert out.attached_vpcs[0].peering_ok is False


@pytest.mark.asyncio
async def test_the_missing_leg_is_written_back() -> None:
    """Self-heal: reading the gateway repairs what an outside write removed."""
    from app.api.v1.egress_gateway import get_egress_gateway

    k8s = _k8s([{"remoteVpc": "ovn-cluster", "localConnectIP": "10.255.1.2/24"}])
    await get_egress_gateway(request=_request(k8s), name=GW)

    peerings = k8s._store[TENANT]["spec"]["vpcPeerings"]
    remotes = {p["remoteVpc"]: p["localConnectIP"] for p in peerings}
    assert remotes.get(GW_VPC) == "10.255.0.2/24", "the gateway leg was not restored"
    assert "ovn-cluster" in remotes, "self-heal clobbered the unrelated peering"
