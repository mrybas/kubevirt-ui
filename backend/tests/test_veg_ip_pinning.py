"""A gateway must keep the same addresses across pod restarts.

Without pinned `internalIPs`/`externalIPs` kube-ovn hands each new gateway pod
a fresh address. One lab run burned `.11 → .12 → .13 → .14 → .15 → .16` on the
external leg, and every burned address left an orphaned dynamic BGP session on
the border router that retries forever:

    vpc1  BGP  start  Active   Socket: No route to host
    vpc2  BGP  up     Established

Routes are withdrawn correctly so there is no blackhole, but the session table
grows without bound and only a BIRD restart clears it. Anything keyed on the
gateway's address — upstream ACLs, firewall rules, monitoring — also has to be
rewritten every time a pod is replaced (backlog U6).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.egress_gateway import allocate_gateway_ips


def _subnet(
    name: str,
    cidr: str,
    gateway: str,
    exclude: list[str] | None = None,
    using: str = "",
) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"cidrBlock": cidr, "gateway": gateway, "excludeIps": exclude or []},
        "status": {"v4usingIPrange": using},
    }


def _k8s(subnets: list[dict], vegs: list[dict]) -> MagicMock:
    k8s = MagicMock()
    by_name = {s["metadata"]["name"]: s for s in subnets}

    async def get_obj(**kw):
        return by_name[kw["name"]]

    async def list_ns(**kw):
        return {"items": vegs}

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.list_namespaced_custom_object = AsyncMock(side_effect=list_ns)
    return k8s


@pytest.mark.asyncio
async def test_addresses_are_allocated_for_every_replica() -> None:
    k8s = _k8s(
        [_subnet("egw-t1-subnet", "10.199.16.0/24", "10.199.16.1"),
         _subnet("external", "10.199.4.0/22", "10.199.4.254")],
        vegs=[],
    )

    internal, external = await allocate_gateway_ips(
        k8s, "egw-t1-subnet", "external", replicas=2,
    )

    assert len(internal) == 2 and len(external) == 2
    assert "10.199.16.1" not in internal, "the subnet gateway must not be handed out"
    assert "10.199.4.254" not in external


@pytest.mark.asyncio
async def test_addresses_already_pinned_by_another_gateway_are_skipped() -> None:
    other = {
        "metadata": {"name": "egw-other"},
        "spec": {
            "internalIPs": ["10.199.16.2"],
            "externalIPs": ["10.199.4.11", "10.199.4.12"],
        },
    }
    k8s = _k8s(
        [_subnet("egw-t1-subnet", "10.199.16.0/24", "10.199.16.1"),
         _subnet("external", "10.199.4.0/22", "10.199.4.254")],
        vegs=[other],
    )

    internal, external = await allocate_gateway_ips(
        k8s, "egw-t1-subnet", "external", replicas=2,
    )

    assert "10.199.16.2" not in internal
    assert not ({"10.199.4.11", "10.199.4.12"} & set(external))


@pytest.mark.asyncio
async def test_excluded_ranges_are_respected() -> None:
    k8s = _k8s(
        [_subnet("egw-t1-subnet", "10.199.16.0/24", "10.199.16.1"),
         _subnet("external", "10.199.4.0/22", "10.199.4.254",
                 exclude=["10.199.4.1..10.199.4.20"])],
        vegs=[],
    )

    _, external = await allocate_gateway_ips(k8s, "egw-t1-subnet", "external", replicas=2)

    assert all(not ip.startswith("10.199.4.1") or int(ip.rsplit(".", 1)[1]) > 20
               for ip in external), f"allocated inside an excluded range: {external}"


@pytest.mark.asyncio
async def test_addresses_held_by_a_vpc_nat_gateway_are_skipped() -> None:
    """A NAT-gateway VPC's router port leaves no trace in the subnet spec.

    `enableExternal: true` makes kube-ovn allocate an address on the external
    subnet for a router port named `<vpc>-external`. There is no IP CR and
    nothing lands in `excludeIps`, so allocating from the spec alone reissues
    an address that is already in use — the gateway then pins it and its pods
    never leave `Init`:

        static address 10.199.4.1 ... has been assigned to team-a-external
        AcquireAddressFailed  NoAvailableAddress

    kube-ovn does record it, in `status.v4usingIPrange`.
    """
    k8s = _k8s(
        [_subnet("egw-t1-subnet", "10.199.128.0/24", "10.199.128.1"),
         _subnet("external", "10.199.4.0/22", "10.199.4.254",
                 using="10.199.4.1-10.199.4.3")],
        vegs=[],
    )

    internal, external = await allocate_gateway_ips(
        k8s, "egw-t1-subnet", "external", replicas=2,
    )

    assert external == ["10.199.4.4", "10.199.4.5"], (
        "the first free address is .4 — .1 through .3 are held by VPC router ports"
    )
    assert internal == ["10.199.128.2", "10.199.128.3"]


@pytest.mark.asyncio
async def test_status_range_absent_falls_back_to_the_spec() -> None:
    """A freshly created subnet has no status yet; allocation must still work."""
    k8s = _k8s(
        [_subnet("egw-t1-subnet", "10.199.128.0/24", "10.199.128.1"),
         _subnet("external", "10.199.4.0/22", "10.199.4.254")],
        vegs=[],
    )

    _, external = await allocate_gateway_ips(k8s, "egw-t1-subnet", "external", replicas=1)

    assert external == ["10.199.4.1"]


def test_status_ranges_use_a_single_dash_not_the_spec_double_dot() -> None:
    """`excludeIps` writes `a..b`; subnet status writes `a-b`. Parsing differs."""
    from app.api.v1.egress_gateway import _expand_status_ranges

    assert _expand_status_ranges("10.0.0.1-10.0.0.3") == {
        "10.0.0.1", "10.0.0.2", "10.0.0.3",
    }
    assert _expand_status_ranges("10.0.0.1-10.0.0.2,10.0.0.9-10.0.0.9") == {
        "10.0.0.1", "10.0.0.2", "10.0.0.9",
    }
    assert _expand_status_ranges("") == set()
    assert _expand_status_ranges("not-an-ip") == set()
