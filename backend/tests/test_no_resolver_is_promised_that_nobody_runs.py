"""A VPC is told where its resolver is only when there is one.

UAT run 4, E2: from a VM in a VPC an IP answers and a name does not. The
subnets hand out `dns_server=10.96.0.200` over DHCP, and this cluster has no
`vpc-dns-config`, no VpcDns CRD, and nothing listening on that address.

The address was arithmetic: take the service CIDR, put 200 in the last octet.
Plausible, never checked, and handed to every guest. When the service CIDR
could not be read at all there was a hardcoded fallback to the same address —
three ways to promise a resolver, none of which asked whether one exists.

`vpc-dns-config` is where kube-ovn's own controller is configured, so its
`coredns-vip` is the cluster stating the address and its absence is the
cluster saying there is none. The operator already reads it to decide whether
a VpcDns will be served at all; one fact, two readers, and neither promises
what the other refuses.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.tenants_common import _vpcdns_vip_from_kubeovn


def _k8s(data: dict | None, *, namespace: str | None = "o0-kube-ovn"):
    k8s = MagicMock()
    if data is None:
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            side_effect=Exception("not found"))
    else:
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            return_value=SimpleNamespace(data=data))
    return k8s, namespace


@pytest.mark.asyncio
class TestWhereTheResolverAddressComesFrom:
    async def _ask(self, data: dict | None, namespace: str | None = "o0-kube-ovn") -> str:
        k8s, ns = _k8s(data, namespace=namespace)
        with patch("app.api.v1.network._find_kubeovn_namespace",
                   AsyncMock(return_value=ns)):
            return await _vpcdns_vip_from_kubeovn(k8s)

    async def test_the_cluster_states_it(self) -> None:
        assert await self._ask({"coredns-vip": "10.96.0.200"}) == "10.96.0.200"

    async def test_whitespace_is_not_an_address(self) -> None:
        assert await self._ask({"coredns-vip": "  "}) == ""

    async def test_a_config_without_the_key_promises_nothing(self) -> None:
        assert await self._ask({"enable-vpc-dns": "true"}) == ""

    async def test_no_config_at_all_promises_nothing(self) -> None:
        """The state of the stand: the feature is off and the CRD is absent."""
        assert await self._ask(None) == ""

    async def test_no_kube_ovn_at_all_promises_nothing(self) -> None:
        assert await self._ask({"coredns-vip": "10.96.0.200"}, namespace=None) == ""


def test_the_address_is_never_computed_from_the_service_cidr() -> None:
    """The arithmetic and its fallback are gone, not merely unused.

    Both produced 10.96.0.200 on this cluster, which is the address in the
    report — a resolver that does not exist, handed to every VM in every VPC.
    """
    import inspect

    from app.api.v1 import tenants_common

    source = inspect.getsource(tenants_common)
    assert "_VPCDNS_VIP_FALLBACK" not in source
    # No "last octet becomes 200" arithmetic anywhere.
    assert '.200"' not in source.replace('# ', '')


def test_an_explicit_override_still_wins() -> None:
    """A deployment that knows better than the cluster's own statement."""
    import inspect

    from app.api.v1 import tenants_common

    source = inspect.getsource(tenants_common._ensure_cluster_config)
    assert source.index("vpcdns_vip = vpcdns_vip_override") < source.index(
        "_vpcdns_vip_from_kubeovn")
