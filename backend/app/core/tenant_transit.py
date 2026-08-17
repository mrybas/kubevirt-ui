"""Wire a VPC tenant to the control-plane transit plane.

A tenant in its own VPC has to reach one address that lives outside it: the
shared control-plane VIP its workers join through. Everything else about the
VPC stays isolated, so this is deliberately the narrowest possible hole —
one destination prefix, reached over a dedicated transit subnet, with the
tenant's source address translated to a single EIP.

Five things make that work, and on the lab all five were written by hand with
`kubectl` because nothing here created them:

  1. `Vpc.spec.extraExternalSubnets` + `enableExternal` — attaches the tenant's
     logical router to the transit subnet. Without it the router has no port
     there at all.
  2. An `OvnEip` on the transit subnet — the tenant's single source address.
  3. An `OvnSnatRule` translating the tenant CIDR to that EIP. It needs
     `spec.vpc`; without it kube-ovn never programs the rule.
  4. A policy route at priority 30000 allowing `ip4.dst == <transit CIDR>`.
     This is the important one and the least obvious: an attached egress
     gateway installs its own reroute at 29100 matching on *source*, which
     would otherwise swallow control-plane traffic and send it out the internet
     leg. A higher-priority allow keeps the control-plane path on the transit
     port. OVN evaluates `lr_in_policy` (17) after `lr_in_ip_routing` (15), so
     the route from step 1 has to exist for this to be reached at all.
  5. ACLs on the transit subnet letting this tenant's EIP talk to the VIP on
     its own two ports, and nothing else.

Removing a tenant undoes 2, 3 and 5 — its own objects. Steps 1 and 4 belong to
the VPC, which may still host other tenants, so they are left alone unless the
caller says this was the last one.
"""

import ipaddress
import logging
import os
from typing import Any

from kubernetes_asyncio.client import ApiException

from app.core.cas import patch_spec_with_retry, upsert
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

logger = logging.getLogger(__name__)

# Priority band for the control-plane guard. Must sit above the egress
# gateway's reroute (29100) and below the per-subnet allows kube-ovn writes.
CP_GUARD_PRIORITY = 30000

MANAGED_LABEL = "kubevirt-ui.io/managed"
TENANT_LABEL = "kubevirt-ui.io/tenant"


def transit_subnet_name() -> str | None:
    """Name of the control-plane transit subnet, or None when not configured."""
    return os.getenv("TENANTS_CP_TRANSIT_SUBNET") or None


def _eip_name(tenant: str) -> str:
    return f"cpt-eip-{tenant}"


def _snat_name(tenant: str) -> str:
    return f"cpt-snat-{tenant}"


def _labels(tenant: str) -> dict[str, str]:
    return {MANAGED_LABEL: "true", TENANT_LABEL: tenant}


async def _get(k8s, plural: str, name: str) -> dict[str, Any] | None:
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=plural, name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


async def _create_ignore_conflict(k8s, plural: str, body: dict[str, Any]) -> None:
    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=plural, body=body,
        )
    except ApiException as e:
        if e.status != 409:
            raise


async def _delete_ignore_missing(k8s, plural: str, name: str) -> None:
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=plural, name=name,
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning("Could not delete %s/%s: %s", plural, name, e)


async def attach_vpc_to_transit(k8s, vpc_name: str, transit_subnet: str) -> None:
    """Give the tenant's router a port on the transit subnet, plus the guard."""
    subnet = await _get(k8s, "subnets", transit_subnet)
    if subnet is None:
        raise LookupError(f"transit subnet {transit_subnet!r} does not exist")
    transit_cidr = (subnet.get("spec", {}) or {}).get("cidrBlock", "")
    if not transit_cidr:
        raise LookupError(f"transit subnet {transit_subnet!r} has no cidrBlock")

    guard = {
        "priority": CP_GUARD_PRIORITY,
        "action": "allow",
        "match": f"ip4.dst == {transit_cidr}",
    }

    def mutate(spec: dict) -> dict | None:
        externals = list(spec.get("extraExternalSubnets") or [])
        policies = list(spec.get("policyRoutes") or [])

        want_external = transit_subnet not in externals
        want_guard = not any(
            p.get("priority") == CP_GUARD_PRIORITY and p.get("match") == guard["match"]
            for p in policies
        )
        if not want_external and not want_guard and spec.get("enableExternal"):
            return None

        if want_external:
            externals.append(transit_subnet)
        if want_guard:
            policies.append(guard)
        return {
            "enableExternal": True,
            "extraExternalSubnets": externals,
            "policyRoutes": policies,
        }

    await patch_spec_with_retry(k8s, "vpcs", vpc_name, mutate)


