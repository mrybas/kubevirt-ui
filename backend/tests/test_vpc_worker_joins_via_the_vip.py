"""A VPC tenant's worker must be told to join at the transit VIP.

`Cluster.spec.controlPlaneEndpoint` is not just what humans dial — CAPI copies
it into the worker's `discovery.bootstrapToken.apiServerEndpoint`. Setting it
to the external ingress name assumed the worker would pick the VIP out of
cluster-info, but kubeadm has to reach the endpoint *to fetch* cluster-info.
The worker therefore dialled a name its isolated VPC could neither resolve
(public DNS is unreachable there) nor route to:

    error execution phase preflight: couldn't validate the identity of the
    API Server: Get "https://t1.<ingress>/.../cluster-info":
    lookup t1.<ingress> on 8.8.8.8:53: i/o timeout

Three lab runs read that as "CAPK will not bootstrap". Nothing was wrong with
the transit datapath — the worker was never on it.

Outside access is unaffected: `GET /tenants/{name}/kubeconfig` rewrites
`server` to the ingress host, so kubectl never reads this field.
"""

import time

import pytest

from app.api.v1 import tenants_common
from app.api.v1.tenants_capi import _build_cluster_cr, _endpoint_host
from app.models.tenant import TenantCreateRequest


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
        name="t1",
        display_name="Tenant One",
        folder="poc-transit",
        environment="dev",
        kubernetes_version="v1.32.1",
        worker_count=1,
    )
    base.update(kw)
    return TenantCreateRequest(**base)


def test_a_vpc_tenant_points_the_endpoint_at_the_vip(monkeypatch) -> None:
    monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")

    cr = _build_cluster_cr(_req(), api_port=20000)

    assert cr["spec"]["controlPlaneEndpoint"] == {
        "host": "10.199.0.100", "port": 20000,
    }, "the worker's join endpoint must be reachable from inside the VPC"


def test_the_api_port_still_reaches_the_cluster_network(monkeypatch) -> None:
    monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")

    cr = _build_cluster_cr(_req(), api_port=20000)

    assert cr["spec"]["clusterNetwork"]["apiServerPort"] == 20000


def test_without_a_configured_vip_the_ingress_name_is_kept(monkeypatch) -> None:
    """No VIP configured is a deployment we must not silently mis-wire."""
    monkeypatch.delenv("TENANTS_CP_DEMUX_VIP", raising=False)

    cr = _build_cluster_cr(_req(), api_port=20000)

    assert cr["spec"]["controlPlaneEndpoint"] == {
        "host": _endpoint_host("t1"), "port": 443,
    }


def test_an_explicit_host_still_wins(monkeypatch) -> None:
    """`cp_host` is the caller saying it knows better; do not override it."""
    monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")

    cr = _build_cluster_cr(_req(), cp_host="cp.example.test", api_port=20000)

    assert cr["spec"]["controlPlaneEndpoint"] == {
        "host": "cp.example.test", "port": 443,
    }


def test_a_default_overlay_tenant_is_untouched(monkeypatch) -> None:
    """No api_port means the ClusterIP model, which the patch step fixes up."""
    monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")

    cr = _build_cluster_cr(_req())

    assert cr["spec"]["controlPlaneEndpoint"]["port"] == 6443
    assert cr["spec"]["controlPlaneEndpoint"]["host"] == _endpoint_host("t1")
    assert "apiServerPort" not in cr["spec"]["clusterNetwork"]
