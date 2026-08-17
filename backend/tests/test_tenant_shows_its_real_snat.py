"""The address the tenant leaves under has to be readable from the API.

The transit guard ACLs are keyed on an *address*, so that address decides
whether a worker can reach its own control plane. It was nowhere in the API,
and on the lab two SNAT rules claimed one logical IP with `status.ready: true`
on both — nothing distinguished the rule OVN had programmed from the one it
had ignored.

A GET must not fix anything, so this resolver only reports: it picks the same
winner the reconcile would, and stays silent when there is no honest answer.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from app.core.tenant_transit import effective_snat_address


def _k8s(snats: list[dict], eips: dict[str, tuple[str, str]]) -> MagicMock:
    deleted: list[str] = []

    async def list_obj(**kw):
        return {"items": snats}

    async def get_obj(**kw):
        if kw["plural"] == "ovn-eips" and kw["name"] in eips:
            addr, subnet = eips[kw["name"]]
            return {"spec": {"externalSubnet": subnet}, "status": {"v4Ip": addr}}
        raise ApiException(status=404)

    async def delete_obj(**kw):
        deleted.append(kw["name"])

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.delete_cluster_custom_object = AsyncMock(side_effect=delete_obj)
    k8s.custom_api.create_cluster_custom_object = AsyncMock()
    k8s._deleted = deleted
    return k8s


def _snat(name: str, vpc: str, subnet: str, eip: str) -> dict:
    return {"metadata": {"name": name},
            "spec": {"vpc": vpc, "vpcSubnet": subnet, "ovnEip": eip}}


@pytest.mark.asyncio
async def test_it_reports_the_transit_address() -> None:
    k8s = _k8s(
        snats=[_snat("cpt-snat-t1", "t1-vpc", "t1-vpc-default", "cpt-eip-t1")],
        eips={"cpt-eip-t1": ("10.199.1.20", "cp-transit")},
    )

    assert await effective_snat_address(
        k8s, "t1-vpc", "t1-vpc-default", "cp-transit"
    ) == "10.199.1.20"


@pytest.mark.asyncio
async def test_it_names_the_same_winner_the_reconcile_would() -> None:
    """Otherwise the UI shows one address and the ACLs are written for another."""
    k8s = _k8s(
        snats=[
            _snat("zzz-snat", "t1-vpc", "t1-vpc-default", "eip-z"),
            _snat("aaa-snat", "t1-vpc", "t1-vpc-default", "eip-a"),
        ],
        eips={"eip-a": ("10.199.1.20", "cp-transit"),
              "eip-z": ("10.199.1.21", "cp-transit")},
    )

    assert await effective_snat_address(
        k8s, "t1-vpc", "t1-vpc-default", "cp-transit"
    ) == "10.199.1.20"


@pytest.mark.asyncio
async def test_reading_never_deletes() -> None:
    """A GET that prunes CRs is a trap; the reconcile owns that."""
    k8s = _k8s(
        snats=[
            _snat("aaa-snat", "t1-vpc", "t1-vpc-default", "eip-a"),
            _snat("zzz-snat", "t1-vpc", "t1-vpc-default", "eip-z"),
        ],
        eips={"eip-a": ("10.199.1.20", "cp-transit"),
              "eip-z": ("10.199.1.21", "cp-transit")},
    )

    await effective_snat_address(k8s, "t1-vpc", "t1-vpc-default", "cp-transit")

    assert k8s._deleted == []


@pytest.mark.asyncio
async def test_a_slot_held_off_the_transit_network_yields_nothing() -> None:
    """There is no honest single address here — `transit_conflict` says why."""
    k8s = _k8s(
        snats=[_snat("snat-t2-vpc", "t2-vpc", "t2-vpc-default", "eip-t2-vpc")],
        eips={"eip-t2-vpc": ("10.199.4.5", "external")},
    )

    assert await effective_snat_address(
        k8s, "t2-vpc", "t2-vpc-default", "cp-transit"
    ) is None


@pytest.mark.asyncio
async def test_nothing_covering_the_subnet_yields_nothing() -> None:
    assert await effective_snat_address(
        _k8s(snats=[], eips={}), "t1-vpc", "t1-vpc-default", "cp-transit"
    ) is None
