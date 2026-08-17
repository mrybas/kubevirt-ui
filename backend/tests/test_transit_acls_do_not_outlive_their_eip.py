"""An allow must not survive the address it was written for.

Removal used to happen only by address, at tenant-delete time. A tenant whose
EIP had already gone — which happened whenever the EIP was cleaned up first —
left its allows behind for good. That is not untidy, it is a hole: the address
goes back into the subnet's pool, the next tenant is handed it, and inherits a
permit to a control-plane port belonging to somebody else. The ports in the
match are per-tenant, so the inherited permit points at another tenant's API.

Measured on the lab: an allow for 10.199.1.9 survived while .9 sat free in
`cp-transit`'s available range and only .1-.4 and .20 were in use.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.tenant_transit import (
    TRANSIT_ALLOW_PRIORITY,
    TRANSIT_DENY_PRIORITY,
    ensure_transit_acls,
)

TRANSIT = "cp-transit"
VIP = "10.199.0.100"


def _allow(src: str, port: int) -> dict:
    return {
        "action": "allow-related",
        "direction": "from-lport",
        "priority": TRANSIT_ALLOW_PRIORITY,
        "match": f"ip4.src == {src} && ip4.dst == {VIP} && tcp.dst == {port}",
    }


def _k8s(acls: list[dict], eips: dict[str, str]) -> tuple[MagicMock, dict]:
    subnet = {
        "metadata": {"name": TRANSIT, "resourceVersion": "1"},
        "spec": {"cidrBlock": "10.199.0.0/22", "excludeIps": ["10.199.0.1"],
                 "acls": acls},
    }
    captured: dict = {}

    async def get_obj(**kw):
        return subnet

    async def patch_obj(**kw):
        captured["acls"] = kw["body"]["spec"]["acls"]
        return subnet

    async def list_obj(**kw):
        return {"items": [
            {"spec": {"externalSubnet": TRANSIT}, "status": {"v4Ip": a}}
            for a in eips.values()
        ]}

    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    return k8s, captured


@pytest.mark.asyncio
async def test_an_allow_for_a_released_address_is_dropped() -> None:
    k8s, captured = _k8s(
        acls=[_allow("10.199.1.9", 20000), _allow("10.199.1.9", 20001)],
        eips={"cpt-eip-t1": "10.199.1.20"},
    )

    await ensure_transit_acls(k8s, TRANSIT, "10.199.1.20", VIP, [20000, 20001])

    sources = {
        a["match"].split("ip4.src == ")[1].split(" ")[0]
        for a in captured["acls"] if a["priority"] == TRANSIT_ALLOW_PRIORITY
    }
    assert sources == {"10.199.1.20"}, (
        "10.199.1.9 no longer belongs to any EIP and its address is back in the "
        f"pool; the allow must go with it: {sources}"
    )


@pytest.mark.asyncio
async def test_allows_of_other_live_tenants_are_kept() -> None:
    k8s, captured = _k8s(
        acls=[_allow("10.199.1.1", 20000)],
        eips={"cpt-eip-a": "10.199.1.1", "cpt-eip-b": "10.199.1.20"},
    )

    await ensure_transit_acls(k8s, TRANSIT, "10.199.1.20", VIP, [20000])

    sources = {
        a["match"].split("ip4.src == ")[1].split(" ")[0]
        for a in captured["acls"] if a["priority"] == TRANSIT_ALLOW_PRIORITY
    }
    assert sources == {"10.199.1.1", "10.199.1.20"}


@pytest.mark.asyncio
async def test_the_baseline_deny_is_never_touched() -> None:
    k8s, captured = _k8s(acls=[_allow("10.199.1.9", 20000)],
                         eips={"cpt-eip-t1": "10.199.1.20"})

    await ensure_transit_acls(k8s, TRANSIT, "10.199.1.20", VIP, [20000])

    denies = [a for a in captured["acls"] if a["priority"] == TRANSIT_DENY_PRIORITY]
    assert len(denies) == 1, "exactly one baseline deny, always"


@pytest.mark.asyncio
async def test_an_unreadable_eip_list_deletes_nothing() -> None:
    """Never prune on a guess: not knowing is not the same as knowing it is gone."""
    from kubernetes_asyncio.client.exceptions import ApiException

    k8s, captured = _k8s(acls=[_allow("10.199.1.9", 20000)], eips={})

    async def boom(**kw):
        raise ApiException(status=500)

    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=boom)

    await ensure_transit_acls(k8s, TRANSIT, "10.199.1.20", VIP, [20000])

    sources = {
        a["match"].split("ip4.src == ")[1].split(" ")[0]
        for a in captured["acls"] if a["priority"] == TRANSIT_ALLOW_PRIORITY
    }
    assert "10.199.1.9" in sources
