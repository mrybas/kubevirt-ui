"""A VPC tenant's workers reach their control plane on the shared demux VIP.

2026.10.24 (`eef01c0`) put VPC tenants on the same path the default overlay
uses — `controlPlaneEndpoint` patched to the Kamaji Service ClusterIP — and
refused any VPC that was not peered with `ovn-cluster`. That peering is what
made the ClusterIP reachable, and it is also what broke tenant isolation: from
an "isolated" VPC the host apiserver, the host pod network and every other
tenant's control plane became reachable (finding B9.11).

This restores the model the platform was built on: Kamaji advertises the shared
MetalLB VIP with per-tenant ports, so `cluster-info` and the join endpoint carry
an address the VPC can reach on its own, and `controlPlaneEndpoint` stays the
Traefik SNI host for admin access from outside.

Measured on the lab 2026-08-15: a pod inside an isolated VPC with no peering at
all reaches a MetalLB address published in the underlay VLAN —
`curl http=200` in 11.7 ms, while `tcpdump` on the site router saw 0 packets.
"""

import time
from pathlib import Path

import pytest

from app.api.v1 import tenants_common
from app.api.v1.tenants_capi import (
    _build_cluster_cr,
    _build_cp_lb_service,
    _build_kamaji_cp_cr,
)
from app.models.tenant import TenantCreateRequest


SRC = Path("app/api/v1/tenants_capi.py").read_text()


@pytest.fixture(autouse=True)
def cluster_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CR builders read the discovered ingress domain from this cache."""
    monkeypatch.setattr(tenants_common, "_cluster_config", {
        "ingress_ip": "10.0.0.1",
        "ingress_domain": "lab.example",
        "ingress_class": "traefik",
        "ingress_controller": "traefik",
        "mgmt_cidr": "10.0.0.0/24",
        "vpcdns_forward_dns": "10.96.0.10",
        "vpcdns_vip": "10.96.0.200",
        "host_service_cidr": "10.96.0.0/12",
        "fetched_at": time.time(),
    })


def _req(**kw) -> TenantCreateRequest:
    base = dict(
        name="tvpc",
        display_name="tvpc",
        folder="acme",
        environment="prod",
        kubernetes_version="v1.31.0",
        worker_count=1,
    )
    base.update(kw)
    return TenantCreateRequest(**base)


class TestKamajiAdvertisesTheVip:
    def test_the_vip_reaches_advertise_address(self) -> None:
        cr = _build_kamaji_cp_cr(_req(vpc_name="acme-net"), advertise_vip="10.198.175.201", konn_port=8140)

        assert cr["spec"]["network"]["advertiseAddress"] == "10.198.175.201", (
            "this address is what Kamaji bakes into cluster-info and admin.conf; "
            "a ClusterIP there is only reachable through a peering with ovn-cluster"
        )

    def test_a_default_overlay_tenant_advertises_nothing(self) -> None:
        cr = _build_kamaji_cp_cr(_req())

        assert "advertiseAddress" not in (cr["spec"].get("network") or {})


class TestTheClusterEndpointIsTheWorkersJoinAddress:
    """This class used to assert the opposite, and the lab proved it wrong.

    It read `controlPlaneEndpoint` as "the address a human dials" and kept the
    Traefik SNI hostname there, on the assumption that a worker would learn the
    VIP from cluster-info. CAPI copies this field into the worker's
    `discovery.bootstrapToken.apiServerEndpoint`, and kubeadm must reach that
    endpoint *to fetch* cluster-info — so the worker dialled a name its
    isolated VPC could neither resolve nor route to, and three runs read the
    result as "CAPK will not bootstrap".

    Admin access did not depend on this field even then:
    `GET /tenants/{name}/kubeconfig` rewrites `server` to the ingress host.
    """

    def test_a_vpc_tenant_joins_at_the_vip(self, monkeypatch) -> None:
        monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.198.175.201")

        cr = _build_cluster_cr(_req(vpc_name="acme-net"), api_port=20001)

        assert cr["spec"]["controlPlaneEndpoint"] == {
            "host": "10.198.175.201", "port": 20001,
        }
        assert cr["spec"]["clusterNetwork"]["apiServerPort"] == 20001

    def test_no_vip_configured_leaves_the_ingress_host(self, monkeypatch) -> None:
        """A deployment without the VIP must not be silently mis-wired."""
        monkeypatch.delenv("TENANTS_CP_DEMUX_VIP", raising=False)

        cr = _build_cluster_cr(_req(vpc_name="acme-net"), api_port=20001)

        host = cr["spec"]["controlPlaneEndpoint"]["host"]
        assert not host[0].isdigit(), f"expected the SNI hostname, got {host!r}"
        assert cr["spec"]["controlPlaneEndpoint"]["port"] == 443

    def test_a_default_overlay_tenant_has_no_apiserver_port(self) -> None:
        cr = _build_cluster_cr(_req())

        assert "apiServerPort" not in cr["spec"]["clusterNetwork"]


class TestTheSharedVipService:
    def test_every_tenant_shares_one_address_and_differs_by_port(self) -> None:
        svc = _build_cp_lb_service(_req(vpc_name="acme-net"), "10.198.175.201", 20001, 8140)

        ann = svc["metadata"]["annotations"]
        assert ann["metallb.universe.tf/loadBalancerIPs"] == "10.198.175.201"
        assert "metallb.universe.tf/allow-shared-ip" in ann, (
            "without the shared-ip key MetalLB refuses the second tenant on this VIP"
        )
        assert svc["spec"]["selector"] == {"kamaji.clastix.io/name": "tvpc"}
        assert {p["port"] for p in svc["spec"]["ports"]} == {20001, 8140}

    def test_cilium_is_told_to_keep_its_hands_off(self) -> None:
        """Cilium's BPF LB rewrites the VIP before kube-ovn routes it, so an
        in-VPC client never reaches the backend."""
        svc = _build_cp_lb_service(_req(vpc_name="acme-net"), "10.198.175.201", 20001, 8140)

        assert svc["metadata"]["annotations"]["service.cilium.io/type"] == "ClusterIP"


class TestNoPeeringIsRequired:
    def test_creation_does_not_demand_a_peering_with_the_cluster_vpc(self) -> None:
        """The guard added in 2026.10.24 refused any VPC without a peering to
        `ovn-cluster` — which is the isolation hole itself."""
        assert "_assert_vpc_reaches_services" not in SRC

    def test_a_vpc_tenant_endpoint_is_not_patched_to_a_cluster_ip(self) -> None:
        body = SRC[SRC.index("    if vpc:\n        # 3-VPC."):SRC.index("    else:\n        # 3-default.")]

        assert "_wait_for_tcp_service_ip" not in body
        assert "controlPlaneEndpoint" not in body
