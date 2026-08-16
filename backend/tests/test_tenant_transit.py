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
    assert len(acls) == 2


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
