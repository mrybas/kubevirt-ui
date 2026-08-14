"""«Isolated» has to block the other tenants, not a range none of them are in.

On the cluster: `acme-net` 10.100.0.0/24 and `beta-net` 10.205.0.0/24, both
created with Isolated on, and the only drop rule between them was

    ip4.dst == 10.198.192.0/18   (TENANT_SUPERNET)

which contains neither — the allocator hands out `10.{200+N}.0.0/24`. Nothing
was blocking them; they merely had no route to each other yet, and the first
BGP announcement of those prefixes would have turned "Isolated" into a caption.
"""

import pytest

from app.api.v1.subnet_acls import (
    ISOLATION_PRIORITY_DROP,
    build_isolation_acls,
)


def _drops(acls):
    return sorted(
        a.match.split("== ")[-1] for a in acls
        if a.action == "drop" and a.direction == "from-lport"
    )


class TestDropScope:
    def test_drops_every_other_tenant_prefix(self) -> None:
        acls = build_isolation_acls(
            subnet_cidr="10.100.0.0/24",
            tenant_supernet="10.198.192.0/18",
            peer_cidrs=["10.205.0.0/24", "10.206.0.0/24"],
        )
        assert _drops(acls) == ["10.205.0.0/24", "10.206.0.0/24"]

    def test_never_drops_its_own_prefix(self) -> None:
        acls = build_isolation_acls(
            subnet_cidr="10.100.0.0/24",
            tenant_supernet="",
            peer_cidrs=["10.100.0.0/24", "10.205.0.0/24"],
        )
        assert _drops(acls) == ["10.205.0.0/24"]

    def test_falls_back_to_the_supernet_for_the_first_vpc(self) -> None:
        acls = build_isolation_acls(
            subnet_cidr="10.100.0.0/24",
            tenant_supernet="10.198.192.0/18",
            peer_cidrs=[],
        )
        assert _drops(acls) == ["10.198.192.0/18"]

    def test_writes_nothing_when_there_is_nothing_to_scope_to(self) -> None:
        # A drop with no scope would take the internet with it.
        assert build_isolation_acls("10.100.0.0/24", "", peer_cidrs=[]) == []

    def test_shared_prefixes_still_outrank_the_drop(self) -> None:
        acls = build_isolation_acls(
            subnet_cidr="10.100.0.0/24",
            tenant_supernet="",
            shared_cidrs=["10.205.0.0/24"],
            peer_cidrs=["10.205.0.0/24"],
        )
        allow = [a for a in acls if a.action == "allow-related" and "10.205" in a.match]
        drop = [a for a in acls if a.action == "drop"]
        assert allow and drop
        assert min(a.priority for a in allow) > ISOLATION_PRIORITY_DROP


@pytest.mark.asyncio
class TestReconcileTeachesTheOldVpcsAboutTheNewOne:
    async def test_it_rewrites_every_isolated_subnet(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from app.api.v1.vpcs import reconcile_isolation_acls

        subnets = {"items": [
            {"metadata": {"name": "a-default"}, "spec": {
                "vpc": "a", "cidrBlock": "10.200.0.0/24",
                "acls": [{"action": "drop", "direction": "from-lport",
                          "match": "ip4.dst == 10.198.192.0/18", "priority": 3000}],
            }},
            {"metadata": {"name": "b-default"}, "spec": {
                "vpc": "b", "cidrBlock": "10.201.0.0/24",
                "acls": [{"action": "drop", "direction": "from-lport",
                          "match": "ip4.dst == 10.198.192.0/18", "priority": 3000}],
            }},
            {"metadata": {"name": "ovn-default"}, "spec": {
                "vpc": "ovn-cluster", "cidrBlock": "10.16.0.0/16",
            }},
        ]}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value=subnets)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        assert await reconcile_isolation_acls(k8s) == 2

        written = {
            c.kwargs["name"]: c.kwargs["body"]["spec"]["acls"]
            for c in k8s.custom_api.patch_cluster_custom_object.await_args_list
        }
        a_drops = [x["match"] for x in written["a-default"] if x["action"] == "drop"]
        assert any("10.201.0.0/24" in m for m in a_drops), a_drops
        assert not any("10.200.0.0/24" in m for m in a_drops), "not its own prefix"

    async def test_it_leaves_un_isolated_vpcs_alone(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from app.api.v1.vpcs import reconcile_isolation_acls

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [
            {"metadata": {"name": "open-default"},
             "spec": {"vpc": "open", "cidrBlock": "10.202.0.0/24", "acls": []}},
        ]})
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        assert await reconcile_isolation_acls(k8s) == 0
        k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


