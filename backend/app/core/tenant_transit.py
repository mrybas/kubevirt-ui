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


async def ensure_tenant_snat(
    k8s, tenant: str, vpc_name: str, tenant_subnet: str, transit_subnet: str,
) -> str | None:
    """One EIP on the transit subnet plus the SNAT rule that uses it.

    Returns the EIP's address once kube-ovn has assigned one, else None — the
    caller can still finish; the ACLs that need the address are written on the
    next reconcile.
    """
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
            "priority": 3200,
            "match": f"ip4.src == {eip} && ip4.dst == {vip} && tcp.dst == {port}",
        })
    return acls


async def ensure_transit_acls(
    k8s, transit_subnet: str, eip: str, vip: str, ports: list[int],
) -> None:
    """Add this tenant's allow rules to the transit subnet, keeping the rest."""
    wanted = build_transit_acls(eip, vip, ports)
    if not wanted:
        return

    def mutate(spec: dict) -> dict | None:
        acls = list(spec.get("acls") or [])
        missing = [a for a in wanted if a not in acls]
        if not missing:
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


def drop_match_for_range(cidr: str) -> str:
    """Match string for a drop covering a whole allocation range.

    Scoping the transit drop to the first /24 of the range is a rule about the
    tenants that happen to be numbered lowest: the 129th tenant gets an address
    outside it and quietly falls out of the deny. The match has to cover the
    range the allocator can actually hand out.
    """
    return f"ip4.src == {ipaddress.ip_network(cidr, strict=False)}"
