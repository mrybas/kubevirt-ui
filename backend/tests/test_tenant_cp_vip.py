"""One VIP per tenant — and MetalLB, not us, decides which.

The shared-VIP model demultiplexed tenants by port. Talos ends it: trustd is
dialled on a fixed :50001 of the endpoint host, MetalLB only shares an address
between Services with non-overlapping ports, so the third port belongs to
exactly one tenant and the second tenant's Service silently never gets an
address.

The tempting implementation — our own ConfigMap allocator over the pool — is
a second allocator over one key space, which is the failure this codebase has
now hit four times. So the Service is created without an address, MetalLB
assigns one, and we read the fact back.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

from app.api.v1.tenants_cp_vip import (
    TENANT_API_PORT,
    TENANT_KONN_PORT,
    TENANT_TRUSTD_PORT,
    acquire_tenant_vip,
    assert_pool_excluded_from_ovn,
    build_cp_lb_service,
    per_tenant_vip_enabled,
    tenant_cp_ports,
)

TENANT = "t9"
NS = "tenant-t9"


def _svc(ip: str | None):
    svc = MagicMock()
    if ip is None:
        svc.status.load_balancer.ingress = []
    else:
        entry = MagicMock()
        entry.ip = ip
        svc.status.load_balancer.ingress = [entry]
    return svc


def _k8s(assigned: str | None = "10.199.0.101", *, create_conflict: bool = False):
    created: list[dict] = []

    async def create_svc(**kw):
        if create_conflict:
            raise ApiException(status=409, reason="AlreadyExists")
        created.append(kw["body"])
        return kw["body"]

    async def read_svc(**kw):
        return _svc(assigned)

    k8s = MagicMock()
    k8s.core_api.create_namespaced_service = AsyncMock(side_effect=create_svc)
    k8s.core_api.read_namespaced_service = AsyncMock(side_effect=read_svc)
    k8s._created = created
    return k8s


class TestTheServiceAsksForNoAddress:
    def test_no_pinned_ip_and_no_shared_ip_key(self) -> None:
        """Pinning would make us the second allocator over MetalLB's pool."""
        svc = build_cp_lb_service(
            TENANT, NS, ports=tenant_cp_ports("cloud-init"), pool="cp-transit-pool",
        )
        ann = svc["metadata"]["annotations"]

        assert "metallb.universe.tf/loadBalancerIPs" not in ann
        assert "metallb.universe.tf/allow-shared-ip" not in ann, (
            "nothing is shared any more — sharing is what forced port demux"
        )
        assert ann["metallb.universe.tf/address-pool"] == "cp-transit-pool"

    def test_cilium_lb_bypass_is_kept(self) -> None:
        """Without it, in-VPC pod traffic is DNAT'd before kube-ovn routing."""
        svc = build_cp_lb_service(TENANT, NS, ports=[("api", 6443)], pool="p")

        assert svc["metadata"]["annotations"]["service.cilium.io/type"] == "ClusterIP"

    def test_it_selects_this_tenants_control_plane_pods(self) -> None:
        svc = build_cp_lb_service(TENANT, NS, ports=[("api", 6443)], pool="p")

        assert svc["spec"]["selector"] == {"kamaji.clastix.io/name": TENANT}


class TestPortsAreTheSameForEveryTenant:
    def test_api_and_konnectivity_only_for_cloud_init(self) -> None:
        assert tenant_cp_ports("cloud-init") == [
            ("api", TENANT_API_PORT), ("konn", TENANT_KONN_PORT),
        ]

    def test_talos_adds_the_one_port_it_cannot_negotiate(self) -> None:
        """50001 is fixed in Talos — the reason each tenant needs an address."""
        assert ("trustd", TENANT_TRUSTD_PORT) in tenant_cp_ports("talos")

    def test_the_ports_are_the_standard_ones(self) -> None:
        """No per-tenant numbering: that allocator is what this replaces."""
        assert (TENANT_API_PORT, TENANT_KONN_PORT, TENANT_TRUSTD_PORT) == (6443, 8132, 50001)


