"""A VPC created on B3 gets both halves, and the deny that must ride with them.

Two lines put a VPC on the routed external plane, and each was learned the
hard way:

  * the attachment is **declared**. `enableExternal: true` alone produced no
    external port at all on a fresh VPC — measured. An older VPC that did have
    one had it left from a previous life, which is exactly what made the flag
    look sufficient.
  * the default route into the external subnet is what puts the VPC *on* B3.
    The announcement generator reads that same fact, so the datapath and the
    announcement cannot drift: there is only one of them.

And the rule that has to ship in the same change: with the prefix routed, a
node reaches a tenant pod directly (measured — a plain curl from a node
returned 200). Before B3 "the management network cannot open connections into
a tenant" was true by accident; a VPC that gets the route without the rule is
reachable from every node for as long as the gap lasts.
"""

import pytest

from app.api.v1.subnet_acls import MGMT_DENY_PRIORITY, build_mgmt_deny_acls


class TestTheManagementDeny:
    def test_it_blocks_connections_coming_at_the_tenant(self) -> None:
        acls = build_mgmt_deny_acls(["10.198.160.0/20"])

        assert len(acls) == 1
        acl = acls[0]
        assert acl.action == "drop"
        assert acl.direction == "to-lport"
        assert acl.match == "ip4.src == 10.198.160.0/20"

    def test_a_management_plane_is_a_set_of_prefixes(self) -> None:
        """T21: one cluster's is a /10, another's a /24 — and there may be
        several. A single derived number is what made the first version hold
        by coincidence."""
        acls = build_mgmt_deny_acls(["10.198.160.0/20", "10.50.0.0/16"])

        assert {a.match for a in acls} == {
            "ip4.src == 10.198.160.0/20", "ip4.src == 10.50.0.0/16",
        }

    def test_duplicates_collapse(self) -> None:
        assert len(build_mgmt_deny_acls(["10.0.0.0/8", "10.0.0.0/8"])) == 1

    def test_it_does_not_touch_what_the_tenant_starts(self) -> None:
        """Dropping from-lport toward mgmt would be the easiest way to break
        the control-plane path, which is reached through the same egress."""
        assert all(a.direction != "from-lport" for a in build_mgmt_deny_acls(["10.0.0.0/8"]))

    def test_it_outranks_the_isolation_band(self) -> None:
        """Not an exception anybody carves out of isolation — it sits above."""
        from app.api.v1.subnet_acls import ISOLATION_PRIORITY_OWN

        assert MGMT_DENY_PRIORITY > ISOLATION_PRIORITY_OWN

    def test_without_a_known_mgmt_network_it_writes_nothing(self) -> None:
        """A drop scoped to nothing is either useless or catastrophic."""
        assert build_mgmt_deny_acls([]) == []
        assert build_mgmt_deny_acls([""]) == []


