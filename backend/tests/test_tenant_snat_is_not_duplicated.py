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


class TestALosingRuleIsRemovedNotLeftToLie:
    """Д26, a live exhibit that sat on the lab for days:

        snat-t1-vpc  ready=true  EIP 10.199.1.5   10.200.8.0/22  ← absent in NB
        cpt-snat-t1  ready=true  EIP 10.199.1.20  10.200.8.0/22  ← programmed
        ovn-nbctl lr-nat-list t1-vpc: snat 10.199.1.20 10.200.8.0/22

    Two rules said `ready: true` for one logical IP; OVN had programmed one.
    Nothing in the API told them apart, and the guard ACLs key on the address,
    so the next person to read this could not tell which address the tenant
    leaves under. The decision "the SNAT slot belongs to the control-plane
    path" settles it, so the transit rule wins and the rest go.
    """

    @pytest.mark.asyncio
    async def test_a_duplicate_transit_rule_is_deleted(self) -> None:
        k8s, created = _k8s(
            snats=[
                _snat("aaa-snat", "t1-vpc", "t1-vpc-default", "eip-a"),
                _snat("zzz-snat", "t1-vpc", "t1-vpc-default", "eip-z"),
            ],
            eips={"eip-a": ("10.199.1.20", "cp-transit"),
                  "eip-z": ("10.199.1.21", "cp-transit")},
        )

        address = await ensure_tenant_snat(
            k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit",
        )

        # Name order decides, so two reconciles cannot disagree.
        assert address == "10.199.1.20"
        assert "zzz-snat" in k8s._deleted
        assert "eip-z" in k8s._deleted, "the loser's EIP was holding a transit address"

    @pytest.mark.asyncio
    async def test_the_winners_eip_survives_the_cleanup(self) -> None:
        k8s, created = _k8s(
            snats=[
                _snat("aaa-snat", "t1-vpc", "t1-vpc-default", "shared-eip"),
                _snat("zzz-snat", "t1-vpc", "t1-vpc-default", "shared-eip"),
            ],
            eips={"shared-eip": ("10.199.1.20", "cp-transit")},
        )

        address = await ensure_tenant_snat(
            k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit",
        )

        assert address == "10.199.1.20"
        assert "shared-eip" not in k8s._deleted

    @pytest.mark.asyncio
    async def test_an_external_duplicate_loses_to_the_transit_rule(self) -> None:
        """A VPC that once had a NAT gateway, now serving a tenant.

        The external rule cannot be in force alongside the transit one, and the
        transit one is the one the control-plane path needs.
        """
        k8s, created = _k8s(
            snats=[
                _snat("cpt-snat-t1", "t1-vpc", "t1-vpc-default", "cpt-eip-t1"),
                _snat("snat-t1-vpc", "t1-vpc", "t1-vpc-default", "eip-t1-vpc"),
            ],
            eips={"cpt-eip-t1": ("10.199.1.20", "cp-transit"),
                  "eip-t1-vpc": ("10.199.4.5", "external")},
        )

        address = await ensure_tenant_snat(
            k8s, "t9", "t1-vpc", "t1-vpc-default", "cp-transit",
        )

        assert address == "10.199.1.20"
        assert "snat-t1-vpc" in k8s._deleted
        assert "eip-t1-vpc" in k8s._deleted

    @pytest.mark.asyncio
    async def test_a_sole_external_rule_is_still_reported_never_removed(self) -> None:
        """Deleting someone's only NAT rule is a bigger act than reporting it.

        Removal is for duplicates that provably cannot all be in force. With one
        claimant there is nothing to disambiguate — it is a conflict for a human.
        """
        from app.core.tenant_transit import TransitSnatSlotTaken

        k8s, _ = _k8s(
            snats=[_snat("snat-t2-vpc", "t2-vpc", "t2-vpc-default", "eip-t2-vpc")],
            eips={"eip-t2-vpc": ("10.199.4.5", "external")},
        )

        with pytest.raises(TransitSnatSlotTaken):
            await ensure_tenant_snat(k8s, "t2", "t2-vpc", "t2-vpc-default", "cp-transit")

        assert k8s._deleted == []

    @pytest.mark.asyncio
    async def test_a_rule_pointing_at_a_missing_eip_is_swept_up(self) -> None:
        """The live exhibit: `snat-t1-vpc` → `eip-t1-vpc`, which no longer exists.

        It cannot be programmed under any circumstances, and it still reported
        `ready: true` for days next to the rule that was actually in force.
        """
        k8s, _ = _k8s(
            snats=[
                _snat("cpt-snat-t1", "t1-vpc", "t1-vpc-default", "cpt-eip-t1"),
                _snat("snat-t1-vpc", "t1-vpc", "t1-vpc-default", "eip-t1-vpc"),
            ],
            eips={"cpt-eip-t1": ("10.199.1.20", "cp-transit")},  # eip-t1-vpc: gone
        )

        address = await ensure_tenant_snat(
            k8s, "t9", "t1-vpc", "t1-vpc-default", "cp-transit",
        )

        assert address == "10.199.1.20"
        assert "snat-t1-vpc" in k8s._deleted

    @pytest.mark.asyncio
    async def test_a_dangling_rule_is_left_alone_when_it_is_the_only_claimant(self) -> None:
        """Mid-create the EIP may just not exist yet — that is not a leftover."""
        k8s, created = _k8s(
            snats=[_snat("snat-t1-vpc", "t1-vpc", "t1-vpc-default", "eip-t1-vpc")],
            eips={"cpt-eip-t1": ("10.199.1.20", "cp-transit")},
        )

        await ensure_tenant_snat(k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit")

        assert "snat-t1-vpc" not in k8s._deleted


class TestARuleWedgedInTerminating:
    """`kubectl delete` already happened; the finalizer cannot finish.

    The lab's exhibit, read 10 hours after someone deleted it:

        deletionTimestamp: 2026-08-17T00:06:00Z
        finalizers: [kubeovn.io/kube-ovn-controller]
        status: {ready: true, v4Eip: 10.199.1.5}
        spec.ovnEip: eip-t1-vpc      ← no such OvnEip
        ovn-nbctl lr-nat-list t1-vpc: snat 10.199.1.20 10.200.8.0/22

    kube-ovn loops "failed to delete v4 snat ... not found ..., requeuing"
    because the EIP it wants to unprogram is gone. Issuing another delete
    changes nothing; the finalizer is the only thing holding the object.
    """

    def _wedged(self, name: str, eip: str) -> dict:
        item = _snat(name, "t1-vpc", "t1-vpc-default", eip)
        item["metadata"]["deletionTimestamp"] = "2026-08-17T00:06:00Z"
        item["metadata"]["finalizers"] = ["kubeovn.io/kube-ovn-controller"]
        return item

    @pytest.mark.asyncio
    async def test_the_unsatisfiable_finalizer_is_released(self) -> None:
        k8s, _ = _k8s(
            snats=[
                _snat("cpt-snat-t1", "t1-vpc", "t1-vpc-default", "cpt-eip-t1"),
                self._wedged("snat-t1-vpc", "eip-t1-vpc"),  # EIP is gone
            ],
            eips={"cpt-eip-t1": ("10.199.1.20", "cp-transit")},
        )
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        await ensure_tenant_snat(k8s, "t9", "t1-vpc", "t1-vpc-default", "cp-transit")

        patch = k8s.custom_api.patch_cluster_custom_object.await_args
        assert patch.kwargs["name"] == "snat-t1-vpc"
        assert patch.kwargs["body"] == {"metadata": {"finalizers": []}}
        assert patch.kwargs["_content_type"] == "application/merge-patch+json", (
            "a strategic-merge patch does not clear a list"
        )

    @pytest.mark.asyncio
    async def test_a_finalizer_with_real_work_is_left_alone(self) -> None:
        """Its EIP still exists, so the controller can finish on its own.

        Ripping the finalizer off here would orphan the NAT it is holding.
        """
        k8s, _ = _k8s(
            snats=[
                _snat("cpt-snat-t1", "t1-vpc", "t1-vpc-default", "cpt-eip-t1"),
                self._wedged("snat-t1-vpc", "eip-t1-vpc"),
            ],
            eips={"cpt-eip-t1": ("10.199.1.20", "cp-transit"),
                  "eip-t1-vpc": ("10.199.1.5", "cp-transit")},
        )
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        await ensure_tenant_snat(k8s, "t9", "t1-vpc", "t1-vpc-default", "cp-transit")

        k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_healthy_loser_keeps_its_finalizer(self) -> None:
        """Not terminating at all — an ordinary delete is the whole story."""
        k8s, _ = _k8s(
            snats=[
                _snat("aaa-snat", "t1-vpc", "t1-vpc-default", "eip-a"),
                _snat("zzz-snat", "t1-vpc", "t1-vpc-default", "eip-z"),
            ],
            eips={"eip-a": ("10.199.1.20", "cp-transit"),
                  "eip-z": ("10.199.1.21", "cp-transit")},
        )
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        await ensure_tenant_snat(k8s, "t1", "t1-vpc", "t1-vpc-default", "cp-transit")

        assert "zzz-snat" in k8s._deleted
        k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()
