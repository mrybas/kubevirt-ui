"""A new peering is described; an old one stays the product's.

The endpoint patches both routers and forgets. A peering is desired state held
on two of them, and nothing reconciles it: an entry removed by hand, or lost by
kube-ovn, stays lost, and a process that dies between the two writes leaves a
peering configured on one side only — a black hole with nothing anywhere
remembering to undo it.

The flag decides who writes **new** peerings. Ownership of the existing ones is
the ManagedNetworkPeering object itself: a pair belongs to the operator when an
object claims it. So there is no cutover — nothing is rewritten when the flag
goes on, and nothing is orphaned when it goes off.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.vpcs import (
    _claimed_remotes, create_vpc_peering, delete_vpc_peering, peering_cr_name,
)
from app.models.vpc import VpcPeeringCreateRequest


def _stand(monkeypatch, *, handed_over: bool, existing: list[dict] | None = None):
    import app.api.v1.vpcs as mod

    monkeypatch.setattr(mod, "peering_path_enabled", lambda: handed_over)
    monkeypatch.setattr(mod, "reconcile_isolation_acls", AsyncMock())

    objects = {o["metadata"]["name"]: o for o in (existing or [])}
    k8s = MagicMock()

    async def get_cr(**kw):
        if kw["name"] not in objects:
            raise ApiException(status=404)
        return objects[kw["name"]]

    async def list_cr(**_kw):
        return {"items": list(objects.values())}

    async def create_cr(**kw):
        objects[kw["body"]["metadata"]["name"]] = kw["body"]
        return kw["body"]

    async def delete_cr(**kw):
        objects.pop(kw["name"], None)
        return {}

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_cr)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_cr)
    k8s.custom_api.create_cluster_custom_object = AsyncMock(side_effect=create_cr)
    k8s.custom_api.delete_cluster_custom_object = AsyncMock(side_effect=delete_cr)

    request = MagicMock()
    request.app.state.k8s_client = k8s
    return request, k8s, objects


def _claim(a: str, b: str) -> dict:
    return {
        "metadata": {"name": peering_cr_name(a, b)},
        "spec": {"networks": sorted((a, b))},
    }


def test_the_name_is_the_same_from_either_side() -> None:
    """A peering is symmetric; the endpoint is not. Without an ordering, the
    same link asked for from both sides is two objects racing to write the same
    two routers."""
    assert peering_cr_name("b", "a") == peering_cr_name("a", "b") == "a-b"


@pytest.mark.asyncio
async def test_a_new_peering_is_described_not_applied(monkeypatch) -> None:
    request, k8s, objects = _stand(monkeypatch, handed_over=True)
    import app.api.v1.vpcs as mod

    applied = AsyncMock()
    monkeypatch.setattr(mod, "_create_peering_pair", applied)

    got = await create_vpc_peering(
        request, "net-a", VpcPeeringCreateRequest(remote_vpc="net-b"),
        user=MagicMock(),
    )

    applied.assert_not_awaited()
    assert "net-a-net-b" in objects
    assert objects["net-a-net-b"]["spec"]["networks"] == ["net-a", "net-b"]
    assert got.remote_vpc == "net-b"


@pytest.mark.asyncio
async def test_with_the_flag_off_nothing_changes(monkeypatch) -> None:
    request, k8s, objects = _stand(monkeypatch, handed_over=False)
    import app.api.v1.vpcs as mod

    applied = AsyncMock(return_value=SimpleNamespace(
        name="net-a-to-net-b", local_vpc="net-a", remote_vpc="net-b"))
    monkeypatch.setattr(mod, "_create_peering_pair", applied)

    await create_vpc_peering(
        request, "net-a", VpcPeeringCreateRequest(remote_vpc="net-b"),
        user=MagicMock(),
    )

    applied.assert_awaited()
    assert objects == {}
    k8s.custom_api.create_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_removing_an_owned_peering_removes_its_object(monkeypatch) -> None:
    """Not the ends. Taking those off from here is a second writer removing what
    the first still believes it holds — and the first puts them back."""
    request, k8s, objects = _stand(
        monkeypatch, handed_over=True, existing=[_claim("net-a", "net-b")],
    )
    import app.api.v1.vpcs as mod

    by_hand = AsyncMock()
    monkeypatch.setattr(mod, "_remove_peering_side", by_hand)

    await delete_vpc_peering(request, "net-a", "net-b", user=MagicMock())

    by_hand.assert_not_awaited()
    assert objects == {}


@pytest.mark.asyncio
async def test_removing_a_product_peering_still_takes_both_ends_off(monkeypatch) -> None:
    """The flag does not decide this — the absence of a claim does. An old
    peering stays the product's to remove, with the flag on or off."""
    request, k8s, objects = _stand(monkeypatch, handed_over=True)
    import app.api.v1.vpcs as mod

    by_hand = AsyncMock()
    monkeypatch.setattr(mod, "_remove_peering_side", by_hand)

    await delete_vpc_peering(request, "old-a", "old-b", user=MagicMock())

    assert by_hand.await_count == 2, "one side left configured is a black hole"
    k8s.custom_api.delete_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_vpc_knows_which_of_its_peerings_are_claimed(monkeypatch) -> None:
    _request, k8s, _objects = _stand(
        monkeypatch, handed_over=True,
        existing=[_claim("net-a", "net-b"), _claim("net-c", "net-a"),
                  _claim("net-x", "net-y")],
    )
    assert await _claimed_remotes(k8s, "net-a") == {"net-b", "net-c"}
    assert await _claimed_remotes(k8s, "net-y") == {"net-x"}
    assert await _claimed_remotes(k8s, "net-z") == set()


@pytest.mark.asyncio
async def test_a_cluster_without_the_crd_is_not_a_crash(monkeypatch) -> None:
    """Half-migrated clusters are the normal state, and the reader runs on
    every VPC delete."""
    _request, k8s, _objects = _stand(monkeypatch, handed_over=True)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=404))
    assert await _claimed_remotes(k8s, "net-a") == set()


@pytest.mark.asyncio
async def test_deleting_a_vpc_releases_each_peering_from_its_owner(monkeypatch) -> None:
    """A VPC is usually in several, and they need not have the same owner.

    The claimed one goes by its object — removing the far side by hand would be
    undone by the controller, which then writes an entry pointing at a router
    that no longer exists.
    """
    import app.api.v1.vpcs as mod

    _request, k8s, objects = _stand(
        monkeypatch, handed_over=True, existing=[_claim("net-a", "new-peer")],
    )
    monkeypatch.setattr(mod, "_get_vpc_peerings", AsyncMock(return_value=[
        SimpleNamespace(remote_vpc="new-peer"),
        SimpleNamespace(remote_vpc="old-peer"),
    ]))
    by_hand = AsyncMock()
    monkeypatch.setattr(mod, "_remove_peering_side", by_hand)

    await mod._release_peerings(k8s, "net-a")

    assert objects == {}, "the claimed peering's object is still there"
    assert [c.args for c in by_hand.await_args_list] == [(k8s, "old-peer", "net-a")], (
        "the claimed peering was also taken off by hand, or the unclaimed one "
        "was not"
    )
