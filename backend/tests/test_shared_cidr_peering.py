"""A shared network needs a route, not only permission.

`Isolated` + `Shared networks 10.198.200.0/24` wrote the allow-ACLs and
nothing else:

    allow-related from-lport ip4.dst == 10.198.200.0/24 priority 3100
    spec.staticRoutes: [{"cidr": "0.0.0.0/0", "nextHopIP": "10.198.191.254"}]

so the ping left by the default route and died. Adding the peering by hand
fixed it at two hops (ttl=62) rather than a hairpin through the upstream.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import vpcs as vpcs_mod
from app.api.v1.vpcs import _peer_shared_cidrs, _vpc_owning_cidr


def _subnets(*pairs: tuple[str, str]) -> dict:
    return {"items": [
        {"spec": {"vpc": vpc, "cidrBlock": cidr}} for vpc, cidr in pairs
    ]}


def _k8s(subnets: dict) -> MagicMock:
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value=subnets)
    return k8s


@pytest.mark.asyncio
async def test_finds_the_vpc_that_owns_the_prefix() -> None:
    k8s = _k8s(_subnets(("shared", "10.198.200.0/24"), ("t2", "10.198.193.0/24")))
    assert await _vpc_owning_cidr(k8s, "10.198.200.0/24") == "shared"


@pytest.mark.asyncio
async def test_a_prefix_nobody_here_owns_returns_none() -> None:
    # A corporate range reached over BGP: ACL only, upstream carries it.
    k8s = _k8s(_subnets(("shared", "10.198.200.0/24")))
    assert await _vpc_owning_cidr(k8s, "192.0.2.0/24") is None


@pytest.mark.asyncio
async def test_the_cluster_vpc_is_never_the_answer() -> None:
    k8s = _k8s(_subnets(("ovn-cluster", "10.16.0.0/16")))
    assert await _vpc_owning_cidr(k8s, "10.16.5.0/24") is None


@pytest.mark.asyncio
async def test_garbage_is_skipped_not_raised() -> None:
    k8s = _k8s(_subnets(("shared", "10.198.200.0/24")))
    assert await _vpc_owning_cidr(k8s, "not-a-cidr") is None


@pytest.mark.asyncio
async def test_it_peers_with_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def _pair(_k8s, local, remote, link=None):
        calls.append((local, remote))

    monkeypatch.setattr(vpcs_mod, "_create_peering_pair", _pair)
    k8s = _k8s(_subnets(("shared", "10.198.200.0/24")))

    await _peer_shared_cidrs(k8s, "t3", ["10.198.200.0/24"])

    assert calls == [("t3", "shared")]


@pytest.mark.asyncio
async def test_an_unowned_prefix_peers_with_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def _pair(_k8s, local, remote, link=None):
        calls.append((local, remote))

    monkeypatch.setattr(vpcs_mod, "_create_peering_pair", _pair)
    k8s = _k8s(_subnets(("shared", "10.198.200.0/24")))

    await _peer_shared_cidrs(k8s, "t3", ["192.0.2.0/24"])

    assert calls == []


@pytest.mark.asyncio
async def test_a_failing_peering_does_not_fail_the_vpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The VPC is already created by this point; losing it over a peering
    # would be worse than the missing route.
    async def _boom(_k8s, local, remote, link=None):
        raise ApiException(status=409, reason="Conflict")

    monkeypatch.setattr(vpcs_mod, "_create_peering_pair", _boom)
    k8s = _k8s(_subnets(("shared", "10.198.200.0/24")))

    await _peer_shared_cidrs(k8s, "t3", ["10.198.200.0/24"])


@pytest.mark.asyncio
async def test_a_vpc_does_not_peer_with_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def _pair(_k8s, local, remote, link=None):
        calls.append((local, remote))

    monkeypatch.setattr(vpcs_mod, "_create_peering_pair", _pair)
    k8s = _k8s(_subnets(("t3", "10.198.194.0/24")))

    await _peer_shared_cidrs(k8s, "t3", ["10.198.194.0/24"])

    assert calls == []


def test_the_create_path_calls_it() -> None:
    """A helper nobody calls passes its own tests."""
    import inspect

    src = inspect.getsource(vpcs_mod.create_vpc)
    assert "_peer_shared_cidrs" in src, (
        "shared networks would get the ACL and no route again"
    )
