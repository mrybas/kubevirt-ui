"""Two peerings created at the same moment must not share a link.

Measured, firing three peerings concurrently from the browser:

    cc1<->cc2 -> 201  link_cidr 169.254.101.8/30  local 169.254.101.9
    cc3<->cc4 -> 201  link_cidr 169.254.101.8/30  local 169.254.101.9
    cc1<->cc3 -> 201  link_cidr 169.254.101.4/30

and on the cluster:

    cc1 {'localConnectIP': '169.254.101.9/30',  'remoteVpc': 'cc2'}
    cc3 {'localConnectIP': '169.254.101.9/30',  'remoteVpc': 'cc4'}

Two unrelated VPC routers holding the same address on what is meant to be a
point-to-point link. `allocate_peering_link` read the used set and picked the
first gap with nothing reserving it, so both callers picked the same one —
while the VPC CIDR allocator beside it has used a counter ConfigMap with
optimistic locking all along.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.core import allocators
from app.core.allocators import allocate_peering_link


class _FakeConfigMaps:
    """A ConfigMap store that enforces resourceVersion like the API server."""

    def __init__(self) -> None:
        self.data = {"next_index": "0"}
        self.version = 1

    def read(self):
        cm = MagicMock()
        cm.data = dict(self.data)
        cm.metadata.resource_version = str(self.version)
        return cm

    def replace(self, body):
        if body.metadata.resource_version != str(self.version):
            raise ApiException(status=409, reason="Conflict")
        self.data = dict(body.data)
        self.version += 1
        return self.read()


def _k8s(store: _FakeConfigMaps, used: list[str]) -> MagicMock:
    k8s = MagicMock()

    async def _read(**kw):
        return store.read()

    async def _replace(**kw):
        # Yield first, so a racing coroutine gets to read the same version.
        await asyncio.sleep(0)
        return store.replace(kw["body"])

    k8s.core_api.read_namespaced_config_map = AsyncMock(side_effect=_read)
    k8s.core_api.replace_namespaced_config_map = AsyncMock(side_effect=_replace)
    k8s.core_api.create_namespaced_config_map = AsyncMock(side_effect=lambda **kw: store.read())
    return k8s


@pytest.fixture
def no_existing(monkeypatch: pytest.MonkeyPatch):
    async def _none(_k8s):
        return []

    monkeypatch.setattr(allocators, "list_peering_link_cidrs", _none)


@pytest.mark.asyncio
async def test_concurrent_allocations_are_all_distinct(no_existing) -> None:
    store = _FakeConfigMaps()
    k8s = _k8s(store, [])

    results = await asyncio.gather(*[allocate_peering_link(k8s) for _ in range(4)])
    cidrs = [c for _, _, c in results]

    assert len(set(cidrs)) == 4, f"link handed out twice: {cidrs}"


@pytest.mark.asyncio
async def test_sequential_allocations_walk_the_pool(no_existing) -> None:
    store = _FakeConfigMaps()
    k8s = _k8s(store, [])

    first = await allocate_peering_link(k8s)
    second = await allocate_peering_link(k8s)

    assert first[2] != second[2]


@pytest.mark.asyncio
async def test_a_link_already_in_use_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # A peering written by hand does not move the counter, so the used set
    # still has to be consulted.
    taken = allocators.peering_link_addresses(0)[2]

    async def _used(_k8s):
        return [taken]

    monkeypatch.setattr(allocators, "list_peering_link_cidrs", _used)

    store = _FakeConfigMaps()
    _, _, cidr = await allocate_peering_link(_k8s(store, [taken]))
    assert cidr != taken


@pytest.mark.asyncio
async def test_an_exhausted_pool_is_refused_not_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    async def _all_used(_k8s):
        return [allocators.peering_link_addresses(i)[2]
                for i in range(allocators.PEERING_LINK_MAX)]

    monkeypatch.setattr(allocators, "list_peering_link_cidrs", _all_used)

    store = _FakeConfigMaps()
    with pytest.raises(HTTPException) as exc:
        await allocate_peering_link(_k8s(store, []))
    assert exc.value.status_code == 409
    assert "exhausted" in str(exc.value.detail)
