"""A VPC tenant's workers must be given an endpoint their VPC can reach.

Measured on the cluster from a pod on `acme-net`:

    https://10.108.25.46:20000/version   (Kamaji Service ClusterIP) → 200, 56ms
    https://10.198.175.201:20000/version (MetalLB CP VIP)           → connects, 0 bytes
    http://10.198.175.200/               (Traefik LB)               → connects, 0 bytes

The node's own reply leaves through its default gateway —
`ip route get 10.100.0.9` → `via 10.198.175.254 dev eth0` — and nothing routes
the VPC prefix back, so anything served from host network is a black hole from
inside a VPC. The service network is reachable because OVN load-balances it.

The failure was silent: the tenant reported Ready while `kubeadm join` spent
five minutes on

    couldn't validate the identity of the API Server: failed to request the
    cluster-info ConfigMap: Get "https://tci.tenants.lab.beardlabs.cc:443/...

and the Machine stayed Provisioned with NodeHealthy=False.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.tenants_capi import _assert_vpc_reaches_services, _build_cluster_cr


def _k8s(peerings):
    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(
        return_value={"spec": {"vpcPeerings": peerings}},
    )
    return k8s


def _req(**kw):
    req = MagicMock()
    req.name = "tci"
    req.display_name = "tci"
    req.pod_cidr = "10.244.0.0/16"
    req.service_cidr = "10.112.0.0/12"
    req.folder = "acme"
    req.environment = "dev"
    req.worker_type = "vm"
    req.enable_oidc = False
    for k, v in kw.items():
        setattr(req, k, v)
    return req


@pytest.mark.asyncio
class TestPeeringIsRequired:
    async def test_an_unpeered_vpc_is_refused_before_anything_is_created(self) -> None:
        with pytest.raises(HTTPException) as e:
            await _assert_vpc_reaches_services(_k8s([]), "beta-net")
        assert e.value.status_code == 422
        assert "not peered" in e.value.detail
        assert "host cluster access" in e.value.detail

    async def test_a_peered_vpc_passes(self) -> None:
        await _assert_vpc_reaches_services(
            _k8s([{"remoteVpc": "ovn-cluster", "localConnectIP": "169.254.101.25/30"}]),
            "acme-net",
        )

    async def test_a_peering_with_someone_else_is_not_enough(self) -> None:
        with pytest.raises(HTTPException):
            await _assert_vpc_reaches_services(
                _k8s([{"remoteVpc": "beta-net"}]), "acme-net",
            )

    async def test_a_missing_vpc_says_so(self) -> None:
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=404),
        )
        with pytest.raises(HTTPException) as e:
            await _assert_vpc_reaches_services(k8s, "ghost")
        assert e.value.status_code == 422
        assert "not found" in e.value.detail


class TestTheEndpointIsNotAHostAddress:
    """The Cluster CR must not be built around a per-tenant VIP any more."""

    def test_no_api_server_port_pins_the_endpoint_to_a_vip(self) -> None:
        # `_build_cluster_cr` reads cluster config for the endpoint host, so
        # drive it the way the creation path does: with no api_port, which is
        # what the VPC branch now passes.
        cr = _build_cluster_cr(_req(vpc_name="acme-net"), cp_host="placeholder")
        endpoint = cr["spec"]["controlPlaneEndpoint"]
        assert endpoint["port"] == 6443, "the ClusterIP model uses 6443"
        assert "apiServerPort" not in cr["spec"]["clusterNetwork"]

    def test_the_creation_path_patches_the_endpoint_to_the_cluster_ip(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_capi.py").read_text()
        assert "_wait_for_tcp_service_ip" in src
        assert 'controlPlaneEndpoint": {"host": cluster_ip' in src
        # …for every tenant, not only the default-overlay ones.
        assert "advertiseAddress model: cluster-info is already correct" not in src
