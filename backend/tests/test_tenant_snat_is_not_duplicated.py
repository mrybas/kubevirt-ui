"""A tenant must SNAT under the address that is actually in force.

A VPC created with a NAT gateway already carries `eip-<vpc>` + `snat-<vpc>`
for its default subnet. The tenant path used to add `cpt-eip-<tenant>` +
`cpt-snat-<tenant>` for the same logical IP. OVN keeps one SNAT per logical
IP, so the second rule was silently ignored — but the guard ACL was written
for *its* address:

    lr-nat-list t1-vpc  ->  snat  10.199.1.5  10.200.8.0/22
    ACL allow           ->  ip4.src == 10.199.1.6 && ... tcp.dst == 20000

Every packet from the tenant to its own API VIP left as .5, missed the allow,
and hit the baseline deny that covers the whole allocation range. The worker
VM booted and then sat at `Provisioned` forever with nothing logged as an
error anywhere.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.tenant_transit import ensure_tenant_snat


def _k8s(snats: list[dict], eips: dict[str, str]) -> tuple[MagicMock, list]:
    created: list = []

    async def list_obj(**kw):
        return {"items": snats}

    async def get_obj(**kw):
        if kw["plural"] == "ovn-eips" and kw["name"] in eips:
            addr, subnet = eips[kw["name"]]
            return {"spec": {"externalSubnet": subnet}, "status": {"v4Ip": addr}}
        from kubernetes_asyncio.client.exceptions import ApiException
        raise ApiException(status=404)

    async def create_obj(**kw):
        created.append((kw["plural"], kw["body"]["metadata"]["name"]))
        return {}

    async def delete_obj(**kw):
        deleted.append(kw["name"])
        return {}

    deleted: list = []
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.create_cluster_custom_object = AsyncMock(side_effect=create_obj)
    k8s.custom_api.delete_cluster_custom_object = AsyncMock(side_effect=delete_obj)
    k8s._deleted = deleted
    return k8s, created


def _snat(name: str, vpc: str, subnet: str, eip: str) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"vpc": vpc, "vpcSubnet": subnet, "ovnEip": eip},
    }


@pytest.mark.asyncio
async def test_an_existing_vpc_snat_is_reused_not_duplicated() -> None:
    k8s, created = _k8s(
        snats=[_snat("snat-t1-vpc", "t1-vpc", "t1-vpc-default", "eip-t1-vpc")],
        eips={"eip-t1-vpc": ("10.199.1.5", "cp-transit")},
    )

    address = await ensure_tenant_snat(
        k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit",
    )

    assert address == "10.199.1.5", "the ACLs must name the address OVN keeps"
    assert [n for _, n in created] == ["snat-t1-vpc"], (
        f"the covering rule is recreated, never duplicated: {created}"
    )
    assert k8s._deleted == ["snat-t1-vpc"], (
        "delete-then-create is the only way kube-ovn re-programs a stale rule"
    )


@pytest.mark.asyncio
async def test_its_own_rule_is_created_when_nothing_covers_the_subnet() -> None:
    k8s, created = _k8s(snats=[], eips={"cpt-eip-t1": ("10.199.1.6", "cp-transit")})

    address = await ensure_tenant_snat(
        k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit",
    )

    assert address == "10.199.1.6"
    assert [name for _, name in created] == ["cpt-eip-t1", "cpt-snat-t1"]


@pytest.mark.asyncio
async def test_a_rule_for_a_different_subnet_is_not_mistaken_for_ours() -> None:
    k8s, created = _k8s(
        snats=[_snat("snat-other", "t1-vpc", "some-other-subnet", "eip-other")],
        eips={"eip-other": ("10.199.1.9", "cp-transit"),
              "cpt-eip-t1": ("10.199.1.6", "cp-transit")},
    )

    address = await ensure_tenant_snat(
        k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit",
    )

    assert address == "10.199.1.6"
    assert [name for _, name in created] == ["cpt-eip-t1", "cpt-snat-t1"]


@pytest.mark.asyncio
async def test_our_own_rule_from_a_previous_reconcile_is_not_read_as_foreign() -> None:
    """Re-running must not see `cpt-snat-<tenant>` and treat it as inherited."""
    k8s, created = _k8s(
        snats=[_snat("cpt-snat-t1", "t1-vpc", "t1-vpc-default", "cpt-eip-t1")],
        eips={"cpt-eip-t1": ("10.199.1.6", "cp-transit")},
    )

    address = await ensure_tenant_snat(
        k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit",
    )

    assert address == "10.199.1.6"
    assert [name for _, name in created] == ["cpt-eip-t1", "cpt-snat-t1"]


@pytest.mark.asyncio
async def test_a_slot_held_on_the_external_subnet_is_a_loud_conflict() -> None:
    """The `t2` incident: internet worked, the control plane was unreachable.

    Inheriting a rule whose EIP is on the external network writes the transit
    ACLs for an external address. It looks configured and works for nothing —
    the reply comes back to an address the node does not know on br-cptransit
    and leaves via its default gateway, where conntrack never saw the flow.
    """
    from app.core.tenant_transit import TransitSnatSlotTaken

    k8s, created = _k8s(
        snats=[_snat("snat-t2-vpc", "t2-vpc", "t2-vpc-default", "eip-t2-vpc")],
        eips={"eip-t2-vpc": ("10.199.4.5", "external")},
    )

    with pytest.raises(TransitSnatSlotTaken) as e:
        await ensure_tenant_snat(k8s, "t2", "t2-vpc", "t2-vpc-default", "cp-transit")

    assert "snat-t2-vpc" in str(e.value)
    assert "10.199.4.5" in str(e.value)
    assert created == [], "nothing may be created while the slot is contested"