def test_the_create_path_passes_the_client_it_actually_has():
    """`create_vpc` binds the client as `k8s`; the reconcile call used
    `k8s_client` and blew up with NameError at the end of a successful create,
    surfacing in the UI as a bare "Request failed:".
    """
    from pathlib import Path

    src = Path("app/api/v1/vpcs.py").read_text()
    start = src.index("async def create_vpc(")
    end = src.index("\n@router", start)
    body = src[start:end]
    assert "reconcile_isolation_acls(k8s)" in body
    # The only legitimate mention is where the client is bound.
    assert body.count("k8s_client") == 1
    assert "k8s = request.app.state.k8s_client" in body


def test_every_create_re_scopes_not_only_isolated_ones():
    """An un-isolated VPC is still a tenant the isolated ones must not reach.

    Gating the reconcile on the *new* VPC's own setting punched a hole in
    every isolated VPC each time an un-isolated one appeared: four VPCs
    created concurrently took 10.211–10.214, and `acme-net`, isolated, went
    on dropping only 10.205/206/208/209.
    """
    from pathlib import Path

    src = Path("app/api/v1/vpcs.py").read_text()
    start = src.index("async def create_vpc(")
    body = src[start:src.index("\n@router.", start)]
    assert "reconcile_isolation_acls(k8s)" in body
    assert "if data.isolated:\n        try:\n            n = await reconcile" not in body


def test_delete_re_scopes_too():
    """A stale drop rule blocks whoever is handed that CIDR next."""
    from pathlib import Path

    src = Path("app/api/v1/vpcs.py").read_text()
    start = src.index("async def delete_vpc(")
    body = src[start:src.index("\n@router.", start)]
    assert "reconcile_isolation_acls(k8s)" in body


def _subnet(name, vpc, cidr, drops, dying=False):
    meta = {"name": name}
    if dying:
        meta["deletionTimestamp"] = "2026-08-14T11:00:00Z"
    return {"metadata": meta, "spec": {"vpc": vpc, "cidrBlock": cidr, "acls": [
        {"action": "allow-related", "direction": "from-lport",
         "match": f"ip4.dst == {cidr}", "priority": 3200},
    ] + [
        {"action": "drop", "direction": "from-lport",
         "match": f"ip4.dst == {d}", "priority": 3000} for d in drops
    ]}}


async def _reconcile(items):
    from unittest.mock import AsyncMock, MagicMock

    from app.api.v1.vpcs import reconcile_isolation_acls

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": items})
    k8s.custom_api.patch_cluster_custom_object = AsyncMock()
    await reconcile_isolation_acls(k8s)
    return {
        c.kwargs["name"]: c.kwargs["body"]["spec"]["acls"]
        for c in k8s.custom_api.patch_cluster_custom_object.await_args_list
    }


@pytest.mark.asyncio
async def test_a_subnet_being_deleted_is_not_a_peer():
    """kube-ovn keeps a deleted subnet listed while its finalizer runs.

    Counting it left every surviving VPC dropping a prefix that no longer
    exists — seen on the cluster after removing four VPCs, with `acme-net`
    still dropping 10.208.0.0/24, 10.209.0.0/24 and 10.212.0.0/24. The
    allocator hands those same CIDRs to the next VPC, which would then be
    unreachable from acme-net for a reason absent from its own spec.
    """
    written = await _reconcile([
        _subnet("a-default", "a", "10.200.0.0/24", ["10.201.0.0/24", "10.202.0.0/24"]),
        _subnet("b-default", "b", "10.201.0.0/24", ["10.200.0.0/24", "10.202.0.0/24"]),
        _subnet("dying-default", "dying", "10.202.0.0/24", ["10.200.0.0/24"], dying=True),
    ])

    assert "dying-default" not in written, "a dying subnet is not re-scoped"
    drops = [x["match"] for x in written["a-default"] if x["action"] == "drop"]
    assert any("10.201.0.0/24" in m for m in drops), drops
    assert not any("10.202.0.0/24" in m for m in drops), drops


@pytest.mark.asyncio
async def test_last_peer_gone_clears_the_drop(monkeypatch):
    """The final delete has to leave a clean subnet, not one stale rule."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("TENANT_SUPERNET", "")
    written = await _reconcile([
        _subnet("a-default", "a", "10.200.0.0/24", ["10.202.0.0/24"]),
        _subnet("dying-default", "dying", "10.202.0.0/24", [], dying=True),
    ])
    get_settings.cache_clear()

    assert [x for x in written["a-default"] if x["action"] == "drop"] == []
    assert written["a-default"], "the VPC keeps its own allow rules"
