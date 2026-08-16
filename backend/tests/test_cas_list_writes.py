"""Concurrent writers must not drop each other's list entries.

`Vpc.spec.vpcPeerings` and `Subnet.spec.acls` are lists that several flows
append to. A merge patch replaces an array wholesale, so a read-modify-write
without compare-and-set loses the entry written between the read and the patch.
Both call sites report success, which is what makes it expensive to find.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from app.core.cas import patch_spec_with_retry, upsert


class FakeCR:
    """A cluster-scoped CR that enforces resourceVersion like the API server."""

    def __init__(self, name: str, spec: dict | None = None) -> None:
        self.name = name
        self.spec = spec or {}
        self.rv = 1
        self.conflicts_to_raise = 0

    async def get(self, **kw) -> dict:
        return {
            "metadata": {"name": self.name, "resourceVersion": str(self.rv)},
            "spec": {k: list(v) if isinstance(v, list) else v for k, v in self.spec.items()},
        }

    async def patch(self, **kw) -> dict:
        body = kw["body"]
        sent_rv = body["metadata"]["resourceVersion"]
        if self.conflicts_to_raise > 0:
            self.conflicts_to_raise -= 1
            self.rv += 1  # somebody else won
            raise ApiException(status=409, reason="Conflict")
        if sent_rv != str(self.rv):
            raise ApiException(status=409, reason="Conflict")
        self.spec.update(body["spec"])
        self.rv += 1
        return await self.get()


def _k8s(cr: FakeCR) -> MagicMock:
    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=cr.get)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=cr.patch)
    return k8s


@pytest.mark.asyncio
async def test_a_stale_write_is_retried_against_the_winner() -> None:
    cr = FakeCR("team-a", {"vpcPeerings": [{"remoteVpc": "egw", "localConnectIP": "10.255.0.2/24"}]})
    cr.conflicts_to_raise = 1  # first attempt loses the race
    k8s = _k8s(cr)

    def add_ovn_cluster(spec: dict) -> dict:
        return {
            "vpcPeerings": upsert(
                spec.get("vpcPeerings", []) or [],
                {"remoteVpc": "ovn-cluster", "localConnectIP": "10.255.1.2/24"},
                "remoteVpc",
            )
        }

    assert await patch_spec_with_retry(k8s, "vpcs", "team-a", add_ovn_cluster) is True

    remotes = {p["remoteVpc"] for p in cr.spec["vpcPeerings"]}
    assert remotes == {"egw", "ovn-cluster"}, "the retry dropped the entry it raced with"


@pytest.mark.asyncio
async def test_upsert_never_drops_foreign_entries() -> None:
    """The exact shape of the lab incident: adding one peering must keep the other."""
    cr = FakeCR("team-a", {"vpcPeerings": [{"remoteVpc": "egw-egw-team-a", "localConnectIP": "10.255.0.2/24"}]})
    k8s = _k8s(cr)

    await patch_spec_with_retry(
        k8s, "vpcs", "team-a",
        lambda spec: {"vpcPeerings": upsert(
            spec.get("vpcPeerings", []) or [],
            {"remoteVpc": "ovn-cluster", "localConnectIP": "10.255.1.2/24"},
            "remoteVpc",
        )},
    )

    assert len(cr.spec["vpcPeerings"]) == 2


@pytest.mark.asyncio
async def test_no_request_when_mutate_returns_none() -> None:
    cr = FakeCR("team-a", {"vpcPeerings": []})
    k8s = _k8s(cr)

    assert await patch_spec_with_retry(k8s, "vpcs", "team-a", lambda spec: None) is False
    k8s.custom_api.patch_cluster_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_missing_object_is_not_an_error() -> None:
    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="NotFound")
    )
    assert await patch_spec_with_retry(k8s, "vpcs", "gone", lambda spec: {"x": 1}) is False


@pytest.mark.asyncio
async def test_conflicts_eventually_give_up() -> None:
    cr = FakeCR("team-a", {"vpcPeerings": []})
    cr.conflicts_to_raise = 99
    k8s = _k8s(cr)

    with pytest.raises(ApiException):
        await patch_spec_with_retry(
            k8s, "vpcs", "team-a", lambda spec: {"vpcPeerings": []}, retries=3,
        )


@pytest.mark.asyncio
async def test_adding_two_acls_keeps_both() -> None:
    """`spec.acls` is the other list several flows append to."""
    from app.api.v1 import subnet_acls as mod

    cr = FakeCR("team-a-default", {"cidrBlock": "10.200.4.0/22", "acls": []})
    k8s = _k8s(cr)
    request = MagicMock()
    request.app.state.k8s_client = k8s

    for prio in (3200, 3000):
        await mod.add_subnet_acl(
            request=request, name="team-a-default",
            data=mod.SubnetAclAddRequest(
                action="drop", direction="from-lport",
                match=f"ip4.dst == 10.200.{prio}.0/22", priority=prio,
            ),
        )

    assert len(cr.spec["acls"]) == 2, "the second add replaced the first"


@pytest.mark.asyncio
async def test_deleting_an_acl_removes_that_rule() -> None:
    from app.api.v1 import subnet_acls as mod

    rules = [
        {"action": "allow-related", "direction": "from-lport", "match": "a", "priority": 3200},
        {"action": "drop", "direction": "from-lport", "match": "b", "priority": 3000},
    ]
    cr = FakeCR("team-a-default", {"cidrBlock": "10.200.4.0/22", "acls": rules})
    k8s = _k8s(cr)
    request = MagicMock()
    request.app.state.k8s_client = k8s

    out = await mod.delete_subnet_acl(request=request, name="team-a-default", index=0)

    assert out["removed_acl"]["match"] == "a"
    assert [r["match"] for r in cr.spec["acls"]] == ["b"]
