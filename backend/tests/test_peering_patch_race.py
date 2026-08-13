"""Two peerings touching one VPC must not erase each other.

`spec.vpcPeerings` is a list and a merge patch replaces it whole. Creating
cc1<->cc2, cc3<->cc4, cc1<->cc3 and cc2<->cc4 in one breath, all four returned
201 and the cluster kept four entries where eight belonged:

    ('cc1','cc3',...)  ('cc2','cc1',...)  ('cc3','cc1',...)  ('cc4','cc3',...)

cc1 never got its cc2 entry, cc2 never got cc4, cc4 never got cc2 — each
second writer computed its list from a read taken before the first landed.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1.vpcs import _apply_peering_side


class _FakeVpcStore:
    """A VPC object that enforces resourceVersion the way the API server does."""

    def __init__(self) -> None:
        self.spec: dict = {}
        self.version = 1

    def get(self) -> dict:
        import copy
        return {
            "metadata": {"resourceVersion": str(self.version)},
            "spec": copy.deepcopy(self.spec),
        }

    def patch(self, body: dict) -> None:
        rv = (body.get("metadata") or {}).get("resourceVersion")
        if rv is not None and rv != str(self.version):
            raise ApiException(status=409, reason="Conflict")
        self.spec.update(body["spec"])
        self.version += 1


def _k8s(store: _FakeVpcStore) -> MagicMock:
    k8s = MagicMock()

    async def _get(**kw):
        return store.get()

    async def _patch(**kw):
        await asyncio.sleep(0)  # let a racing coroutine read the same version
        store.patch(kw["body"])

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=_get)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=_patch)
    return k8s


@pytest.mark.asyncio
async def test_concurrent_peerings_on_one_vpc_all_survive() -> None:
    store = _FakeVpcStore()
    k8s = _k8s(store)

    await asyncio.gather(
        _apply_peering_side(k8s, "cc1", "cc2", "169.254.101.9/30",
                            "169.254.101.8/30", ["10.201.0.0/24"], "169.254.101.10"),
        _apply_peering_side(k8s, "cc1", "cc3", "169.254.101.17/30",
                            "169.254.101.16/30", ["10.203.0.0/24"], "169.254.101.18"),
    )

    peers = {p["remoteVpc"] for p in store.spec["vpcPeerings"]}
    assert peers == {"cc2", "cc3"}, f"an entry was overwritten: {peers}"


@pytest.mark.asyncio
async def test_the_routes_of_both_peerings_survive_too() -> None:
    store = _FakeVpcStore()
    k8s = _k8s(store)

    await asyncio.gather(
        _apply_peering_side(k8s, "cc1", "cc2", "169.254.101.9/30",
                            "169.254.101.8/30", ["10.201.0.0/24"], "169.254.101.10"),
        _apply_peering_side(k8s, "cc1", "cc3", "169.254.101.17/30",
                            "169.254.101.16/30", ["10.203.0.0/24"], "169.254.101.18"),
    )

    dsts = {r["cidr"] for r in store.spec.get("staticRoutes", [])}
    assert {"10.201.0.0/24", "10.203.0.0/24"} <= dsts, dsts


@pytest.mark.asyncio
async def test_a_single_write_still_works() -> None:
    store = _FakeVpcStore()
    await _apply_peering_side(_k8s(store), "cc1", "cc2", "169.254.101.9/30",
                              "169.254.101.8/30", ["10.201.0.0/24"], "169.254.101.10")
    assert [p["remoteVpc"] for p in store.spec["vpcPeerings"]] == ["cc2"]


@pytest.mark.asyncio
async def test_a_non_conflict_error_is_not_retried_away() -> None:
    store = _FakeVpcStore()
    k8s = _k8s(store)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=403, reason="Forbidden"),
    )
    with pytest.raises(ApiException) as exc:
        await _apply_peering_side(k8s, "cc1", "cc2", "169.254.101.9/30",
                                  "169.254.101.8/30", ["10.201.0.0/24"], "169.254.101.10")
    assert exc.value.status == 403
