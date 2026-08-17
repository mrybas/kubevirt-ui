"""The generated FRR config, against the form proved on the lab.

Three lines in it were each measured, and each fails *silently* when missing —
the session comes up, the CR looks healthy, and nothing is announced. So this
is a fixture test on the rendered text, not a test of intentions:

  * `no bgp ebgp-requires-policy` — without it FRR advertises nothing to an
    eBGP peer and the only clue is `(Policy)` in `show bgp ipv4 summary`;
  * `no bgp network import-check` — a `network` statement for a prefix that is
    not in the node's RIB (a tenant /22 never is) is ignored;
  * the next hop is honoured on the outbound **neighbor** route-map only.
    On `network <cidr> route-map X` the advertised next hop came out as
    `0.0.0.0` — measured — which the border resolves to the node itself, so
    the announcement looks accepted and points at the wrong place.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.core.b3_announce import (
    Announcement,
    announce_replicas,
    build_frr_configuration,
    collect_announcements,
    pick_announce_nodes,
    reload_failures,
    render_raw_config,
)

TEAM_A = Announcement(vpc="team-a", cidr="10.200.0.0/22", next_hop="10.199.4.1")
T8V = Announcement(vpc="t8v", cidr="10.200.24.0/22", next_hop="10.199.4.9")

PEER = "10.198.175.254"


def _render(*ann: Announcement) -> str:
    return render_raw_config(list(ann), peer=PEER, asn=65030, remote_asn=65000)


class TestTheThreeSilentRequirements:
    def test_ebgp_requires_policy_is_disabled(self) -> None:
        assert "no bgp ebgp-requires-policy" in _render(TEAM_A)

    def test_network_import_check_is_disabled(self) -> None:
        assert "no bgp network import-check" in _render(TEAM_A)

    def test_the_next_hop_is_set_on_the_outbound_neighbor_route_map(self) -> None:
        out = _render(TEAM_A)

        assert f"neighbor {PEER} route-map B3-NH out" in out
        assert "set ip next-hop 10.199.4.1" in out

    def test_the_next_hop_is_NOT_attached_to_the_network_statement(self) -> None:
        """That form advertises 0.0.0.0 — the node, not the tenant's router."""
        assert "network 10.200.0.0/22 route-map" not in _render(TEAM_A)


class TestTwoVpcsOneRouter:
    """Measured live: both prefixes on one session, different next hops."""

    def test_each_vpc_gets_its_own_prefix_list_branch(self) -> None:
        out = _render(TEAM_A, T8V)

        assert "ip prefix-list PL-TEAM-A seq 5 permit 10.200.0.0/22" in out
        assert "ip prefix-list PL-T8V seq 5 permit 10.200.24.0/22" in out
        assert "set ip next-hop 10.199.4.1" in out
        assert "set ip next-hop 10.199.4.9" in out

    def test_the_branches_have_distinct_sequence_numbers(self) -> None:
        out = _render(TEAM_A, T8V)

        assert "route-map B3-NH permit 10" in out
        assert "route-map B3-NH permit 20" in out

    def test_both_prefixes_are_advertised(self) -> None:
        out = _render(TEAM_A, T8V)

        assert "  network 10.200.0.0/22" in out
        assert "  network 10.200.24.0/22" in out


class TestTheRenderIsStable:
    def test_input_order_does_not_change_the_output(self) -> None:
        """A CR that churns on every reconcile reloads FRR for nothing."""
        assert _render(TEAM_A, T8V) == _render(T8V, TEAM_A)

    def test_no_announcements_yields_no_route_map_or_neighbor_policy(self) -> None:
        """An empty set must not leave a dangling outbound policy behind."""
        out = _render()

        assert "route-map B3-NH" not in out
        # `network <cidr>` statements — not the `no bgp network import-check` line.
        assert "  network " not in out
        assert "router bgp 65030" in out


class TestTheCustomResource:
    def test_it_pins_a_fixed_set_of_nodes(self) -> None:
        cr = build_frr_configuration(
            [TEAM_A], ["kubevirt-lab-worker-2", "kubevirt-lab-worker-1"],
            peer=PEER, asn=65030, remote_asn=65000, namespace="o0-metallb",
        )

        expr = cr["spec"]["nodeSelector"]["matchExpressions"][0]
        assert expr["key"] == "kubernetes.io/hostname"
        assert expr["operator"] == "In"
        assert expr["values"] == ["kubevirt-lab-worker-1", "kubevirt-lab-worker-2"]

    def test_more_than_one_node_is_redundancy_not_ecmp(self) -> None:
        """Both nodes advertise the same prefix with the same next hop, so
        there is nothing to split — only the announcement is duplicated."""
        cr = build_frr_configuration(
            [TEAM_A], ["a", "b"],
            peer=PEER, asn=65030, remote_asn=65000, namespace="o0-metallb",
        )

        raw = cr["spec"]["raw"]["rawConfig"]
        assert raw.count("set ip next-hop 10.199.4.1") == 1


