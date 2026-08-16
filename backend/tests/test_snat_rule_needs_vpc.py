"""Every OvnSnatRule we create must carry `spec.vpc`.

kube-ovn 1.16.2 resolves the router for a SNAT rule from `spec.vpc`. A rule
written with only `ovnEip` + `vpcSubnet` is accepted by the API server and then
sits unready forever with

    failed to get vpc for snat

which reads like a transient controller hiccup and is not — the rule simply
never programs, so the tenant's egress is silently dropped. Measured on the lab
in run #2 (backlog U5).

There are three creation sites and all three regressed together, so all three
are pinned here.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

VPC = "team-a"
SUBNET = "team-a-default"


def _k8s() -> MagicMock:
    k8s = MagicMock()
    k8s.custom_api.create_cluster_custom_object = AsyncMock(return_value={})
    k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value={})
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(return_value={})
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
    return k8s


def _snat_bodies(k8s: MagicMock) -> list[dict]:
    """Every OvnSnatRule body handed to the API server."""
    return [
        c.kwargs["body"]
        for c in k8s.custom_api.create_cluster_custom_object.call_args_list
        if c.kwargs.get("body", {}).get("kind") == "OvnSnatRule"
    ]


@pytest.mark.asyncio
async def test_snat_rule_endpoint_sets_vpc(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import ovn_gateway as mod

    k8s = _k8s()
    request = MagicMock()
    request.app.state.k8s_client = k8s

    monkeypatch.setattr(
        mod, "_get_gateway_tracking",
        AsyncMock(return_value=({"eip": f"eip-{VPC}"}, "kube-system")),
    )

    await mod.create_snat_rule(
        request=request,
        vpc_name=VPC,
        data=mod.OvnSnatRuleCreateRequest(ovn_eip=f"eip-{VPC}", vpc_subnet=SUBNET),
    )

    bodies = _snat_bodies(k8s)
    assert bodies, "no OvnSnatRule was created"
    assert bodies[0]["spec"]["vpc"] == VPC
    assert bodies[0]["spec"]["vpcSubnet"] == SUBNET


@pytest.mark.asyncio
async def test_enabling_ovn_nat_on_a_vpc_sets_vpc() -> None:
    """The `POST /vpcs/{name}/ovn-gateway` path builds its own manifest."""
    from app.api.v1 import ovn_gateway as mod

    src = __import__("inspect").getsource(mod.create_ovn_gateway)
    assert '"vpc": vpc_name' in src, "create_ovn_gateway builds a SNAT rule without spec.vpc"


def test_vpc_create_with_nat_sets_vpc() -> None:
    """The VPC-create flow (`nat_gateway: true`) builds a third manifest."""
    from app.api.v1 import vpcs as mod

    src = __import__("inspect").getsource(mod)
    snat = src[src.index('"kind": "OvnSnatRule"'):]
    spec = snat[snat.index('"spec"'):snat.index("}", snat.index('"spec"')) + 1]
    assert '"vpc"' in spec, f"VPC-create SNAT manifest lacks spec.vpc: {spec}"