class TestWhereTheManagementPlaneIsLookedUp:
    @pytest.mark.asyncio
    async def test_the_explicit_setting_wins_and_is_a_list(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from app.api.v1.vpcs import _mgmt_deny_sources

        monkeypatch.setenv("TENANTS_MGMT_CIDR", "10.198.160.0/20, 10.50.0.0/16")

        assert await _mgmt_deny_sources(MagicMock()) == [
            "10.198.160.0/20", "10.50.0.0/16",
        ]

    @pytest.mark.asyncio
    async def test_otherwise_each_node_exactly_and_never_a_guessed_mask(
        self, monkeypatch,
    ) -> None:
        """The API reports node addresses, not the network they sit on. The
        old fallback guessed /24 from the first node and covered a /20 lab by
        coincidence; a /32 each is exact and follows the node list."""
        from unittest.mock import AsyncMock, MagicMock

        from app.api.v1.vpcs import _mgmt_deny_sources

        monkeypatch.delenv("TENANTS_MGMT_CIDR", raising=False)

        def _node(ip: str):
            n = MagicMock()
            a = MagicMock()
            a.type, a.address = "InternalIP", ip
            n.status.addresses = [a]
            return n

        result = MagicMock()
        result.items = [_node("10.198.160.4"), _node("10.198.160.1")]
        k8s = MagicMock()
        k8s.core_api.list_node = AsyncMock(return_value=result)

        assert await _mgmt_deny_sources(k8s) == [
            "10.198.160.1/32", "10.198.160.4/32",
        ]


class TestB3IsOnlyOnWhenBothHalvesAreConfigured:
    def test_a_peer_without_a_gateway_is_not_enough(self, monkeypatch) -> None:
        """They are different addresses of the same box: one is where BGP is
        spoken, the other is where packets go. A VPC pointed at the BGP
        address would default-route into a network its leg cannot reach."""
        from app.core.b3_announce import b3_enabled

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.delenv("B3_VPC_GATEWAY", raising=False)

        assert b3_enabled() is False

    def test_a_gateway_without_a_peer_is_not_enough(self, monkeypatch) -> None:
        """Routed out and never announced is the "attached but not announced"
        state — traffic leaves and nothing comes back."""
        from app.core.b3_announce import b3_enabled

        monkeypatch.delenv("B3_BGP_PEER", raising=False)
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")

        assert b3_enabled() is False

    def test_both_together(self, monkeypatch) -> None:
        from app.core.b3_announce import b3_enabled

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")

        assert b3_enabled() is True


class TestTheGeneratorAgreesWithWhatCreationWrites:
    """The two must describe the same VPC or B3 is half-applied in silence."""

    @pytest.mark.asyncio
    async def test_a_vpc_created_on_b3_is_the_one_the_generator_announces(
        self, monkeypatch,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from app.core.b3_announce import collect_announcements

        monkeypatch.setenv("B3_EXTERNAL_SUBNET", "external")

        # Exactly what creation writes: an attachment and a default route into
        # the external subnet.
        created_vpc = {
            "metadata": {"name": "newvpc"},
            "spec": {
                "extraExternalSubnets": ["cp-transit", "external"],
                "staticRoutes": [
                    {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.4.254",
                     "policy": "policyDst"},
                ],
            },
        }

        async def list_obj(**kw):
            if kw["plural"] == "vpcs":
                return {"items": [created_vpc]}
            if kw["plural"] == "ovn-eips":
                return {"items": [{
                    "metadata": {"name": "newvpc-external"},
                    "spec": {"type": "lrp", "externalSubnet": "external"},
                    "status": {"v4Ip": "10.199.4.11"},
                }]}
            return {"items": [
                {"metadata": {"name": "external"},
                 "spec": {"vpc": "ovn-cluster", "cidrBlock": "10.199.4.0/22"}},
                {"metadata": {"name": "newvpc-default"},
                 "spec": {"vpc": "newvpc", "cidrBlock": "10.200.28.0/22"}},
            ]}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)

        announced = await collect_announcements(k8s)

        assert [(a.vpc, a.cidr, a.next_hop) for a in announced] == [
            ("newvpc", "10.200.28.0/22", "10.199.4.11"),
        ]


class TestTheBaselineSurvivesTheOtherAuthor:
    """`reconcile_isolation_acls` replaces `spec.acls` wholesale.

    Measured: the management deny was written at create time, appeared in the
    created object, and was gone from the cluster within the minute — stripped
    by the reconciler, which rebuilds the list from the isolation rules alone.
    A rule present in the code, in its tests and in the API response, and
    absent where it matters. Both authors of that list carry the baseline now.
    """

    @pytest.mark.asyncio
    async def test_the_reconciler_rewrites_it_back_in(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from app.api.v1 import vpcs

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources",
                            AsyncMock(return_value=["10.198.160.0/24"]))

        patched: list[dict] = []

        async def list_obj(**kw):
            return {"items": [
                {"metadata": {"name": "a-default",
                              "labels": {"kubevirt-ui.io/managed": "true"}},
                 "spec": {"vpc": "a", "cidrBlock": "10.200.0.0/22",
                          "acls": [{"action": "drop", "direction": "from-lport",
                                    "match": "ip4.dst == 10.200.4.0/22",
                                    "priority": 3000}]}},
                {"metadata": {"name": "b-default",
                              "labels": {"kubevirt-ui.io/managed": "true"}},
                 "spec": {"vpc": "b", "cidrBlock": "10.200.4.0/22", "acls": []}},
            ]}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        assert patched, "the reconciler rewrote nothing at all"
        written = [
            a for call in patched for a in call["body"]["spec"]["acls"]
            if a["match"] == "ip4.src == 10.198.160.0/24"
        ]
        assert written, "the management deny did not survive the rewrite"
        assert all(a["direction"] == "to-lport" and a["action"] == "drop"
                   for a in written)


class TestTheIsolatedFlagStillMeansWhatTheCheckboxMeant:
    """The baseline is a drop that every VPC gets. Counted as isolation, it
    made the create response report `isolated: true` for a VPC created with the
    box unticked — one list serving two questions, which is this file's whole
    theme, this time in the field that tells the operator what they just built.
    """

    def test_the_flag_is_taken_before_the_baseline_is_appended(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vpcs.py").read_text()
        body = src[src.index("async def create_vpc("):]
        body = body[:body.index("\n@router")]

        capture = body.index("tenant_isolation = list(isolation_acls)")
        append = body.index("build_mgmt_deny_acls(")
        assert capture < append, "the baseline is already in the list being read"
        assert "isolated=bool(tenant_isolation)" in body

    def test_the_baseline_is_not_conditional_on_the_checkbox(self) -> None:
        """An un-isolated VPC is still routed, so it still needs the barrier;
        the two settings are unrelated and were never meant to be nested."""
        from pathlib import Path

        src = Path("app/api/v1/vpcs.py").read_text()
        body = src[src.index("async def create_vpc("):]
        body = body[:body.index("\n@router")]

        baseline = body.index("build_mgmt_deny_acls(")
        isolated_branch = body.index("if data.isolated:")
        # The baseline sits after the isolation branch has closed, at function
        # indentation — not inside it.
        line_start = body.rfind("\n", 0, body.rindex("if b3_enabled():", 0, baseline)) + 1
        indent = len(body[line_start:]) - len(body[line_start:].lstrip())
        assert isolated_branch < baseline
        assert indent == 4, f"the baseline is nested {indent} deep"


class TestIntentIsRecorded_NotInferred:
    """`team-a` sat open for as long as it did *because* it was open.

    The reconciler skipped any subnet with no drop rule, reading that as
    "created un-isolated". But an empty ACL list equally means the VPC predates
    isolation, or predates B3, or had its rules cleared by hand — and the one
    state that could never fix itself was the one that most needed fixing.
    Measured: a plain curl from a node into `team-a` returned 200 while the
    same call into an isolated VPC timed out.

    Inferring a choice from a state is the mirror of the T14 rule: the datapath
    is the right source for a *fact*, and a user's *decision* has to be written
    down. So it is, and silence now means "no decision recorded" — which the
    reconciler resolves by isolating, not by leaving the VPC open.
    """

    def _k8s(self, items, patched):
        from unittest.mock import AsyncMock, MagicMock

        async def list_obj(**kw):
            return {"items": items}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
        return k8s

    def _subnet(self, name, vpc, cidr, acls, *, opted_out=False):
        meta = {"name": name}
        if opted_out:
            meta["annotations"] = {"kubevirt-ui.io/isolation": "disabled"}
        return {"metadata": meta, "spec": {"vpc": vpc, "cidrBlock": cidr, "acls": acls}}

    @pytest.mark.asyncio
    async def test_a_subnet_with_no_rules_is_backfilled(self, monkeypatch) -> None:
        from app.api.v1 import vpcs

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources",
                            AsyncMock_return(["10.198.160.3/32"]))

        patched: list[dict] = []
        k8s = self._k8s([
            self._subnet("team-a-default", "team-a", "10.200.0.0/22", []),
            self._subnet("b3v-default", "b3v", "10.200.36.0/22",
                         [{"action": "drop", "direction": "to-lport",
                           "match": "ip4.src == 10.200.0.0/22", "priority": 3000}]),
        ], patched)

        await vpcs.reconcile_isolation_acls(k8s)

        team = [c for c in patched if c["name"] == "team-a-default"]
        assert team, "the subnet with no rules was skipped again"
        written = team[0]["body"]["spec"]["acls"]
        assert any(a["match"] == "ip4.src == 10.198.160.3/32"
                   and a["priority"] == 3300 for a in written), written
        assert any(a["action"] == "drop" and "10.200.36.0/22" in a["match"]
                   for a in written), "it was not isolated from its peer"

    @pytest.mark.asyncio
    async def test_the_marker_is_honoured(self, monkeypatch) -> None:
        """Opting out is legitimate — it just has to be said out loud."""
        from app.api.v1 import vpcs

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources",
                            AsyncMock_return(["10.198.160.3/32"]))

        patched: list[dict] = []
        k8s = self._k8s([
            self._subnet("open-default", "open", "10.200.0.0/22", [], opted_out=True),
            self._subnet("b3v-default", "b3v", "10.200.36.0/22", []),
        ], patched)

        await vpcs.reconcile_isolation_acls(k8s)

        assert not [c for c in patched if c["name"] == "open-default"]

    def test_creation_writes_the_marker_only_when_the_box_is_unticked(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vpcs.py").read_text()
        body = src[src.index("subnet_manifest: dict[str, Any] = {"):]
        body = body[:body.index("\n    try:")]

        assert "ISOLATION_OPT_OUT_ANNOTATION" in body
        assert "if not data.isolated else {}" in body


def AsyncMock_return(value):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)
