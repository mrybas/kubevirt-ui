"""A VPC tenant needs a path to the control-plane VIP, built for it.

Run #2 on the lab created a VPC tenant through the UI and then had to write
five things by hand before a worker could even see the control plane: the
transit attachment, an EIP, a SNAT rule, the priority-30000 guard, and the
transit ACLs. None of them were the operator's business (backlog U4).

The guard is the subtle one. An attached egress gateway installs a reroute at
priority 29100 matching on *source*, which catches control-plane traffic too
and sends it out the internet leg. A higher-priority allow for the transit
prefix keeps that traffic on the transit port.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.core.tenant_transit import (
    CP_GUARD_PRIORITY,
    attach_vpc_to_transit,
    build_transit_acls,
    ensure_tenant_snat,
    ensure_transit_acls,
    remove_tenant_transit,
)

TENANT = "ta"
VPC = "team-a"
TENANT_SUBNET = "team-a-default"
TRANSIT = "cp-transit"
TRANSIT_CIDR = "10.199.0.0/22"


def _k8s(vpc_spec: dict | None = None, transit_spec: dict | None = None) -> MagicMock:
    store = {
        "vpcs": {VPC: {"metadata": {"name": VPC, "resourceVersion": "1"}, "spec": vpc_spec or {}}},
        "subnets": {
            TRANSIT: {
                "metadata": {"name": TRANSIT, "resourceVersion": "1"},
                "spec": transit_spec or {"cidrBlock": TRANSIT_CIDR},
            },
        },
        "ovn-eips": {},
        "ovn-snat-rules": {},
    }

    k8s = MagicMock()

    async def get_obj(**kw):
        bucket = store[kw["plural"]]
        if kw["name"] not in bucket:
            raise ApiException(status=404, reason="NotFound")
        return bucket[kw["name"]]

    async def patch_obj(**kw):
        store[kw["plural"]][kw["name"]]["spec"].update(kw["body"]["spec"])
        return store[kw["plural"]][kw["name"]]

    async def create_obj(**kw):
        body = kw["body"]
        name = body["metadata"]["name"]
        bucket = store[kw["plural"]]
        if name in bucket:
            raise ApiException(status=409, reason="Conflict")
        bucket[name] = {**body, "metadata": {**body["metadata"], "resourceVersion": "1"}}
        return bucket[name]

    async def delete_obj(**kw):
        bucket = store[kw["plural"]]
        if kw["name"] not in bucket:
            raise ApiException(status=404, reason="NotFound")
        del bucket[kw["name"]]

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    k8s.custom_api.create_cluster_custom_object = AsyncMock(side_effect=create_obj)
    k8s.custom_api.delete_cluster_custom_object = AsyncMock(side_effect=delete_obj)
    k8s._store = store
    return k8s


@pytest.mark.asyncio
async def test_the_vpc_gets_a_port_on_the_transit_subnet() -> None:
    k8s = _k8s()
    await attach_vpc_to_transit(k8s, VPC, TRANSIT)

    spec = k8s._store["vpcs"][VPC]["spec"]
    assert spec["enableExternal"] is True
    assert TRANSIT in spec["extraExternalSubnets"]


@pytest.mark.asyncio
async def test_the_guard_outranks_the_egress_gateway_reroute() -> None:
    k8s = _k8s()
    await attach_vpc_to_transit(k8s, VPC, TRANSIT)

    guards = [
        p for p in k8s._store["vpcs"][VPC]["spec"]["policyRoutes"]
        if p["match"] == f"ip4.dst == {TRANSIT_CIDR}"
    ]
    assert len(guards) == 1
    assert guards[0]["priority"] == CP_GUARD_PRIORITY
    assert guards[0]["priority"] > 29100, "the egress gateway reroute would win"
    assert guards[0]["action"] == "allow"


@pytest.mark.asyncio
async def test_attaching_twice_changes_nothing() -> None:
    k8s = _k8s()
    await attach_vpc_to_transit(k8s, VPC, TRANSIT)
    await attach_vpc_to_transit(k8s, VPC, TRANSIT)

    spec = k8s._store["vpcs"][VPC]["spec"]
    assert spec["extraExternalSubnets"] == [TRANSIT]
    assert len(spec["policyRoutes"]) == 1


@pytest.mark.asyncio
async def test_an_existing_policy_route_is_not_dropped() -> None:
    other = {"priority": 31000, "action": "allow", "match": "ip4.dst == 10.200.4.0/22"}
    k8s = _k8s(vpc_spec={"policyRoutes": [other]})

    await attach_vpc_to_transit(k8s, VPC, TRANSIT)

    assert other in k8s._store["vpcs"][VPC]["spec"]["policyRoutes"]


@pytest.mark.asyncio
async def test_snat_rule_carries_the_vpc() -> None:
    k8s = _k8s()
    await ensure_tenant_snat(k8s, TENANT, VPC, TENANT_SUBNET, TRANSIT)

    rule = k8s._store["ovn-snat-rules"][f"cpt-snat-{TENANT}"]
    assert rule["spec"]["vpc"] == VPC
    assert rule["spec"]["vpcSubnet"] == TENANT_SUBNET
    assert rule["spec"]["ovnEip"] == f"cpt-eip-{TENANT}"

    eip = k8s._store["ovn-eips"][f"cpt-eip-{TENANT}"]
    assert eip["spec"]["externalSubnet"] == TRANSIT
    assert eip["spec"]["type"] == "nat"


def test_acls_open_only_this_tenants_ports() -> None:
    acls = build_transit_acls("10.199.1.1", "10.199.0.100", [16443, 18132])

    matches = [a["match"] for a in acls]
    assert "ip4.src == 10.199.1.1 && ip4.dst == 10.199.0.100 && tcp.dst == 16443" in matches
    assert "ip4.src == 10.199.1.1 && ip4.dst == 10.199.0.100 && tcp.dst == 18132" in matches
    assert all(a["action"] == "allow-related" for a in acls)


@pytest.mark.asyncio
async def test_transit_acls_keep_other_tenants_rules() -> None:
    theirs = {
        "action": "allow-related", "direction": "from-lport", "priority": 3200,
        "match": "ip4.src == 10.199.1.3 && ip4.dst == 10.199.0.100 && tcp.dst == 16444",
    }
    k8s = _k8s(transit_spec={"cidrBlock": TRANSIT_CIDR, "acls": [theirs]})

    await ensure_transit_acls(k8s, TRANSIT, "10.199.1.1", "10.199.0.100", [16443])

    acls = k8s._store["subnets"][TRANSIT]["spec"]["acls"]
    assert theirs in acls
    # theirs + mine + the subnet-wide deny the allows are exceptions to
    assert len(acls) == 3
    assert sum(1 for a in acls if a["action"] == "drop") == 1


@pytest.mark.asyncio
async def test_removing_a_tenant_takes_only_its_own_rules() -> None:
    mine = {
        "action": "allow-related", "direction": "from-lport", "priority": 3200,
        "match": "ip4.src == 10.199.1.1 && ip4.dst == 10.199.0.100 && tcp.dst == 16443",
    }
    theirs = {
        "action": "allow-related", "direction": "from-lport", "priority": 3200,
        "match": "ip4.src == 10.199.1.3 && ip4.dst == 10.199.0.100 && tcp.dst == 16444",
    }
    k8s = _k8s(transit_spec={"cidrBlock": TRANSIT_CIDR, "acls": [mine, theirs]})
    await ensure_tenant_snat(k8s, TENANT, VPC, TENANT_SUBNET, TRANSIT)

    await remove_tenant_transit(k8s, TENANT, TRANSIT, eip_address="10.199.1.1")

    assert f"cpt-snat-{TENANT}" not in k8s._store["ovn-snat-rules"]
    assert f"cpt-eip-{TENANT}" not in k8s._store["ovn-eips"]
    assert k8s._store["subnets"][TRANSIT]["spec"]["acls"] == [theirs]


@pytest.mark.asyncio
async def test_the_vpc_attachment_survives_a_tenant_removal() -> None:
    """Other tenants of the same VPC are still using it."""
    k8s = _k8s()
    await attach_vpc_to_transit(k8s, VPC, TRANSIT)
    await remove_tenant_transit(k8s, TENANT, TRANSIT, eip_address="10.199.1.1")

    assert TRANSIT in k8s._store["vpcs"][VPC]["spec"]["extraExternalSubnets"]


@pytest.mark.asyncio
async def test_the_tenant_flow_calls_the_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers the call site itself.

    The first version of this change referenced the helpers without importing
    them. Every test still passed, because nothing exercised the VPC branch —
    Python only resolves the name when the line runs. A tenant created through
    the API would have died with NameError.
    """
    from app.api.v1 import tenants_capi as mod

    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", TRANSIT)
    k8s = _k8s()

    await mod._wire_tenant_to_transit(k8s, TENANT, VPC, "10.199.0.100", [16443, 18132])

    assert TRANSIT in k8s._store["vpcs"][VPC]["spec"]["extraExternalSubnets"]
    assert f"cpt-snat-{TENANT}" in k8s._store["ovn-snat-rules"]