async def _existing_snat_address(
    k8s, vpc_name: str, tenant_subnet: str, skip: str,
) -> str | None:
    """The address already SNATting `tenant_subnet`, if something else set it up.

    A VPC created with a NAT gateway already carries `eip-<vpc>` + `snat-<vpc>`
    for its default subnet. Adding a second rule for the same logical IP does
    not give the tenant its own address — OVN keeps one SNAT per logical IP and
    the loser simply never appears in `lr-nat-list`.

    That silence is what made it fatal rather than untidy: the guard ACL is
    written for the address we asked for, traffic leaves under the address OVN
    actually kept, and the baseline deny — which covers the whole allocation
    range on purpose — drops it. On the lab the tenant's worker sat at
    `Provisioned` forever because every packet to its own API VIP was dropped:

        lr-nat-list t1-vpc  ->  snat  10.199.1.5  10.200.8.0/22
        ACL allow           ->  ip4.src == 10.199.1.6 && ... tcp.dst == 20000

    So: find the address that is already in force and write the ACLs for it.
    """
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="ovn-snat-rules",
        )
    except ApiException:
        return None

    for item in result.get("items", []) or []:
        if item.get("metadata", {}).get("name") == skip:
            continue
        spec = item.get("spec", {}) or {}
        if spec.get("vpc") != vpc_name or spec.get("vpcSubnet") != tenant_subnet:
            continue
        eip_name = spec.get("ovnEip")
        if not eip_name:
            continue
        eip = await _get(k8s, "ovn-eips", eip_name)
        address = ((eip or {}).get("status", {}) or {}).get("v4Ip")
        if not address:
            continue

        # Reusing a rule is only safe if the rule is actually in force, and
        # `status.ready` does not tell us that. Cycling the VPC's transit
        # attachment — which is exactly what deleting the last tenant does —
        # leaves the CR reporting ready while the router has no NAT at all,
        # and kube-ovn never recovers on its own:
        #
        #     OvnSnatRule snat-t1-vpc: status.ready = true, v4Eip = 10.199.1.5
        #     ovn-nbctl lr-nat-list t1-vpc: <empty>
        #     controller: failed to delete v4 snat ... not found ..., requeuing
        #
        # It loops trying to remove a NAT that is not there and never gets as
        # far as creating one. Recreating the rule is the only exit — the same
        # lesson `recreate_snat_rule` already records for updates.
        #
        # The cost is a momentary SNAT reset for anything already using this
        # VPC. That is worth paying here: this runs on tenant create, and the
        # alternative is a tenant whose worker can never reach its own control
        # plane, which is how this cost three lab runs.
        await _delete_ignore_missing(k8s, "ovn-snat-rules", item["metadata"]["name"])
        await _create_ignore_conflict(k8s, "ovn-snat-rules", {
            "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
            "kind": "OvnSnatRule",
            "metadata": {
                "name": item["metadata"]["name"],
                "labels": (item.get("metadata", {}).get("labels") or {}),
            },
            "spec": {
                "ovnEip": eip_name,
                "vpc": vpc_name,
                "vpcSubnet": tenant_subnet,
            },
        })
        return address
    return None


