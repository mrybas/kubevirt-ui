"""The address a worker joins through was nowhere in the product.

The tenant page showed `endpoint` — the ingress URL a human opens — and the
transit SNAT. Neither is what a joining node dials: it reaches the API,
konnectivity and (for Talos) trustd by address and port. A join that fails is
diagnosed against exactly those, and they were readable only with kubectl.

Worse than missing: this stand runs three schemes at once, and nothing
distinguished them.

  * `t8` — its own VIP, standard ports 6443 / 8132 / 50001;
  * `t1`, `t3` — one VIP between them, each on non-standard ports (20000,
    20002, …). The address alone identifies nothing here, so printing it
    without the port would be worse than printing nothing;
  * `tal1` — no load balancer at all; the ingress is the only way in.

Measured on the live stand, all three, before this existed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.tenants_crud import _enrich_with_cp_address
from app.models.tenant import TenantResponse


def _tenant(name: str) -> TenantResponse:
    return TenantResponse(
        name=name, display_name=name, namespace=f"tenant-{name}",
        kubernetes_version="v1.32.1", status="Ready",
        endpoint=f"https://{name}.10.198.175.200.nip.io",
    )


def _svc(ns: str, name: str, ip: str | None, ports: dict[str, int]):
    svc = MagicMock()
    svc.metadata.namespace, svc.metadata.name = ns, name
    if ip is None:
        svc.status.load_balancer.ingress = []
    else:
        ing = MagicMock()
        ing.ip = ip
        svc.status.load_balancer.ingress = [ing]
    svc.spec.ports = []
    for pname, port in ports.items():
        p = MagicMock()
        p.name, p.port = pname, port
        svc.spec.ports.append(p)
    return svc


def _k8s(services: list, kcp: dict | None = None):
    k8s = MagicMock()
    result = MagicMock()
    result.items = services
    k8s.core_api.list_service_for_all_namespaces = AsyncMock(return_value=result)
    if kcp is None:
        from kubernetes_asyncio.client import ApiException
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=404))
    else:
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(return_value=kcp)
    return k8s


class TestTheOwnVipScheme:
    @pytest.mark.asyncio
    async def test_address_and_all_three_ports(self) -> None:
        k8s = _k8s([_svc("tenant-t8", "t8-cp-lb", "10.199.0.101",
                         {"api": 6443, "konnectivity": 8132, "trustd": 50001})])

        cp = (await _enrich_with_cp_address(k8s, _tenant("t8"))).control_plane_address

        assert cp is not None
        assert (cp.address, cp.api_port) == ("10.199.0.101", 6443)
        assert (cp.konnectivity_port, cp.trustd_port) == (8132, 50001)
        assert cp.shared_with == []
        assert cp.source == "service"


class TestTheSharedVipScheme:
    @pytest.mark.asyncio
    async def test_the_port_is_read_from_the_service_not_assumed(self) -> None:
        """Defaulting to 6443 here would print an address:port that nothing
        listens on, which is the most expensive kind of wrong: it looks
        actionable."""
        k8s = _k8s([
            _svc("tenant-t1", "t1-cp-lb", "10.199.0.100",
                 {"api": 20000, "konnectivity": 20001}),
            _svc("tenant-t3", "t3-cp-lb", "10.199.0.100",
                 {"api": 20002, "konnectivity": 20003}),
        ])

        cp = (await _enrich_with_cp_address(k8s, _tenant("t1"))).control_plane_address

        assert (cp.address, cp.api_port) == ("10.199.0.100", 20000)
        assert cp.trustd_port is None

    @pytest.mark.asyncio
    async def test_the_neighbours_on_that_address_are_named(self) -> None:
        k8s = _k8s([
            _svc("tenant-t1", "t1-cp-lb", "10.199.0.100", {"api": 20000}),
            _svc("tenant-t3", "t3-cp-lb", "10.199.0.100", {"api": 20002}),
            _svc("tenant-t8", "t8-cp-lb", "10.199.0.101", {"api": 6443}),
        ])

        cp = (await _enrich_with_cp_address(k8s, _tenant("t1"))).control_plane_address

        assert cp.shared_with == ["t3"]

    @pytest.mark.asyncio
    async def test_a_tenant_alone_on_its_address_is_not_called_shared(self) -> None:
        k8s = _k8s([
            _svc("tenant-t8", "t8-cp-lb", "10.199.0.101", {"api": 6443}),
            _svc("tenant-t1", "t1-cp-lb", "10.199.0.100", {"api": 20000}),
        ])

        cp = (await _enrich_with_cp_address(k8s, _tenant("t8"))).control_plane_address

        assert cp.shared_with == []


class TestNoLoadBalancerAtAll:
    @pytest.mark.asyncio
    async def test_the_advertised_address_is_the_fallback(self) -> None:
        """The Service is what MetalLB assigned and what a node dials;
        `advertiseAddress` is the intent. Preferring the intent would show a
        working address for a control plane whose Service never got one."""
        k8s = _k8s([], kcp={"spec": {"network": {"advertiseAddress": "10.199.0.100"}}})

        cp = (await _enrich_with_cp_address(k8s, _tenant("t3"))).control_plane_address

        assert (cp.address, cp.source) == ("10.199.0.100", "advertised")

    @pytest.mark.asyncio
    async def test_with_neither_it_says_the_ingress_is_the_only_way_in(self) -> None:
        """An empty field reads as "we failed to look"; this tenant genuinely
        has no address of its own."""
        k8s = _k8s([], kcp={"spec": {}})

        cp = (await _enrich_with_cp_address(k8s, _tenant("tal1"))).control_plane_address

        assert cp is not None
        assert cp.source == "ingress"
        assert cp.api_port == 443
        # The same host the page shows as the endpoint, not a second derivation
        # of it.
        assert cp.address == "tal1.10.198.175.200.nip.io"

    @pytest.mark.asyncio
    async def test_a_pending_load_balancer_does_not_become_an_address(self) -> None:
        """A Service with no ingress yet is a VIP that has not been assigned."""
        k8s = _k8s([_svc("tenant-t9", "t9-cp-lb", None, {"api": 6443})],
                   kcp={"spec": {}})

        cp = (await _enrich_with_cp_address(k8s, _tenant("t9"))).control_plane_address

        assert cp.source == "ingress"
