"""A VPC's NAT gateway must SNAT to a subnet that can reach the internet.

`_find_infra_subnet` returned `items[0]` of everything labelled
`purpose=infrastructure`. That was right for exactly as long as there was one
such subnet. The control-plane transit subnet carries the same label, the API
returns subnets in name order, and `cp-transit` sorts before `external` — so
every VPC created after the transit architecture landed had its NAT gateway
put on the transit network, whose gateway has no route out.

Nothing reported an error. The EIP is created, the SNAT rule goes Ready, the
OVN gateway looks configured, and packets leave under an address that cannot
come back. On the lab this was read as "the egress gateway is broken" — but
the egress gateway only announces the tenant CIDR over BGP and was never on
this path.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.network import _find_infra_subnet


def _subnet(name: str, gateway: str, vlan: str | None = None) -> dict:
    spec = {"gateway": gateway, "cidrBlock": "10.0.0.0/24"}
    if vlan:
        spec["vlan"] = vlan
    return {"metadata": {"name": name}, "spec": spec}


def _k8s(items: list[dict]) -> MagicMock:
    async def list_obj(**kw):
        return {"items": items}

    async def get_obj(**kw):
        for i in items:
            if i["metadata"]["name"] == kw["name"]:
                return i
        from kubernetes_asyncio.client.exceptions import ApiException
        raise ApiException(status=404)

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    return k8s


# The lab's two infrastructure subnets, in the order the API returns them.
LAB = [
    _subnet("cp-transit", "10.199.0.1", "vlan-cptransit"),
    _subnet("external", "10.199.4.254", "vlan-extnet"),
]


@pytest.mark.asyncio
async def test_the_transit_subnet_is_never_chosen(monkeypatch) -> None:
    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "cp-transit")
    monkeypatch.delenv("VPC_NAT_EXTERNAL_SUBNET", raising=False)

    picked = await _find_infra_subnet(_k8s(LAB))

    assert picked["metadata"]["name"] == "external", (
        "cp-transit sorts first and has no route out; picking it silently "
        "kills egress for every VPC"
    )


@pytest.mark.asyncio
async def test_an_explicit_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "cp-transit")
    monkeypatch.setenv("VPC_NAT_EXTERNAL_SUBNET", "cp-transit")

    picked = await _find_infra_subnet(_k8s(LAB))

    assert picked["metadata"]["name"] == "cp-transit", (
        "the operator knows their topology better than a label does"
    )


@pytest.mark.asyncio
async def test_an_underlay_is_preferred_over_an_overlay(monkeypatch) -> None:
    """A subnet with a VLAN has an upstream; one without does not."""
    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "cp-transit")
    monkeypatch.delenv("VPC_NAT_EXTERNAL_SUBNET", raising=False)
    items = [
        _subnet("aaa-overlay", "10.50.0.1"),
        _subnet("zzz-underlay", "10.199.4.254", "vlan-extnet"),
    ]

    picked = await _find_infra_subnet(_k8s(items))

    assert picked["metadata"]["name"] == "zzz-underlay"


@pytest.mark.asyncio
async def test_only_the_transit_subnet_means_no_answer(monkeypatch) -> None:
    """Better no NAT gateway than one that cannot route — and say so."""
    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "cp-transit")
    monkeypatch.delenv("VPC_NAT_EXTERNAL_SUBNET", raising=False)

    picked = await _find_infra_subnet(_k8s([LAB[0]]))

    assert picked is None


@pytest.mark.asyncio
async def test_a_single_unlabelled_transit_env_keeps_the_old_behaviour(monkeypatch) -> None:
    """Deployments without a transit subnet configured must not regress."""
    monkeypatch.delenv("TENANTS_CP_TRANSIT_SUBNET", raising=False)
    monkeypatch.delenv("VPC_NAT_EXTERNAL_SUBNET", raising=False)

    picked = await _find_infra_subnet(_k8s([_subnet("external", "10.199.4.254", "vlan-extnet")]))

    assert picked["metadata"]["name"] == "external"
