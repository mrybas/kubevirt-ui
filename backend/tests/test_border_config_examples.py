"""The page tells you what to put on the upstream router. It had better be right.

The examples used to be generated from the kube-ovn-speaker DaemonSet and were
hidden unless it was deployed — so on a stand that peers without the speaker
(every stand with per-tenant VPCs) the one screen that explains the other half
of the session showed nothing at all.

They are generated from the routed plane now, and from the *same* settings that
allocate the VPCs: a prefix this cluster hands out is a prefix the generated
filter accepts. Those two disagreeing has already happened once — the filter
took `10.200.0.0/14{22,22}` while a VPC was carved as a /24, and the
announcement was rejected by a router nobody thought to look at.

The BIRD output was checked with `bird -p` on the live border: PARSE-OK.
"""

import pytest

from app.core.b3_announce import render_border_bird, render_border_frr


ARGS = dict(supernet="10.200.0.0/14", prefix_len=22, leg_cidr="10.199.4.0/22",
            local=65030, remote=65000)


class TestTheFilterFollowsTheAllocator:
    def test_bird_accepts_exactly_what_this_cluster_hands_out(self) -> None:
        cfg = render_border_bird(node_cidr="10.198.160.0/20", **ARGS)

        assert "if net ~ [ 10.200.0.0/14{22,22} ] then accept;" in cfg
        assert "reject;" in cfg

    def test_a_different_prefix_length_moves_the_filter_with_it(self) -> None:
        cfg = render_border_bird(
            node_cidr="10.198.160.0/20",
            **{**ARGS, "supernet": "10.64.0.0/12", "prefix_len": 24},
        )

        assert "10.64.0.0/12{24,24}" in cfg

    def test_frr_says_the_same_thing_in_its_own_words(self) -> None:
        cfg = render_border_frr(node_cidr="10.198.160.0/20", **ARGS)

        assert "ip prefix-list tenants seq 10 permit 10.200.0.0/14 ge 22 le 22" in cfg


class TestWhatTheRouterWouldOtherwiseGetWrong:
    def test_it_names_the_network_the_next_hops_live_in(self) -> None:
        """The next hop is a VPC leg, not the peer address. A router without an
        interface in that network installs every accepted route as unreachable —
        the session looks perfect and nothing works."""
        for cfg in (render_border_bird(node_cidr="10.198.160.0/20", **ARGS),
                    render_border_frr(node_cidr="10.198.160.0/20", **ARGS)):
            assert "10.199.4.0/22" in cfg
            assert "third-party next hop" in cfg or "not the peer address" in cfg

    def test_graceful_restart_is_off_and_says_why(self) -> None:
        """Measured: with it on, BIRD kept a dead session's routes and built
        ECMP across a live and a dead next hop."""
        cfg = render_border_bird(node_cidr="10.198.160.0/20", **ARGS)

        assert "graceful restart off;" in cfg
        assert "dead next hop" in cfg

    def test_multipath_is_asked_for_on_both(self) -> None:
        """Several nodes announce the same prefix; one installed path makes the
        redundancy decorative."""
        assert "merge paths on;" in render_border_bird(node_cidr="10.198.160.0/20", **ARGS)
        assert "maximum-paths" in render_border_frr(node_cidr="10.198.160.0/20", **ARGS)


class TestASingleAnnouncerIsNotHidden:
    def test_a_slash_32_range_is_called_out(self) -> None:
        """The config is complete and correct with one node in the range — and
        that node is then a single point of failure for every tenant. It is the
        current state of the lab, so the generated file has to say so rather
        than let it read as finished."""
        cfg = render_border_bird(node_cidr="10.198.160.3/32", **ARGS)

        assert "single node" in cfg
        assert "10.198.160.3/32" in cfg

    def test_a_real_range_carries_no_such_note(self) -> None:
        cfg = render_border_bird(node_cidr="10.198.160.0/20", **ARGS)

        assert "single node" not in cfg

    def test_the_frr_note_is_commented_the_frr_way(self) -> None:
        """A `#` inside an FRR config is not a comment — pasting it breaks the
        file at the one line meant to be a warning."""
        cfg = render_border_frr(node_cidr="10.198.160.3/32", **ARGS)

        note = [ln for ln in cfg.splitlines() if "single node" in ln]
        assert note and all(ln.lstrip().startswith("!") for ln in note)


class TestTheNodeRangeIsDerivedFromTheNodesThatAnnounce:
    @pytest.mark.asyncio
    async def test_several_nodes_collapse_to_the_smallest_covering_prefix(
        self, monkeypatch,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from app.core import b3_announce

        monkeypatch.setattr(b3_announce, "pick_announce_nodes",
                            AsyncMock(return_value=["a", "b"]))

        def _node(ip: str):
            n = MagicMock()
            addr = MagicMock()
            addr.type, addr.address = "InternalIP", ip
            n.status.addresses = [addr]
            return n

        k8s = MagicMock()
        k8s.core_api.read_node = AsyncMock(
            side_effect=[_node("10.198.160.3"), _node("10.198.160.4")])

        assert await b3_announce.node_peer_range(k8s) == "10.198.160.0/29"

    @pytest.mark.asyncio
    async def test_one_node_yields_a_slash_32_rather_than_a_guessed_subnet(
        self, monkeypatch,
    ) -> None:
        """Widening it here would silently authorise every node in the subnet
        to announce tenant prefixes. The /32 is the truth, and the generated
        config carries the warning instead."""
        from unittest.mock import AsyncMock, MagicMock

        from app.core import b3_announce

        monkeypatch.setattr(b3_announce, "pick_announce_nodes",
                            AsyncMock(return_value=["a"]))
        node = MagicMock()
        addr = MagicMock()
        addr.type, addr.address = "InternalIP", "10.198.160.3"
        node.status.addresses = [addr]
        k8s = MagicMock()
        k8s.core_api.read_node = AsyncMock(return_value=node)

        assert await b3_announce.node_peer_range(k8s) == "10.198.160.3/32"