class TestAcquiringTheAddress:
    @pytest.mark.asyncio
    async def test_it_returns_what_metallb_assigned(self) -> None:
        k8s = _k8s("10.199.0.104")

        vip = await acquire_tenant_vip(k8s, TENANT, NS, worker_os="talos")

        assert vip == "10.199.0.104"

    @pytest.mark.asyncio
    async def test_an_existing_service_is_reused_not_duplicated(self) -> None:
        """A retried create must not consume a second address from the pool."""
        k8s = _k8s("10.199.0.104", create_conflict=True)

        vip = await acquire_tenant_vip(k8s, TENANT, NS, worker_os="talos")

        assert vip == "10.199.0.104"

    @pytest.mark.asyncio
    async def test_an_exhausted_pool_says_what_to_do(self) -> None:
        """Service stays Pending forever; a bare timeout teaches nobody."""
        k8s = _k8s(None)

        with pytest.raises(HTTPException) as e:
            await acquire_tenant_vip(
                k8s, TENANT, NS, worker_os="talos", timeout=0.05, poll=0.01,
            )

        assert e.value.status_code == 409
        assert "exhausted" in e.value.detail
        assert "Append a range" in e.value.detail
        assert "never resize a range in use" in e.value.detail

    @pytest.mark.asyncio
    async def test_it_does_not_wait_for_the_address_to_answer(self) -> None:
        """MetalLB assigns immediately but only announces once endpoints are
        ready — and they cannot be, because the control plane is built after
        this. Waiting for reachability here would deadlock every create."""
        k8s = _k8s("10.199.0.104")

        await acquire_tenant_vip(k8s, TENANT, NS, worker_os="talos", timeout=0.2)

        # One read is enough: the address is in status from the start.
        assert k8s.core_api.read_namespaced_service.await_count == 1


class TestThePoolMustBeExcludedFromKubeOvn:
    def _k8s(self, pool_addresses: list[str], exclude: list[str]):
        async def get_ns(**kw):
            return {"spec": {"addresses": pool_addresses}}

        async def get_cluster(**kw):
            return {"spec": {"excludeIps": exclude}}

        k8s = MagicMock()
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get_ns)
        k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_cluster)
        return k8s

    @pytest.mark.asyncio
    async def test_the_lab_configuration_passes(self) -> None:
        """Verbatim from the cluster: pool .100-.119 inside exclude .1...255."""
        k8s = self._k8s(["10.199.0.100-10.199.0.119"], ["10.199.0.1..10.199.0.255"])

        await assert_pool_excluded_from_ovn(k8s, "cp-transit-pool", "cp-transit")

    @pytest.mark.asyncio
    async def test_a_pool_outside_the_exclusion_is_refused(self) -> None:
        """Otherwise kube-ovn can hand an lrp the address MetalLB gave a VIP."""
        k8s = self._k8s(["10.199.1.10-10.199.1.20"], ["10.199.0.1..10.199.0.255"])

        with pytest.raises(HTTPException) as e:
            await assert_pool_excluded_from_ovn(k8s, "cp-transit-pool", "cp-transit")

        assert e.value.status_code == 422
        assert "10.199.1.10-10.199.1.20" in e.value.detail
        assert "excludeIps" in e.value.detail

    @pytest.mark.asyncio
    async def test_partial_overlap_is_refused_too(self) -> None:
        """The dangerous half is the part that sticks out."""
        k8s = self._k8s(["10.199.0.200-10.199.1.5"], ["10.199.0.1..10.199.0.255"])

        with pytest.raises(HTTPException):
            await assert_pool_excluded_from_ovn(k8s, "cp-transit-pool", "cp-transit")

    @pytest.mark.asyncio
    async def test_several_ranges_are_all_checked(self) -> None:
        """T21: a pool is a LIST of ranges — checking only the first is the bug."""
        k8s = self._k8s(
            ["10.199.0.100-10.199.0.119", "10.199.9.0-10.199.9.10"],
            ["10.199.0.1..10.199.0.255"],
        )

        with pytest.raises(HTTPException) as e:
            await assert_pool_excluded_from_ovn(k8s, "cp-transit-pool", "cp-transit")

        assert "10.199.9.0-10.199.9.10" in e.value.detail

    @pytest.mark.asyncio
    async def test_adjacent_exclusions_count_as_covering(self) -> None:
        k8s = self._k8s(
            ["10.199.0.100-10.199.0.150"],
            ["10.199.0.1..10.199.0.120", "10.199.0.121..10.199.0.255"],
        )

        await assert_pool_excluded_from_ovn(k8s, "cp-transit-pool", "cp-transit")

    @pytest.mark.asyncio
    async def test_an_unreadable_object_does_not_block_creation(self) -> None:
        """A diagnostic must not become the thing that stops tenant creation."""
        k8s = MagicMock()
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=403),
        )
        k8s.custom_api.get_cluster_custom_object = AsyncMock()

        await assert_pool_excluded_from_ovn(k8s, "p", "s")


class TestTheOffSwitch:
    def test_on_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("TENANTS_CP_PER_TENANT_VIP", raising=False)

        assert per_tenant_vip_enabled() is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no"])
    def test_the_shared_vip_model_can_be_kept(self, monkeypatch, value: str) -> None:
        """Existing tenants keep baked ports; the switch is for the window."""
        monkeypatch.setenv("TENANTS_CP_PER_TENANT_VIP", value)

        assert per_tenant_vip_enabled() is False