async def ensure_tenant_snat(
    k8s, tenant: str, vpc_name: str, tenant_subnet: str, transit_subnet: str,
) -> str | None:
    """The address this tenant's subnet SNATs to on the transit network.

    Reuses whatever rule already covers `tenant_subnet` — see
    `_existing_snat_address` for why a second one is worse than none. Only when
    nothing covers it do we create the tenant's own EIP and rule.

    Returns the address once kube-ovn has assigned one, else None — the caller
    can still finish; the ACLs that need the address are written on the next
    reconcile.
    """
    inherited = await _existing_snat_address(
        k8s, vpc_name, tenant_subnet, skip=_snat_name(tenant),
    )
    if inherited:
        return inherited

    eip = _eip_name(tenant)
    await _create_ignore_conflict(k8s, "ovn-eips", {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "OvnEip",
        "metadata": {"name": eip, "labels": {**_labels(tenant), "kubevirt-ui.io/vpc": vpc_name}},
        "spec": {"externalSubnet": transit_subnet, "type": "nat"},
    })

    await _create_ignore_conflict(k8s, "ovn-snat-rules", {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "OvnSnatRule",
        "metadata": {"name": _snat_name(tenant), "labels": _labels(tenant)},
        # `vpc` is mandatory — kube-ovn resolves the logical router from it and
        # a rule without it never leaves "failed to get vpc for snat".
        "spec": {"ovnEip": eip, "vpc": vpc_name, "vpcSubnet": tenant_subnet},
    })

    item = await _get(k8s, "ovn-eips", eip)
    return ((item or {}).get("status", {}) or {}).get("v4Ip") or None


async def recreate_snat_rule(
    k8s, tenant: str, vpc_name: str, tenant_subnet: str,
) -> None:
    """Re-point a SNAT rule at a different tenant subnet.

    kube-ovn accepts a patch of `v4IpCidr`/`vpcSubnet` and silently keeps
    serving the old value, so the only reliable update is delete-then-create.
    """
    await _delete_ignore_missing(k8s, "ovn-snat-rules", _snat_name(tenant))
    await _create_ignore_conflict(k8s, "ovn-snat-rules", {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "OvnSnatRule",
        "metadata": {"name": _snat_name(tenant), "labels": _labels(tenant)},
        "spec": {"ovnEip": _eip_name(tenant), "vpc": vpc_name, "vpcSubnet": tenant_subnet},
    })


def build_transit_acls(eip: str, vip: str, ports: list[int]) -> list[dict[str, Any]]:
    """Let one tenant's EIP reach the control-plane VIP on its own ports only."""
    acls: list[dict[str, Any]] = []
    for port in ports:
        if not port:
            continue
        acls.append({
            "action": "allow-related",
            "direction": "from-lport",
            "priority": TRANSIT_ALLOW_PRIORITY,
            "match": f"ip4.src == {eip} && ip4.dst == {vip} && tcp.dst == {port}",
        })
    return acls


async def ensure_transit_acls(
    k8s, transit_subnet: str, eip: str, vip: str, ports: list[int],
) -> None:
    """This tenant's allows, plus the deny they are exceptions to.

    The deny is written once for the subnet and left alone afterwards: it is
    the baseline every tenant's allow punches through, so it must not be
    duplicated per tenant and must not disappear when one is removed.
    """
    wanted = build_transit_acls(eip, vip, ports)
    if not wanted:
        return

    def mutate(spec: dict) -> dict | None:
        acls = list(spec.get("acls") or [])

        cidr = spec.get("cidrBlock", "")
        if cidr and not any(
            a.get("action") == "drop" and a.get("priority") == TRANSIT_DENY_PRIORITY
            for a in acls
        ):
            acls.append(build_transit_deny(cidr, spec.get("excludeIps") or []))

        missing = [a for a in wanted if a not in acls]
        if not missing and acls == (spec.get("acls") or []):
            return None
        return {"acls": acls + missing}

    await patch_spec_with_retry(k8s, "subnets", transit_subnet, mutate)


