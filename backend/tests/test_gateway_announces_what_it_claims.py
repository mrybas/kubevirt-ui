"""Attached is not announced, and only one of the two carries traffic.

The chain is longer than the spec suggests, and its last link is a pod:

    spec.policies (ipBlocks)
      → kube-ovn renders ovn.kubernetes.io/routes on the pod template
        → the CNI writes kernel routes into the pod at creation
          → FRR redistributes kernel — there are no `network` statements
            → the border learns the tenant /22

Routes are written when a pod is created, so a policy change reaches BGP only
after the pods roll. In between, the gateway is configured for a tenant it is
not announcing: the SYN leaves and nothing comes back.

Measured on the lab: policies, address set, peering legs and router ports all
verified complete while a freshly attached VPC had no internet. Every check
that looked at the spec passed. This one looks at the pod.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import _announcement_lag, _unannounced_cidrs
from app.models.egress_gateway import AttachedVpcInfo

GW = "shared-egress"


def _vpc(name: str, cidr: str) -> AttachedVpcInfo:
    return AttachedVpcInfo(
        vpc_name=name, subnet_name=f"{name}-default", cidr=cidr,
        transit_ip="", peering_name="", peering_ok=True,
    )


def _pod(name: str, dsts: list[str], phase: str = "Running"):
    p = MagicMock()
    p.metadata.name = name
    p.metadata.annotations = {
        "ovn.kubernetes.io/routes": __import__("json").dumps(
            [{"dst": d, "gw": "10.199.128.1"} for d in dsts]
        )
    }
    p.status.phase = phase
    return p


def _k8s(pods: list) -> MagicMock:
    async def list_pods(**kw):
        r = MagicMock()
        r.items = pods
        return r

    k8s = MagicMock()
    k8s.core_api.list_namespaced_pod = AsyncMock(side_effect=list_pods)
    return k8s


@pytest.mark.asyncio
async def test_a_freshly_attached_cidr_is_reported_as_not_announced() -> None:
    k8s = _k8s([_pod("gw-1", ["10.200.0.0/22"]), _pod("gw-2", ["10.200.0.0/22"])])

    missing = await _unannounced_cidrs(
        k8s, GW, [_vpc("team-a", "10.200.0.0/22"), _vpc("t1-vpc", "10.200.8.0/22")],
    )

    assert missing == ["10.200.8.0/22"]
    reason = _announcement_lag(missing)
    assert "10.200.8.0/22" in reason and "cannot come back" in reason


@pytest.mark.asyncio
async def test_everything_announced_is_not_a_complaint() -> None:
    k8s = _k8s([_pod("gw-1", ["10.200.0.0/22", "10.200.8.0/22"]),
                _pod("gw-2", ["10.200.0.0/22", "10.200.8.0/22"])])

    missing = await _unannounced_cidrs(
        k8s, GW, [_vpc("team-a", "10.200.0.0/22"), _vpc("t1-vpc", "10.200.8.0/22")],
    )

    assert missing == []
    assert _announcement_lag(missing) is None


@pytest.mark.asyncio
async def test_a_cidr_only_half_the_pods_carry_counts_as_missing() -> None:
    """Mid-roll is exactly the window this exists to show."""
    k8s = _k8s([_pod("gw-old", ["10.200.0.0/22"]),
                _pod("gw-new", ["10.200.0.0/22", "10.200.8.0/22"])])

    missing = await _unannounced_cidrs(
        k8s, GW, [_vpc("team-a", "10.200.0.0/22"), _vpc("t1-vpc", "10.200.8.0/22")],
    )

    assert missing == ["10.200.8.0/22"]


@pytest.mark.asyncio
async def test_pods_that_are_not_running_do_not_vote() -> None:
    """A Pending pod has no routes yet; that is not evidence of a lag."""
    k8s = _k8s([_pod("gw-1", ["10.200.0.0/22", "10.200.8.0/22"]),
                _pod("gw-2", [], phase="Pending")])

    missing = await _unannounced_cidrs(
        k8s, GW, [_vpc("t1-vpc", "10.200.8.0/22")],
    )

    assert missing == []


@pytest.mark.asyncio
async def test_no_running_pods_is_not_reported_as_a_lag() -> None:
    """A gateway with no pods has other problems, and they are already named."""
    k8s = _k8s([])

    assert await _unannounced_cidrs(k8s, GW, [_vpc("t1-vpc", "10.200.8.0/22")]) == []


@pytest.mark.asyncio
async def test_an_unreadable_annotation_does_not_invent_a_fault() -> None:
    p = _pod("gw-1", [])
    p.metadata.annotations = {"ovn.kubernetes.io/routes": "{not json"}
    k8s = _k8s([p])

    assert await _unannounced_cidrs(k8s, GW, [_vpc("t1-vpc", "10.200.8.0/22")]) == []