@pytest.mark.asyncio
async def test_an_unconfigured_transit_subnet_warns_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1 import tenants_capi as mod

    monkeypatch.delenv("TENANTS_CP_TRANSIT_SUBNET", raising=False)
    k8s = _k8s()

    await mod._wire_tenant_to_transit(k8s, TENANT, VPC, "10.199.0.100", [16443])

    assert k8s._store["ovn-snat-rules"] == {}, "nothing should be created blind"


@pytest.mark.asyncio
async def test_a_missing_transit_subnet_does_not_abort_tenant_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-built tenant is worse than a tenant with wiring to repair."""
    from app.api.v1 import tenants_capi as mod

    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", "does-not-exist")
    k8s = _k8s()

    await mod._wire_tenant_to_transit(k8s, TENANT, VPC, "10.199.0.100", [16443])


@pytest.mark.asyncio
async def test_tenant_deletion_releases_the_transit_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the delete call site — same missing-import trap as above."""
    from app.api.v1 import tenants_crud as mod

    monkeypatch.setenv("TENANTS_CP_TRANSIT_SUBNET", TRANSIT)
    k8s = _k8s()
    await ensure_tenant_snat(k8s, TENANT, VPC, TENANT_SUBNET, TRANSIT)

    await mod._release_tenant_transit(k8s, TENANT)

    assert f"cpt-eip-{TENANT}" not in k8s._store["ovn-eips"]
    assert f"cpt-snat-{TENANT}" not in k8s._store["ovn-snat-rules"]