async def remove_tenant_transit(
    k8s, tenant: str, transit_subnet: str, eip_address: str | None = None,
) -> None:
    """Drop everything this tenant owns on the transit plane.

    The VPC's attachment and guard are left in place: other tenants of the same
    VPC still need them, and a stray `extraExternalSubnets` entry costs nothing
    while a missing one costs every tenant behind it.
    """
    await _delete_ignore_missing(k8s, "ovn-snat-rules", _snat_name(tenant))
    await _delete_ignore_missing(k8s, "ovn-eips", _eip_name(tenant))

    if not eip_address:
        return

    def mutate(spec: dict) -> dict | None:
        acls = list(spec.get("acls") or [])
        remaining = [a for a in acls if f"ip4.src == {eip_address} " not in a.get("match", "")]
        if len(remaining) == len(acls):
            return None
        return {"acls": remaining}

    await patch_spec_with_retry(k8s, "subnets", transit_subnet, mutate)


# The allows sit above the deny; both are on the transit subnet.
TRANSIT_ALLOW_PRIORITY = 3200
TRANSIT_DENY_PRIORITY = 3000


def _allocatable_ranges(cidr: str, exclude_ips: list[str]) -> list[str]:
    """The part of a subnet kube-ovn can actually hand addresses out of.

    `excludeIps` is where the deployment records what is reserved — on the lab
    the transit subnet is 10.199.0.0/22 with `10.199.0.1..10.199.0.255`
    excluded, because the first /24 holds the nodes and the control-plane VIP.
    What is left is what tenants get.
    """
    network = ipaddress.ip_network(cidr, strict=False)

    reserved: list[Any] = []
    for entry in exclude_ips or []:
        entry = str(entry).strip()
        try:
            if ".." in entry:
                first, _, last = entry.partition("..")
                reserved.extend(ipaddress.summarize_address_range(
                    ipaddress.IPv4Address(first.strip()),
                    ipaddress.IPv4Address(last.strip()),
                ))
            elif "/" in entry:
                reserved.append(ipaddress.ip_network(entry, strict=False))
            elif entry:
                reserved.append(ipaddress.ip_network(f"{entry}/32"))
        except ValueError:
            logger.warning("Unparseable excludeIps entry %r on %s", entry, cidr)

    remaining = [network]
    for block in reserved:
        nxt = []
        for net in remaining:
            if block.subnet_of(net):
                nxt.extend(net.address_exclude(block))
            elif not net.overlaps(block):
                nxt.append(net)
        remaining = nxt

    # The subnet's own network address survives the exclusion arithmetic as a
    # /32 that no host can ever hold. Dropping it keeps the rule readable —
    # these end up in front of whoever is debugging an ACL at 3am.
    network_addr = network.network_address
    return [
        str(n) for n in sorted(remaining)
        if not (n.prefixlen == 32 and n.network_address == network_addr)
    ]


def build_transit_deny(transit_cidr: str, exclude_ips: list[str]) -> dict[str, Any]:
    """The deny the per-tenant allows are exceptions to.

    Without it the transit plane is open: every allow is additive, so one
    tenant's EIP could still reach another tenant's control-plane ports and the
    nodes sitting on that subnet.

    Scoped by **source** to the range kube-ovn allocates EIPs from — the subnet
    minus its `excludeIps`. Using the whole subnet instead puts the nodes' own
    addresses and the control-plane VIP on the left-hand side of a drop rule;
    the rule set measured working on the lab scopes every drop by the EIP range,
    and service traffic is not something to leave resting on connection
    tracking.

    The range is taken whole rather than as its first /24: a /24-scoped rule is
    a rule about the tenants numbered lowest, and the 129th one is allocated
    outside it, keeps its own allow, and quietly falls out of the deny.
    """
    ranges = _allocatable_ranges(transit_cidr, exclude_ips)
    if not ranges:
        ranges = [str(ipaddress.ip_network(transit_cidr, strict=False))]

    match = (
        f"ip4.src == {ranges[0]}" if len(ranges) == 1
        else "ip4.src == {" + ", ".join(ranges) + "}"
    )
    return {
        "action": "drop",
        "direction": "from-lport",
        "priority": TRANSIT_DENY_PRIORITY,
        "match": match,
    }

