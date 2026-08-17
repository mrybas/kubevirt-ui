"""An infrastructure VPC is peered with every tenant, without being asked.

It exists to be reached — a VPN concentrator, a shared service, the thing an
environment routes through. Left to the operator it is N manual peerings that
have to be repeated for every VPC created afterwards, and the forgotten one
fails as silence: the VPC is healthy, the service is healthy, and there is no
route between them.

The invariant that matters is under concurrency. Several VPCs are created at
the same moment — a tenant wizard makes one per environment — and each pass
decides what is missing from a list it read. Read the hub once per pair and two
passes both see "no peering with me" and allocate the same link CIDR; the
second write wins and the first VPC has a route to a leg that no longer exists.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import vpcs


def _subnet(name, vpc, cidr, *, role=None, acls=None):
    labels = {"kubevirt-ui.io/role": role} if role else {}
    return {"metadata": {"name": name, "labels": labels},
            "spec": {"vpc": vpc, "cidrBlock": cidr, "acls": acls or []}}


def _k8s(subnets, peerings_by_vpc):
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(
        return_value={"items": subnets})

    async def get_obj(**kw):
        name = kw["name"]
        return {"metadata": {"name": name},
                "spec": {"vpcPeerings": [
                    {"remoteVpc": r} for r in peerings_by_vpc.get(name, [])]}}

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    return k8s


class TestEveryTenantGetsALeg:
    @pytest.mark.asyncio
    async def test_all_of_them(self, monkeypatch) -> None:
        pairs: list[tuple[str, str]] = []

        async def fake_pair(k8s, a, b, link=None):
            pairs.append((a, b))
            return MagicMock()

        monkeypatch.setattr(vpcs, "_create_peering_pair", fake_pair)

        k8s = _k8s([
            _subnet("infra-default", "infra", "10.199.60.0/24",
                    role="infrastructure"),
            _subnet("a-default", "a", "10.200.0.0/22"),
            _subnet("b-default", "b", "10.200.4.0/22"),
            _subnet("c-default", "c", "10.200.8.0/22"),
        ], {})

        assert await vpcs.reconcile_infra_peerings(k8s) == 3
        assert sorted(t for _, t in pairs) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_an_existing_leg_is_not_built_twice(self, monkeypatch) -> None:
        """A second peering for the same pair allocates a second link CIDR and
        the later write replaces the earlier — the first leg simply vanishes."""
        pairs: list[tuple[str, str]] = []

        async def fake_pair(k8s, a, b, link=None):
            pairs.append((a, b))
            return MagicMock()

        monkeypatch.setattr(vpcs, "_create_peering_pair", fake_pair)

        k8s = _k8s([
            _subnet("infra-default", "infra", "10.199.60.0/24",
                    role="infrastructure"),
            _subnet("a-default", "a", "10.200.0.0/22"),
            _subnet("b-default", "b", "10.200.4.0/22"),
        ], {"infra": ["a"]})

        assert await vpcs.reconcile_infra_peerings(k8s) == 1
        assert pairs == [("infra", "b")]

    @pytest.mark.asyncio
    async def test_concurrent_passes_do_not_both_build_the_same_pair(
        self, monkeypatch,
    ) -> None:
        """The real shape of it: several creations land together, so several
        passes run at once. Each must see the peerings the others wrote."""
        import asyncio

        state: dict[str, list[str]] = {"infra": []}
        built: list[str] = []

        async def fake_pair(k8s, a, b, link=None):
            await asyncio.sleep(0)          # a real write yields
            state["infra"].append(b)
            built.append(b)
            return MagicMock()

        monkeypatch.setattr(vpcs, "_create_peering_pair", fake_pair)

        subnets = [
            _subnet("infra-default", "infra", "10.199.60.0/24",
                    role="infrastructure"),
            _subnet("a-default", "a", "10.200.0.0/22"),
            _subnet("b-default", "b", "10.200.4.0/22"),
        ]

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(
            return_value={"items": subnets})

        async def get_obj(**kw):
            # Live read, so a pass sees what a concurrent pass has written.
            return {"metadata": {"name": kw["name"]},
                    "spec": {"vpcPeerings": [
                        {"remoteVpc": r} for r in state["infra"]]}}

        k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)

        await asyncio.gather(*(vpcs.reconcile_infra_peerings(k8s) for _ in range(3)))

        # Without serialisation this was ['a','a','a','b','b','b']: every pass
        # read before any pass wrote, so re-reading before each write bought
        # nothing. The lock is what fixes it, and it covers one backend
        # process — a second replica would race again.
        assert sorted(built) == ["a", "b"], f"duplicate legs built: {built}"


class TestWhatItRefusesToTouch:
    @pytest.mark.asyncio
    async def test_two_infrastructure_vpcs_are_not_peered_to_each_other(
        self, monkeypatch,
    ) -> None:
        """They serve tenants; a mesh between them is a decision nobody made."""
        pairs: list[tuple[str, str]] = []

        async def fake_pair(k8s, a, b, link=None):
            pairs.append((a, b))
            return MagicMock()

        monkeypatch.setattr(vpcs, "_create_peering_pair", fake_pair)

        k8s = _k8s([
            _subnet("i1-default", "i1", "10.199.60.0/24", role="infrastructure"),
            _subnet("i2-default", "i2", "10.199.61.0/24", role="infrastructure"),
        ], {})

        assert await vpcs.reconcile_infra_peerings(k8s) == 0
        assert pairs == []

    @pytest.mark.asyncio
    async def test_the_egress_gateway_is_not_a_hub_for_this(self, monkeypatch) -> None:
        """It carries a role, so it is not a tenant — and it is not
        `infrastructure` either, so it does not acquire a peering with every
        VPC in the cluster."""
        pairs: list[tuple[str, str]] = []

        async def fake_pair(k8s, a, b, link=None):
            pairs.append((a, b))
            return MagicMock()

        monkeypatch.setattr(vpcs, "_create_peering_pair", fake_pair)

        gw = _subnet("egw-x-subnet", "egw-x", "10.199.128.0/24")
        gw["metadata"]["labels"] = {"kubevirt-ui.io/egress-gateway": "x"}

        k8s = _k8s([gw, _subnet("a-default", "a", "10.200.0.0/22")], {})

        assert await vpcs.reconcile_infra_peerings(k8s) == 0

    @pytest.mark.asyncio
    async def test_a_vpc_without_a_subnet_is_skipped_not_attempted(
        self, monkeypatch,
    ) -> None:
        """`_create_peering_pair` refuses it with a 400; attempting it once per
        pass would fill the log with a failure that is not one."""
        pairs: list[tuple[str, str]] = []

        async def fake_pair(k8s, a, b, link=None):
            pairs.append((a, b))
            return MagicMock()

        monkeypatch.setattr(vpcs, "_create_peering_pair", fake_pair)

        k8s = _k8s([
            _subnet("infra-default", "infra", "10.199.60.0/24",
                    role="infrastructure"),
            {"metadata": {"name": "empty-default", "labels": {}},
             "spec": {"vpc": "empty", "cidrBlock": ""}},
        ], {})

        assert await vpcs.reconcile_infra_peerings(k8s) == 0

    @pytest.mark.asyncio
    async def test_it_never_removes_a_peering(self, monkeypatch) -> None:
        """A peering the operator deleted on purpose would otherwise return on
        the next pass; a reconciler that argues with the operator is worse than
        one that does too little."""
        k8s = _k8s([
            _subnet("infra-default", "infra", "10.199.60.0/24",
                    role="infrastructure"),
        ], {"infra": ["gone", "also-gone"]})
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        await vpcs.reconcile_infra_peerings(k8s)

        k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_infrastructure_vpc_means_no_work_and_no_reads(self) -> None:
        k8s = _k8s([_subnet("a-default", "a", "10.200.0.0/22")], {})

        assert await vpcs.reconcile_infra_peerings(k8s) == 0
        k8s.custom_api.get_cluster_custom_object.assert_not_awaited()


class TestAPeeringWithoutAnAllowCarriesNothing:
    """Measured, and it is the reason this pass exists.

    `b3v` and `b3w` were peered from the UI. Both sides written, both routes
    present, the row showed Active — and both directions timed out (exit=28),
    because isolation still dropped the peer's prefix. The peering built the
    path and lifted nothing.

    So the allow is derived from the peering list: one fact, read where it
    lives, rather than a second record that has to be kept in step.
    """

    @pytest.mark.asyncio
    async def test_a_peered_prefix_is_allowed(self, monkeypatch) -> None:
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources", AsyncMock(return_value=[]))

        patched: list[dict] = []

        async def list_obj(**kw):
            if kw["plural"] == "vpcs":
                return {"items": [
                    {"metadata": {"name": "b3v"},
                     "spec": {"vpcPeerings": [{"remoteVpc": "b3w"}]}},
                    {"metadata": {"name": "b3w"},
                     "spec": {"vpcPeerings": [{"remoteVpc": "b3v"}]}},
                    {"metadata": {"name": "t8v"}, "spec": {}},
                ]}
            return {"items": [
                _subnet("b3v-default", "b3v", "10.200.36.0/22"),
                _subnet("b3w-default", "b3w", "10.200.40.0/22"),
                _subnet("t8v-default", "t8v", "10.200.24.0/22"),
            ]}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        b3v = [c for c in patched if c["name"] == "b3v-default"][0]
        acls = b3v["body"]["spec"]["acls"]
        allows = [a for a in acls if a["action"] == "allow-related"
                  and "10.200.40.0/22" in a["match"]]
        drops = [a for a in acls if a["action"] == "drop"
                 and "10.200.40.0/22" in a["match"]]
        assert allows, "the peered prefix was not allowed"
        assert all(a["priority"] > d["priority"] for a in allows for d in drops), (
            "the allow does not outrank the drop it has to lift"
        )

    @pytest.mark.asyncio
    async def test_an_unpeered_tenant_is_still_dropped(self, monkeypatch) -> None:
        """The control that makes the measurement above mean anything."""
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources", AsyncMock(return_value=[]))

        patched: list[dict] = []

        async def list_obj(**kw):
            if kw["plural"] == "vpcs":
                return {"items": [{"metadata": {"name": "b3v"},
                                   "spec": {"vpcPeerings": [{"remoteVpc": "b3w"}]}}]}
            return {"items": [
                _subnet("b3v-default", "b3v", "10.200.36.0/22"),
                _subnet("b3w-default", "b3w", "10.200.40.0/22"),
                _subnet("t8v-default", "t8v", "10.200.24.0/22"),
            ]}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        acls = [c for c in patched if c["name"] == "b3v-default"][0]["body"]["spec"]["acls"]
        assert not [a for a in acls if a["action"] == "allow-related"
                    and "10.200.24.0/22" in a["match"]]


class TestTheAllowListDoesNotFeedItself:
    """Deleting a peered VPC left 18 allows for its prefix behind.

    `shared` was re-read from the ACLs this same pass had written, so a stale
    entry could never age out — the list was fed by its own previous output.
    Inside the tenant supernet the peering is the source now; outside it (a
    corporate network, a service on another range) there is no peering to
    consult and the existing rule is all there is.
    """

    @pytest.mark.asyncio
    async def test_an_allow_for_a_departed_vpc_is_dropped(self, monkeypatch) -> None:
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources", AsyncMock(return_value=[]))

        patched: list[dict] = []
        stale = {"action": "allow-related", "direction": "to-lport",
                 "match": "ip4.src == 10.200.52.0/22", "priority": 3101}

        async def list_obj(**kw):
            if kw["plural"] == "vpcs":
                return {"items": [{"metadata": {"name": "b3v"}, "spec": {}}]}
            return {"items": [_subnet("b3v-default", "b3v", "10.200.36.0/22",
                                      acls=[stale])]}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        acls = patched[0]["body"]["spec"]["acls"]
        assert not [a for a in acls if "10.200.52.0/22" in a["match"]
                    and a["action"] == "allow-related"]

    @pytest.mark.asyncio
    async def test_a_shared_network_outside_the_supernet_survives(
        self, monkeypatch,
    ) -> None:
        """t1/t3 reach the egress hub on 10.199.128.0/24 through exactly such a
        rule. Ageing those out would cut the tenants off the gateway."""
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources", AsyncMock(return_value=[]))

        patched: list[dict] = []
        legacy = {"action": "allow-related", "direction": "to-lport",
                  "match": "ip4.src == 10.199.128.0/24", "priority": 3100}

        async def list_obj(**kw):
            if kw["plural"] == "vpcs":
                return {"items": [{"metadata": {"name": "t1-vpc"}, "spec": {}}]}
            return {"items": [_subnet("t1-vpc-default", "t1-vpc", "10.200.8.0/22",
                                      acls=[legacy])]}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        if patched:
            acls = patched[0]["body"]["spec"]["acls"]
            assert [a for a in acls if "10.199.128.0/24" in a["match"]
                    and a["action"] == "allow-related"]
