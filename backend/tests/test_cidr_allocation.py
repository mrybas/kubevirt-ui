"""Unit tests for VPC CIDR allocation and overlap validation.

Overlapping VPC ranges break three things at once — peering static routes go
ambiguous, the isolation ACLs are written in terms of CIDRs, and each BGP
gateway derives its router-id from its internal address, so two VPCs on one
range give two speakers the same id.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

from app.core import allocators
from app.core.allocators import (
    vpc_prefix_len,
    vpc_supernet,
    allocate_vpc_cidr,
    assert_cidr_free,
    find_cidr_conflicts,
    list_subnet_cidrs,
)


def _k8s(subnet_cidrs: list[tuple[str, str]], next_index: str = "0") -> MagicMock:
    """Mock with the given subnets defined and the counter at next_index."""
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={
        "items": [
            {"metadata": {"name": name}, "spec": {"cidrBlock": cidr}}
            for name, cidr in subnet_cidrs
        ],
    })
    cm = MagicMock()
    cm.data = {"next_index": next_index}
    cm.metadata.resource_version = "42"
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.replace_namespaced_config_map = AsyncMock()
    return k8s


class TestFindCidrConflicts:
    def test_no_conflict_for_disjoint_ranges(self) -> None:
        assert find_cidr_conflicts("10.203.0.0/24", [("a", "10.204.0.0/24")]) == []

    def test_exact_match_conflicts(self) -> None:
        assert find_cidr_conflicts("10.203.0.0/24", [("a", "10.203.0.0/24")]) == [
            "a (10.203.0.0/24)",
        ]

    def test_containment_conflicts_either_way(self) -> None:
        # Ours inside theirs...
        assert find_cidr_conflicts("10.203.0.0/24", [("big", "10.203.0.0/16")])
        # ...and theirs inside ours.
        assert find_cidr_conflicts("10.203.0.0/16", [("small", "10.203.7.0/24")])

    def test_partial_overlap_conflicts(self) -> None:
        assert find_cidr_conflicts("10.203.0.0/23", [("a", "10.203.1.0/24")])

    def test_reports_every_conflict(self) -> None:
        conflicts = find_cidr_conflicts("10.203.0.0/16", [
            ("a", "10.203.1.0/24"), ("b", "10.203.2.0/24"), ("c", "10.204.0.0/24"),
        ])
        assert len(conflicts) == 2

    def test_ipv6_is_not_compared_against_ipv4(self) -> None:
        assert find_cidr_conflicts("10.203.0.0/24", [("v6", "fd00::/64")]) == []

    def test_malformed_entries_are_skipped(self) -> None:
        assert find_cidr_conflicts("10.203.0.0/24", [("junk", "not-a-cidr")]) == []

    def test_malformed_input_conflicts_with_nothing(self) -> None:
        assert find_cidr_conflicts("not-a-cidr", [("a", "10.203.0.0/24")]) == []


@pytest.mark.asyncio
class TestListSubnetCidrs:
    async def test_splits_dual_stack_entries(self) -> None:
        k8s = _k8s([("ds", "10.203.0.0/24,fd00::/64")])
        assert await list_subnet_cidrs(k8s) == [
            ("ds", "10.203.0.0/24"), ("ds", "fd00::/64"),
        ]

    async def test_returns_empty_when_listing_fails(self) -> None:
        # A conflict check that cannot list must not block VPC creation.
        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden"),
        )
        assert await list_subnet_cidrs(k8s) == []


@pytest.mark.asyncio
class TestAssertCidrFree:
    async def test_passes_when_free(self) -> None:
        await assert_cidr_free(_k8s([("a", "10.204.0.0/24")]), "10.203.0.0/24")

    async def test_409_with_the_offender_named(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await assert_cidr_free(_k8s([("taken", "10.203.0.0/24")]), "10.203.0.0/24")

        assert exc.value.status_code == 409
        assert "taken" in exc.value.detail


@pytest.mark.asyncio
class TestAllocateVpcCidr:
    """Allocation comes out of TENANT_SUPERNET at TENANT_VPC_PREFIX.

    Both used to be constants in this module (`10.{200+N}.0.0/24`) that no
    configuration could reach, so the supernet the isolation ACLs are scoped
    to and the prefix the border router accepts were free to disagree with the
    allocator — and did.
    """

    def _nth(self, n: int) -> str:
        import ipaddress
        nets = ipaddress.ip_network(vpc_supernet()).subnets(new_prefix=vpc_prefix_len())
        return str(next(x for i, x in enumerate(nets) if i == n))

    async def test_allocates_the_first_free_range(self) -> None:
        cidr, gateway = await allocate_vpc_cidr(_k8s([], next_index="0"))
        assert cidr == self._nth(0)
        assert gateway == self._nth(0).split("/")[0][:-1] + "1"

    async def test_skips_a_range_taken_by_hand(self) -> None:
        # Somebody created a VPC with an explicit subnet_cidr out of this same
        # pool; the counter never saw it, so without the scan the allocator
        # would hand out a duplicate.
        k8s = _k8s([("manual", self._nth(0))], next_index="0")
        cidr, _ = await allocate_vpc_cidr(k8s)
        assert cidr == self._nth(1)

    async def test_skips_a_run_of_taken_ranges(self) -> None:
        taken = [(f"m{i}", self._nth(i)) for i in range(3)]
        cidr, _ = await allocate_vpc_cidr(_k8s(taken, next_index="0"))
        assert cidr == self._nth(3)

    async def test_counter_advances_past_the_skipped_ones(self) -> None:
        k8s = _k8s([("manual", self._nth(0))], next_index="0")
        await allocate_vpc_cidr(k8s)

        body = k8s.core_api.replace_namespaced_config_map.await_args.kwargs["body"]
        assert body.data == {"next_index": "2"}

    async def test_a_full_supernet_is_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_SUPERNET", "10.200.0.0/22")
        monkeypatch.setenv("TENANT_VPC_PREFIX", "22")
        with pytest.raises(HTTPException) as exc:
            await allocate_vpc_cidr(_k8s([("only-one", "10.200.0.0/22")], next_index="0"))
        assert exc.value.status_code == 409
        assert "supernet" in exc.value.detail.lower()

    async def test_retries_on_optimistic_lock_conflict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(allocators.asyncio, "sleep", AsyncMock())
        k8s = _k8s([], next_index="0")
        k8s.core_api.replace_namespaced_config_map = AsyncMock(side_effect=[
            ApiException(status=409, reason="Conflict"), None,
        ])

        cidr, _ = await allocate_vpc_cidr(k8s)

        assert cidr == self._nth(0)
        assert k8s.core_api.replace_namespaced_config_map.await_count == 2