def _covered(deny: dict) -> list:
    """Prefixes on the left-hand side of a deny, however they were summarised."""
    import ipaddress

    body = deny["match"].split("==", 1)[1].strip().strip("{}")
    return [ipaddress.ip_network(p.strip()) for p in body.split(",")]


class TestTransitDenyBaseline:
    """Allow rules alone leave the transit plane open.

    Each tenant gets an allow from its own EIP to the control-plane VIP on its
    own two ports. Without a deny underneath, everything else sourced in the
    transit subnet is still permitted — one tenant's EIP can reach another
    tenant's control-plane ports, and the nodes on that plane.

    The deny has to cover the whole range the allocator hands out. Scoping it
    to the first /24 is a rule about the tenants that happen to be numbered
    lowest: the 129th tenant gets an address outside it and silently falls out
    of the deny while still keeping its own allow.
    """

    def test_the_deny_covers_the_whole_allocation_range(self) -> None:
        """Scoped to where EIPs come from — not to the whole subnet.

        The subnet is 10.199.0.0/22 but `excludeIps` reserves the first /24
        for the nodes and the control-plane VIP, so kube-ovn hands EIPs out of
        10.199.1.0-10.199.3.255. Denying by the whole /22 would put the nodes'
        own addresses and the VIP on the left-hand side of a drop rule; the
        set that was measured working on the lab scopes every drop by the EIP
        range instead.
        """
        from app.core.tenant_transit import build_transit_deny

        deny = build_transit_deny("10.199.0.0/22", ["10.199.0.1..10.199.0.255"])

        import ipaddress

        assert deny["action"] == "drop"
        covered = _covered(deny)
        # every address kube-ovn can hand a tenant is denied by default
        for addr in ("10.199.1.1", "10.199.2.5", "10.199.3.255"):
            assert any(ipaddress.ip_address(addr) in n for n in covered), addr

    def test_node_and_vip_addresses_are_outside_the_deny(self) -> None:
        import ipaddress

        from app.core.tenant_transit import build_transit_deny

        deny = build_transit_deny("10.199.0.0/22", ["10.199.0.1..10.199.0.255"])
        covered = _covered(deny)

        for addr in ("10.199.0.11", "10.199.0.12", "10.199.0.13", "10.199.0.100"):
            assert not any(ipaddress.ip_address(addr) in n for n in covered), addr

    def test_without_exclusions_the_whole_subnet_is_the_range(self) -> None:
        from app.core.tenant_transit import build_transit_deny

        deny = build_transit_deny("10.199.0.0/22", [])

        assert "10.199.0.0/22" in deny["match"]

    def test_the_deny_sits_below_the_allows(self) -> None:
        from app.core.tenant_transit import build_transit_acls, build_transit_deny

        allows = build_transit_acls("10.199.1.1", "10.199.0.100", [16443])
        deny = build_transit_deny("10.199.0.0/22", ["10.199.0.1..10.199.0.255"])

        assert all(a["priority"] > deny["priority"] for a in allows), (
            "the deny would swallow the allows"
        )

    def test_a_tenant_outside_the_first_24_is_still_denied(self) -> None:
        """The 129th tenant — the case a /24-scoped rule misses."""
        import ipaddress

        from app.core.tenant_transit import build_transit_deny

        deny = build_transit_deny("10.199.0.0/22", ["10.199.0.1..10.199.0.255"])
        covered = _covered(deny)

        assert any(ipaddress.ip_address("10.199.2.5") in n for n in covered)
        assert any(ipaddress.ip_address("10.199.3.200") in n for n in covered)

    @pytest.mark.asyncio
    async def test_the_deny_is_written_once_and_kept(self) -> None:
        from app.core.tenant_transit import ensure_transit_acls

        k8s = _k8s(transit_spec={"cidrBlock": TRANSIT_CIDR})

        await ensure_transit_acls(k8s, TRANSIT, "10.199.1.1", "10.199.0.100", [16443])
        await ensure_transit_acls(k8s, TRANSIT, "10.199.1.3", "10.199.0.100", [16444])

        acls = k8s._store["subnets"][TRANSIT]["spec"]["acls"]
        denies = [a for a in acls if a["action"] == "drop"]
        assert len(denies) == 1, "the deny baseline was duplicated per tenant"
        assert len([a for a in acls if a["action"] == "allow-related"]) == 2

    @pytest.mark.asyncio
    async def test_removing_a_tenant_leaves_the_deny_alone(self) -> None:
        from app.core.tenant_transit import ensure_transit_acls

        k8s = _k8s(transit_spec={"cidrBlock": TRANSIT_CIDR})
        await ensure_transit_acls(k8s, TRANSIT, "10.199.1.1", "10.199.0.100", [16443])

        await remove_tenant_transit(k8s, TENANT, TRANSIT, eip_address="10.199.1.1")

        acls = k8s._store["subnets"][TRANSIT]["spec"]["acls"]
        assert [a["action"] for a in acls] == ["drop"], (
            "removing a tenant took the deny baseline with it"
        )
