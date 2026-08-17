"""A VPC tenant is built around the address MetalLB gave *it*.

The shared-VIP model kept the address in an env var, so every builder could
reach for it. With one address per tenant that is a trap: the endpoint would
still read the deployment-wide VIP while the tenant's own Service holds a
different one — a perfectly green spec pointing at somebody else's control
plane. So the address flows in as an argument, and the env fallback only
serves the legacy shared model.

Covered here: the endpoint follows the tenant's VIP, the standard ports
replace the allocator, trustd only appears for Talos, and the transit guard
is opened for exactly the ports the tenant actually uses.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1 import tenants_common
from app.api.v1.tenants_capi import _build_cluster_cr
from app.api.v1.tenants_cp_vip import (
    TENANT_API_PORT,
    TENANT_KONN_PORT,
    TENANT_TRUSTD_PORT,
    tenant_cp_ports,
)
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
        name="t9", display_name="T9", folder="poc", environment="dev",
        vpc_name="t9-vpc", worker_os="cloud-init",
    )
    base.update(kw)
    return TenantCreateRequest(**base)


class TestTheEndpointFollowsTheTenantsOwnAddress:
    def test_the_passed_vip_wins_over_the_deployment_wide_one(self, monkeypatch) -> None:
        """The bug this guards: a per-tenant address silently replaced by the
        old shared one, leaving the Cluster CR pointing at another tenant's
        control plane while everything reports green."""
        monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")

        cr = _build_cluster_cr(
            _req(), api_port=TENANT_API_PORT, tenant_vip="10.199.0.107",
        )

        ep = cr["spec"]["controlPlaneEndpoint"]
        assert ep["host"] == "10.199.0.107"
        assert ep["port"] == TENANT_API_PORT

    def test_without_a_tenant_vip_the_legacy_shared_one_is_used(self, monkeypatch) -> None:
        monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")

        cr = _build_cluster_cr(_req(), api_port=20000)

        assert cr["spec"]["controlPlaneEndpoint"]["host"] == "10.199.0.100"

    def test_the_endpoint_port_is_the_standard_one_now(self, monkeypatch) -> None:
        """No allocator: 6443 for everyone, because the address is unique."""
        monkeypatch.delenv("TENANTS_CP_DEMUX_VIP", raising=False)

        cr = _build_cluster_cr(
            _req(), api_port=TENANT_API_PORT, tenant_vip="10.199.0.107",
        )

        assert cr["spec"]["controlPlaneEndpoint"] == {
            "host": "10.199.0.107", "port": 6443,
        }


class TestTrustdOnlyWhereItIsNeeded:
    def test_a_cloud_init_tenant_opens_two_ports(self) -> None:
        assert [p for _, p in tenant_cp_ports("cloud-init")] == [
            TENANT_API_PORT, TENANT_KONN_PORT,
        ]

    def test_a_talos_tenant_opens_three(self) -> None:
        assert [p for _, p in tenant_cp_ports("talos")] == [
            TENANT_API_PORT, TENANT_KONN_PORT, TENANT_TRUSTD_PORT,
        ]


class TestTheLegacyModelRefusesTalos:
    """A shared address can give the fixed :50001 to exactly one tenant.

    Silently allowing it would produce a tenant whose second worker can never
    get a signed certificate — with every spec field green. Better a refusal
    that names the reason.
    """

    @pytest.mark.asyncio
    async def test_talos_on_the_shared_vip_is_refused(self, monkeypatch) -> None:
        from app.api.v1 import tenants_capi

        monkeypatch.setenv("TENANTS_CP_PER_TENANT_VIP", "false")
        monkeypatch.setenv("TENANTS_CP_DEMUX_VIP", "10.199.0.100")
        k8s = MagicMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock()

        with pytest.raises(HTTPException) as e:
            await tenants_capi._create_capi_resources(
                k8s, _req(worker_os="talos"), storage_info=None,
            )

        assert e.value.status_code == 422
        assert "fixed :50001" in e.value.detail
        assert "TENANTS_CP_PER_TENANT_VIP" in e.value.detail