EXTERNAL_SUBNET = {"metadata": {"name": "external"},
                   "spec": {"vpc": "ovn-cluster", "cidrBlock": "10.199.4.0/22"}}


def _vpc(name: str, next_hop: str | None):
    """A VPC CR; `next_hop` is where its default route points, if anywhere."""
    routes = [{"cidr": "0.0.0.0/0", "nextHopIP": next_hop}] if next_hop else []
    return {"metadata": {"name": name}, "spec": {"staticRoutes": routes}}


class TestWhatGetsAnnounced:
    def _k8s(self, eips: list[dict], subnets: list[dict],
             vpcs: list[dict] | None = None) -> MagicMock:
        # Default: every VPC mentioned in `subnets` is routed via the border,
        # so the older cases keep testing what they were written to test.
        if vpcs is None:
            names = {s["spec"]["vpc"] for s in subnets}
            vpcs = [_vpc(n, "10.199.4.254") for n in names]

        async def list_obj(**kw):
            if kw["plural"] == "ovn-eips":
                return {"items": eips}
            if kw["plural"] == "vpcs":
                return {"items": vpcs}
            return {"items": [*subnets, EXTERNAL_SUBNET]}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        return k8s

    @pytest.mark.asyncio
    async def test_a_hub_tenant_is_NOT_announced(self) -> None:
        """The defect this rule exists for, caught on the first live run.

        A tenant on the VEG hub also has an external leg, but its default route
        points at the gateway's transit address and its traffic leaves SNAT'd
        from a gateway pod. Announcing its /22 here too would put a second,
        competing path to a working tenant on the border.
        """
        k8s = self._k8s(
            [self._eip("t1-vpc-external", "10.199.4.8")],
            [self._subnet("t1-vpc-default", "t1-vpc", "10.200.8.0/22")],
            vpcs=[_vpc("t1-vpc", "10.199.129.1")],   # the hub's transit, not external
        )

        assert await collect_announcements(k8s) == []

    @pytest.mark.asyncio
    async def test_a_vpc_with_no_default_route_is_NOT_announced(self) -> None:
        """An egress gateway's own VPC has a leg and no business being here."""
        k8s = self._k8s(
            [self._eip("egw-shared-egress-external", "10.199.4.4")],
            [self._subnet("egw-sub", "egw-shared-egress", "10.199.128.0/24")],
            vpcs=[_vpc("egw-shared-egress", None)],
        )

        assert await collect_announcements(k8s) == []

    def _eip(self, name: str, ip: str, subnet: str = "external", type_: str = "lrp"):
        return {"metadata": {"name": name},
                "spec": {"type": type_, "externalSubnet": subnet},
                "status": {"v4Ip": ip}}

    def _subnet(self, name: str, vpc: str, cidr: str):
        return {"metadata": {"name": name}, "spec": {"vpc": vpc, "cidrBlock": cidr}}

    @pytest.mark.asyncio
    async def test_a_vpc_with_an_external_leg_is_announced(self) -> None:
        k8s = self._k8s(
            [self._eip("t8v-external", "10.199.4.9")],
            [self._subnet("t8v-default", "t8v", "10.200.24.0/22")],
        )

        assert await collect_announcements(k8s) == [
            Announcement("t8v", "10.200.24.0/22", "10.199.4.9"),
        ]

    @pytest.mark.asyncio
    async def test_a_vpc_without_a_leg_is_not_announced(self) -> None:
        """There would be no next hop — the border would learn a black hole."""
        k8s = self._k8s([], [self._subnet("t8v-default", "t8v", "10.200.24.0/22")])

        assert await collect_announcements(k8s) == []

    @pytest.mark.asyncio
    async def test_a_nat_eip_is_not_mistaken_for_a_router_leg(self) -> None:
        """`type: nat` addresses belong to SNAT rules, not to the router."""
        k8s = self._k8s(
            [self._eip("eip-t2-vpc", "10.199.4.5", type_="nat")],
            [self._subnet("t2-vpc-default", "t2-vpc", "10.200.12.0/22")],
        )

        assert await collect_announcements(k8s) == []

    @pytest.mark.asyncio
    async def test_a_leg_on_the_transit_network_is_not_the_external_one(self) -> None:
        k8s = self._k8s(
            [self._eip("t8v-cp-transit", "10.199.1.24", subnet="cp-transit")],
            [self._subnet("t8v-default", "t8v", "10.200.24.0/22")],
        )

        assert await collect_announcements(k8s) == []

    @pytest.mark.asyncio
    async def test_every_subnet_of_a_vpc_is_announced(self) -> None:
        k8s = self._k8s(
            [self._eip("t8v-external", "10.199.4.9")],
            [self._subnet("t8v-default", "t8v", "10.200.24.0/22"),
             self._subnet("t8v-extra", "t8v", "10.200.28.0/22")],
        )

        assert {a.cidr for a in await collect_announcements(k8s)} == {
            "10.200.24.0/22", "10.200.28.0/22",
        }


