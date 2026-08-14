"""A worker's path to its own apiserver must not depend on the VPC default route.

Measured on the lab cluster with ovn-trace: a worker in `acme-net` reaching
`apiServerEndpoint: 10.99.165.180:6443` left the VPC on its **default route**,
was SNAT'd to the VPC's EIP and landed on `ext-sub`, where the host cluster's
OVN load balancer happens to be attached —

    15. lr_in_ip_routing: ip4.dst == 0.0.0.0/0, priority 4
        ct_snat(ip4.src=10.198.190.200) → output("localnet.ext-sub")

— and tcpdump on the lab router saw nothing, because the DNAT happened on the
way out. So the node's lifeline was a property of that one route: attaching an
egress gateway added a second `0.0.0.0/0` and the tenant then sat with zero
registered nodes for eight minutes.

Publishing the same VIP on the VPC's own load balancer moves the DNAT inside
the VPC — verified by hand with `ovn-nbctl lb-add`: the trace then ends at the
control-plane pod itself, over the ovn-cluster peering.

The supported route, a SwitchLBRule, is **not** enough on its own. kube-ovn
accepts the rule, creates its `slr-<name>` Service and resolves the endpoints,
then refuses to program the load balancer:

    service.go:568 Service tenant-tstor4/slr-tstor4-cp
                   external IP belongs to subnet: false

— the VIP must belong to a subnet kube-ovn manages, and a host-cluster
ClusterIP does not. So these helpers exist and are correct, but nothing calls
them until the control-plane endpoint moves to an address inside the VPC
subnet (which also means adding it to the apiserver SANs). What is wired up is
the *removal*, so any rule left behind by 2026.10.23 gets cleaned up with its
tenant.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException


def _svc(cluster_ip="10.99.165.180"):
    svc = MagicMock()
    svc.spec.cluster_ip = cluster_ip
    api, konn = MagicMock(), MagicMock()
    api.name, api.port, api.target_port, api.protocol = "api", 6443, 6443, "TCP"
    konn.name, konn.port, konn.target_port, konn.protocol = "konnectivity", 8132, 8132, "TCP"
    svc.spec.ports = [api, konn]
    return svc


def _k8s(svc=None, create_error=None):
    k8s = MagicMock()
    if svc is None:
        k8s.core_api.read_namespaced_service = AsyncMock(side_effect=ApiException(status=404))
    else:
        k8s.core_api.read_namespaced_service = AsyncMock(return_value=svc)
    k8s.custom_api.create_cluster_custom_object = AsyncMock(side_effect=create_error)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock()
    k8s.custom_api.delete_cluster_custom_object = AsyncMock()
    return k8s


def _body(k8s):
    return k8s.custom_api.create_cluster_custom_object.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_the_vip_is_published_on_the_vpcs_own_load_balancer():
    from app.api.v1.tenants_capi import _ensure_cp_reachable_in_vpc

    k8s = _k8s(_svc())

    assert await _ensure_cp_reachable_in_vpc(k8s, "tstor4", "acme-net") is True

    body = _body(k8s)
    assert body["kind"] == "SwitchLBRule"
    assert body["spec"]["vip"] == "10.99.165.180", \
        "the endpoint the workers already hold — only the DNAT location changes"
    assert body["spec"]["namespace"] == "tenant-tstor4"
    assert body["spec"]["selector"] == ["kamaji.clastix.io/name:tstor4"]
    assert {p["port"] for p in body["spec"]["ports"]} == {6443, 8132}


@pytest.mark.asyncio
async def test_it_waits_rather_than_failing_when_kamaji_has_not_made_the_service():
    """Create runs before Kamaji's Service exists; the read path retries."""
    from app.api.v1.tenants_capi import _ensure_cp_reachable_in_vpc

    k8s = _k8s(None)

    assert await _ensure_cp_reachable_in_vpc(k8s, "tstor4", "acme-net") is False
    k8s.custom_api.create_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_call_updates_instead_of_erroring():
    from app.api.v1.tenants_capi import _ensure_cp_reachable_in_vpc

    k8s = _k8s(_svc(), create_error=ApiException(status=409))

    assert await _ensure_cp_reachable_in_vpc(k8s, "tstor4", "acme-net") is True
    assert k8s.custom_api.patch_cluster_custom_object.await_args.kwargs["name"] == "tstor4-cp"


@pytest.mark.asyncio
async def test_a_headless_service_is_not_published():
    from app.api.v1.tenants_capi import _ensure_cp_reachable_in_vpc

    k8s = _k8s(_svc(cluster_ip="None"))

    assert await _ensure_cp_reachable_in_vpc(k8s, "tstor4", "acme-net") is False


@pytest.mark.asyncio
async def test_the_rule_goes_with_the_tenant():
    """It is cluster-scoped, so the namespace cascade cannot reach it — the
    exact shape of leftover this codebase has been bitten by repeatedly."""
    from app.api.v1.tenants_capi import _remove_cp_vpc_publication

    k8s = _k8s(_svc())

    await _remove_cp_vpc_publication(k8s, "tstor4")

    call = k8s.custom_api.delete_cluster_custom_object.await_args.kwargs
    assert call["plural"] == "switch-lb-rules"
    assert call["name"] == "tstor4-cp"


@pytest.mark.asyncio
async def test_deleting_one_that_is_already_gone_is_not_an_error():
    from app.api.v1.tenants_capi import _remove_cp_vpc_publication

    k8s = _k8s(_svc())
    k8s.custom_api.delete_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=404),
    )

    await _remove_cp_vpc_publication(k8s, "tstor4")  # must not raise


@pytest.mark.asyncio
async def test_the_vpc_is_read_from_where_it_is_actually_recorded():
    """The Cluster CR carries folder/environment/tenant labels but no VPC; the
    binding lives on the machine template, which is also what places the pod."""
    from app.api.v1.tenants_capi import tenant_vpc_name

    k8s = MagicMock()
    k8s.custom_api.list_namespaced_custom_object = AsyncMock(return_value={"items": [{
        "spec": {"template": {"spec": {"virtualMachineTemplate": {"spec": {"template": {
            "metadata": {"annotations": {
                "ovn.kubernetes.io/logical_switch": "acme-net-default",
            }},
        }}}}}},
    }]})

    assert await tenant_vpc_name(k8s, "tstor4") == "acme-net"


@pytest.mark.asyncio
async def test_a_tenant_on_the_cluster_overlay_reports_no_vpc():
    from app.api.v1.tenants_capi import tenant_vpc_name

    k8s = MagicMock()
    k8s.custom_api.list_namespaced_custom_object = AsyncMock(return_value={"items": [{
        "spec": {"template": {"spec": {"virtualMachineTemplate": {"spec": {"template": {
            "metadata": {"annotations": {}},
        }}}}}},
    }]})

    assert await tenant_vpc_name(k8s, "tci") == ""


def test_nothing_creates_it_until_the_vip_can_live_in_the_vpc() -> None:
    """Guards against re-wiring this before the endpoint moves: kube-ovn would
    accept the rule and quietly not program it, which reads as working."""
    from pathlib import Path

    src = Path("app/api/v1/tenants_crud.py").read_text()

    assert "_ensure_cp_reachable_in_vpc" not in src


def test_delete_tenant_cleans_it_up() -> None:
    from pathlib import Path

    src = Path("app/api/v1/tenants_crud.py").read_text()
    body = src[src.index("async def delete_tenant("):src.index("\n@router.", src.index("async def delete_tenant("))]

    assert "_remove_cp_vpc_publication" in body
