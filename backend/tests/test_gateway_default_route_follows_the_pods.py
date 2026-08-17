"""The gateway VPC's default route can name a pod address that is long gone.

An unpinned gateway that went through a pod replacement leaves

    egw-<name>.spec.staticRoutes: 0.0.0.0/0 → 10.199.16.3

while the pods now hold .5 and .7. Measured on the lab, traffic keeps flowing:
kube-ovn maintains its own reroute policy at priority 29100 pointing at the
live pods, and `lr_in_policy` (17) runs after `lr_in_ip_routing` (15), so the
policy wins. That is why this is repaired quietly on read instead of reported
as Degraded — it is a spec that lies, not an outage.

It is repaired because a next hop that does not exist is exactly the kind of
thing someone chasing a real fault stops on. It cost two such stops.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import realign_gateway_default_route

GW = "shared-egress"
GW_VPC = "egw-shared-egress"


def _k8s(routes: list[dict]) -> MagicMock:
    vpc = {
        "metadata": {"name": GW_VPC, "resourceVersion": "1"},
        "spec": {"staticRoutes": list(routes)},
    }

    async def get_obj(**kw):
        return vpc

    async def patch_obj(**kw):
        vpc["spec"].update(kw["body"]["spec"])
        return vpc

    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    k8s._vpc = vpc
    return k8s


def _veg(internal: list[str], pinned: bool = True) -> dict:
    field = "spec" if pinned else "status"
    return {"spec": {}, "status": {}, field: {"internalIPs": internal}}


@pytest.mark.asyncio
async def test_a_route_to_a_dead_pod_address_is_repointed() -> None:
    k8s = _k8s([{"cidr": "0.0.0.0/0", "nextHopIP": "10.199.16.3", "policy": "policyDst"}])

    changed = await realign_gateway_default_route(k8s, GW, _veg(["10.199.16.5", "10.199.16.7"]))

    assert changed is True
    defaults = [r for r in k8s._vpc["spec"]["staticRoutes"] if r["cidr"] == "0.0.0.0/0"]
    assert len(defaults) == 1, "one prefix, one route — never a second one alongside"
    assert defaults[0]["nextHopIP"] == "10.199.16.5"


@pytest.mark.asyncio
async def test_a_route_that_names_a_live_pod_is_left_alone() -> None:
    """Rewriting on every read would churn the spec for nothing."""
    k8s = _k8s([{"cidr": "0.0.0.0/0", "nextHopIP": "10.199.16.7"}])

    changed = await realign_gateway_default_route(k8s, GW, _veg(["10.199.16.5", "10.199.16.7"]))

    assert changed is False
    k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_prefixes_are_untouched() -> None:
    """The tenant routes on the hub share this list; only the default moves."""
    k8s = _k8s([
        {"cidr": "0.0.0.0/0", "nextHopIP": "10.199.16.3"},
        {"cidr": "10.200.8.0/22", "nextHopIP": "10.199.129.2"},
    ])

    await realign_gateway_default_route(k8s, GW, _veg(["10.199.16.5"]))

    tenant = [r for r in k8s._vpc["spec"]["staticRoutes"] if r["cidr"] == "10.200.8.0/22"]
    assert tenant == [{"cidr": "10.200.8.0/22", "nextHopIP": "10.199.129.2"}]


@pytest.mark.asyncio
async def test_nothing_happens_without_a_known_live_address() -> None:
    """Better a stale next hop than one invented from an empty status."""
    k8s = _k8s([{"cidr": "0.0.0.0/0", "nextHopIP": "10.199.16.3"}])

    changed = await realign_gateway_default_route(k8s, GW, {"spec": {}, "status": {}})

    assert changed is False
    k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_status_address_is_used_when_nothing_is_pinned() -> None:
    k8s = _k8s([{"cidr": "0.0.0.0/0", "nextHopIP": "10.199.16.3"}])

    changed = await realign_gateway_default_route(
        k8s, GW, _veg(["10.199.16.9"], pinned=False),
    )

    assert changed is True
    assert k8s._vpc["spec"]["staticRoutes"][0]["nextHopIP"] == "10.199.16.9"
