"""Unit tests for tenant-isolation ACLs on VPC creation.

Separate VPCs are separate routing domains, but every tenant prefix is
announced to the same upstream router and that router forwards between them —
so without ACLs a VPC is reachable from every other tenant by way of a hairpin
through the lab gateway. Isolation is a creation-time default, not something
to remember later.
"""

import pytest

from app.api.v1.subnet_acls import (
    ISOLATION_PRIORITY_DROP,
    ISOLATION_PRIORITY_OWN,
    ISOLATION_PRIORITY_SHARED,
    build_isolation_acls,
)
from app.api.v1.vpcs import _has_isolation_acls
from app.models.vpc import VpcCreateRequest

OWN = "10.198.224.0/20"
SUPERNET = "10.198.192.0/18"


def _matches(acls: list, action: str) -> set[str]:
    return {a.match for a in acls if a.action == action}


class TestBuildIsolationAcls:
    def test_own_subnet_is_allowed_both_ways(self) -> None:
        acls = build_isolation_acls(OWN, SUPERNET)
        allows = [a for a in acls if a.priority == ISOLATION_PRIORITY_OWN]

        assert {a.direction for a in allows} == {"from-lport", "to-lport"}
        assert _matches(allows, "allow-related") == {
            f"ip4.dst == {OWN}", f"ip4.src == {OWN}",
        }

    def test_catch_all_drop_covers_only_tenant_space(self) -> None:
        # Scoped to the supernet, so traffic to the internet matches nothing
        # and stays allowed. A 0.0.0.0/0 drop here would kill egress.
        acls = build_isolation_acls(OWN, SUPERNET)
        drops = [a for a in acls if a.action == "drop"]

        assert len(drops) == 2
        assert _matches(drops, "drop") == {
            f"ip4.dst == {SUPERNET}", f"ip4.src == {SUPERNET}",
        }

    def test_own_subnet_outranks_the_drop(self) -> None:
        # Own range is inside the supernet, so it only survives by priority.
        assert ISOLATION_PRIORITY_OWN > ISOLATION_PRIORITY_DROP

    def test_shared_prefixes_outrank_the_drop(self) -> None:
        assert ISOLATION_PRIORITY_SHARED > ISOLATION_PRIORITY_DROP

    def test_shared_cidrs_are_allowed(self) -> None:
        acls = build_isolation_acls(OWN, SUPERNET, ["10.198.192.0/24"])
        shared = [a for a in acls if a.priority == ISOLATION_PRIORITY_SHARED]

        assert _matches(shared, "allow-related") == {
            "ip4.dst == 10.198.192.0/24", "ip4.src == 10.198.192.0/24",
        }

    def test_each_shared_prefix_gets_its_own_priority(self) -> None:
        acls = build_isolation_acls(
            OWN, SUPERNET, ["10.198.192.0/24", "10.198.193.0/24"],
        )
        priorities = {
            a.priority for a in acls
            if ISOLATION_PRIORITY_DROP < a.priority < ISOLATION_PRIORITY_OWN
        }
        assert priorities == {ISOLATION_PRIORITY_SHARED, ISOLATION_PRIORITY_SHARED + 1}

    def test_empty_shared_entries_are_skipped(self) -> None:
        acls = build_isolation_acls(OWN, SUPERNET, ["", "10.198.192.0/24"])
        assert len([a for a in acls if a.priority == ISOLATION_PRIORITY_SHARED]) == 2

    def test_no_supernet_yields_no_rules(self) -> None:
        # A drop with nothing to scope it to would blackhole the internet, so
        # write nothing rather than something half-formed.
        assert build_isolation_acls(OWN, "") == []

    def test_no_subnet_cidr_yields_no_rules(self) -> None:
        assert build_isolation_acls("", SUPERNET) == []

    def test_rule_set_matches_the_verified_recipe(self) -> None:
        # Shape verified on the live lab (ceph-lab/vpc-bgp/make-vpc.sh):
        # 2 own + 2 per shared + 2 drop.
        acls = build_isolation_acls(OWN, SUPERNET, ["10.198.192.0/24"])
        assert len(acls) == 6
        assert all(a.action in ("allow-related", "drop") for a in acls)


class TestHasIsolationAcls:
    def test_detects_the_drop(self) -> None:
        spec = {"acls": [
            {"action": "drop", "priority": ISOLATION_PRIORITY_DROP,
             "direction": "from-lport", "match": f"ip4.dst == {SUPERNET}"},
        ]}
        assert _has_isolation_acls(spec) is True

    def test_allows_alone_are_not_isolation(self) -> None:
        # The drop is what isolates; the allows only carve exceptions from it.
        spec = {"acls": [
            {"action": "allow-related", "priority": ISOLATION_PRIORITY_OWN,
             "direction": "from-lport", "match": f"ip4.dst == {OWN}"},
        ]}
        assert _has_isolation_acls(spec) is False

    def test_unrelated_drop_at_another_priority_does_not_count(self) -> None:
        spec = {"acls": [
            {"action": "drop", "priority": 2900,
             "direction": "from-lport", "match": "ip4.dst == 192.168.0.0/16"},
        ]}
        assert _has_isolation_acls(spec) is False

    def test_missing_and_null_acls(self) -> None:
        assert _has_isolation_acls({}) is False
        assert _has_isolation_acls({"acls": None}) is False


class TestCreateRequestDefaults:
    def test_isolated_defaults_on(self) -> None:
        # The whole point: a VPC created without thinking about it is closed.
        assert VpcCreateRequest(name="t1").isolated is True

    def test_isolation_can_be_declined(self) -> None:
        assert VpcCreateRequest(name="t1", isolated=False).isolated is False

    def test_shared_cidrs_default_empty(self) -> None:
        assert VpcCreateRequest(name="t1").shared_cidrs == []