class TestReloadFailuresAreTheCheapInvariant:
    """FRR keeps the old config when a reload fails, so live traffic survives
    — and the new VPC silently is not added. That is the "attached but not
    announced" state, and this is how it becomes visible without a vtysh exec.
    """

    def _k8s(self, states: dict[str, dict]) -> MagicMock:
        async def get_obj(**kw):
            if kw["name"] not in states:
                raise ApiException(status=404)
            return {"status": states[kw["name"]]}

        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
        return k8s

    @pytest.mark.asyncio
    async def test_a_healthy_node_reports_nothing(self) -> None:
        k8s = self._k8s({"n1": {"lastConversionResult": "success",
                                "lastReloadResult": "success"}})

        assert await reload_failures(k8s, ["n1"]) == {}

    @pytest.mark.asyncio
    async def test_a_rejected_config_is_reported_with_frrs_own_words(self) -> None:
        """Verbatim shape from the lab: the error names the offending line."""
        k8s = self._k8s({"n1": {
            "lastConversionResult": "success",
            "lastReloadResult": (
                "ERROR: vtysh failed to process new configuration: "
                "line 9: % Unknown command:  this-is-not-a-valid-frr-directive\n"
                "more noise"
            ),
        }})

        failures = await reload_failures(k8s, ["n1"])

        assert "n1" in failures
        assert "Unknown command" in failures["n1"]

    @pytest.mark.asyncio
    async def test_a_missing_node_state_is_not_a_failure(self) -> None:
        """Absence is not evidence — the lesson from reading a status CR too
        soon after applying it."""
        assert await reload_failures(self._k8s({}), ["n1"]) == {}


class TestNodeSelection:
    @pytest.mark.asyncio
    async def test_only_ready_nodes_and_deterministically(self) -> None:
        def _node(name: str, ready: str):
            n = MagicMock()
            n.metadata.name = name
            cond = MagicMock()
            cond.type, cond.status = "Ready", ready
            n.status.conditions = [cond]
            return n

        k8s = MagicMock()
        result = MagicMock()
        result.items = [_node("w3", "True"), _node("w1", "True"), _node("w2", "False")]
        k8s.core_api.list_node = AsyncMock(return_value=result)

        assert await pick_announce_nodes(k8s, 2) == ["w1", "w3"]

    @pytest.mark.asyncio
    async def test_control_plane_nodes_never_announce(self) -> None:
        """Measured defect: plain sorted() over all nodes put cp-1/cp-2 first,
        the border peers with workers, and every B3 prefix silently vanished
        from the border while the generated CR looked perfect."""
        def _node(name: str, cp: bool):
            n = MagicMock()
            n.metadata.name = name
            n.metadata.labels = ({"node-role.kubernetes.io/control-plane": ""}
                                 if cp else {})
            c = MagicMock()
            c.type, c.status = "Ready", "True"
            n.status.conditions = [c]
            return n

        k8s = MagicMock()
        result = MagicMock()
        result.items = [_node("cp-1", True), _node("cp-2", True),
                        _node("worker-1", False), _node("worker-2", False)]
        k8s.core_api.list_node = AsyncMock(return_value=result)

        assert await pick_announce_nodes(k8s, 2) == ["worker-1", "worker-2"]

    @pytest.mark.asyncio
    async def test_an_explicit_list_wins(self, monkeypatch) -> None:
        """The border may peer with a narrower set than "all workers"."""
        monkeypatch.setenv("B3_ANNOUNCE_NODE_LIST", "worker-2")
        k8s = MagicMock()
        k8s.core_api.list_node = AsyncMock()

        assert await pick_announce_nodes(k8s, 2) == ["worker-2"]

    def test_two_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("B3_ANNOUNCE_NODES", raising=False)

        assert announce_replicas() == 2

    def test_never_zero(self, monkeypatch) -> None:
        """Zero nodes means nothing announces — an outage, not a setting."""
        monkeypatch.setenv("B3_ANNOUNCE_NODES", "0")

        assert announce_replicas() == 1


class TestTheReconcilerWritesOnlyWhenItMust:
    """A patch per pass reloads FRR for nothing, and a reload is the one
    moment a session can flap. So an unchanged input must be a no-op.
    """

    def _k8s(self, existing: dict | None, eips, subnets, nodes=("w1", "w2")):
        state = {"created": [], "patched": []}

        async def get_ns(**kw):
            if existing is None:
                raise ApiException(status=404)
            return existing

        async def create_ns(**kw):
            state["created"].append(kw["body"])
            return kw["body"]

        async def patch_ns(**kw):
            state["patched"].append(kw["body"])
            return kw["body"]

        async def list_obj(**kw):
            if kw["plural"] == "ovn-eips":
                return {"items": eips}
            if kw["plural"] == "vpcs":
                return {"items": [_vpc(s["spec"]["vpc"], "10.199.4.254")
                                  for s in subnets]}
            return {"items": [*subnets, EXTERNAL_SUBNET]}

        async def get_cluster(**kw):
            return {"status": {"lastConversionResult": "success",
                               "lastReloadResult": "success"}}

        def _node(name):
            n = MagicMock()
            n.metadata.name = name
            n.metadata.labels = {"node-role.kubernetes.io/worker": ""}
            c = MagicMock()
            c.type, c.status = "Ready", "True"
            n.status.conditions = [c]
            return n

        node_list = MagicMock()
        node_list.items = [_node(n) for n in nodes]

        k8s = MagicMock()
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get_ns)
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(side_effect=create_ns)
        k8s.custom_api.patch_namespaced_custom_object = AsyncMock(side_effect=patch_ns)
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_cluster)
        k8s.core_api.list_node = AsyncMock(return_value=node_list)
        k8s._state = state
        return k8s

    _EIPS = [{"metadata": {"name": "t8v-external"},
              "spec": {"type": "lrp", "externalSubnet": "external"},
              "status": {"v4Ip": "10.199.4.9"}}]
    _SUBNETS = [{"metadata": {"name": "t8v-default"},
                 "spec": {"vpc": "t8v", "cidrBlock": "10.200.24.0/22"}}]

    @pytest.mark.asyncio
    async def test_it_creates_the_configuration_when_absent(self, monkeypatch) -> None:
        monkeypatch.setenv("B3_BGP_PEER", PEER)
        k8s = self._k8s(None, self._EIPS, self._SUBNETS)

        report = await __import__(
            "app.core.b3_announce", fromlist=["ensure_announcements"],
        ).ensure_announcements(k8s)

        assert len(k8s._state["created"]) == 1
        assert report["announced"] == [("t8v", "10.200.24.0/22", "10.199.4.9")]

    @pytest.mark.asyncio
    async def test_an_unchanged_cluster_is_a_no_op(self, monkeypatch) -> None:
        from app.core.b3_announce import build_frr_configuration, ensure_announcements

        monkeypatch.setenv("B3_BGP_PEER", PEER)
        desired = build_frr_configuration(
            [T8V], ["w1", "w2"], peer=PEER, asn=65030, remote_asn=65000,
            namespace="o0-metallb",
        )
        k8s = self._k8s(desired, self._EIPS, self._SUBNETS)

        await ensure_announcements(k8s)

        assert k8s._state["patched"] == [], "an identical spec must not be rewritten"
        assert k8s._state["created"] == []

    @pytest.mark.asyncio
    async def test_a_new_vpc_updates_it(self, monkeypatch) -> None:
        from app.core.b3_announce import build_frr_configuration, ensure_announcements

        monkeypatch.setenv("B3_BGP_PEER", PEER)
        stale = build_frr_configuration(
            [], ["w1", "w2"], peer=PEER, asn=65030, remote_asn=65000,
            namespace="o0-metallb",
        )
        k8s = self._k8s(stale, self._EIPS, self._SUBNETS)

        await ensure_announcements(k8s)

        assert len(k8s._state["patched"]) == 1
        assert "10.200.24.0/22" in k8s._state["patched"][0]["spec"]["raw"]["rawConfig"]

    @pytest.mark.asyncio
    async def test_without_a_configured_peer_it_does_nothing(self, monkeypatch) -> None:
        """No peer means no B3 on this deployment — not an error."""
        from app.core.b3_announce import ensure_announcements

        monkeypatch.delenv("B3_BGP_PEER", raising=False)
        k8s = self._k8s(None, self._EIPS, self._SUBNETS)

        report = await ensure_announcements(k8s)

        assert "skipped" in report
        assert k8s._state["created"] == []

    @pytest.mark.asyncio
    async def test_with_no_ready_nodes_it_refuses_to_write(self, monkeypatch) -> None:
        """Pinning an empty node set would withdraw every announcement."""
        from app.core.b3_announce import ensure_announcements

        monkeypatch.setenv("B3_BGP_PEER", PEER)
        k8s = self._k8s(None, self._EIPS, self._SUBNETS, nodes=())

        report = await ensure_announcements(k8s)

        assert "skipped" in report
        assert k8s._state["created"] == []
