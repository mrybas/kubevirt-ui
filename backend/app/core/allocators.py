"""Shared CIDR allocator using ConfigMap-based counter with optimistic locking."""

import asyncio
import ipaddress
import logging
from typing import Any

from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException, V1ConfigMap, V1ObjectMeta

from app.core.constants import SYSTEM_NAMESPACE

logger = logging.getLogger(__name__)

VPC_CIDR_CONFIGMAP = "vpc-cidr-allocator"
VPC_CIDR_BASE = 200  # 10.{200+N}.0.0/24 — above K8s service CIDR (10.96.0.0/12)

MAX_RETRIES = 5
BASE_DELAY = 0.1  # seconds

KUBEOVN_GROUP = "kubeovn.io"
KUBEOVN_VERSION = "v1"


async def list_subnet_cidrs(k8s) -> list[tuple[str, str]]:
    """Every (subnet name, cidrBlock) currently defined in the cluster.

    Includes subnets we did not create — an overlap with one of those breaks
    just as thoroughly as an overlap with ours.
    """
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
        )
    except ApiException as e:
        logger.warning(f"Could not list subnets for CIDR conflict check: {e}")
        return []

    cidrs: list[tuple[str, str]] = []
    for item in result.get("items", []):
        cidr = item.get("spec", {}).get("cidrBlock", "")
        name = item.get("metadata", {}).get("name", "")
        # Dual-stack subnets carry "v4cidr,v6cidr"; we only allocate IPv4.
        for part in str(cidr).split(","):
            part = part.strip()
            if part:
                cidrs.append((name, part))
    return cidrs


def _parse(cidr: str) -> Any | None:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


def find_cidr_conflicts(cidr: str, existing: list[tuple[str, str]]) -> list[str]:
    """Names of the subnets whose range overlaps `cidr`.

    Overlap — not equality: 10.203.0.0/24 inside somebody's 10.203.0.0/16 is
    just as broken, and containment in either direction is a conflict.
    """
    wanted = _parse(cidr)
    if wanted is None:
        return []

    conflicts: list[str] = []
    for name, other in existing:
        parsed = _parse(other)
        if parsed is None or parsed.version != wanted.version:
            continue
        if wanted.overlaps(parsed):
            conflicts.append(f"{name} ({other})")
    return conflicts


async def assert_cidr_free(k8s, cidr: str) -> None:
    """Raise 409 when `cidr` overlaps an existing subnet.

    Overlapping VPCs break more than addressing: peering static routes become
    ambiguous, the isolation ACLs are written in terms of CIDRs, and BGP
    derives each gateway's router-id from its internal address — two VPCs on
    the same range give two speakers the same id, which the peer rejects
    within one AS.
    """
    conflicts = find_cidr_conflicts(cidr, await list_subnet_cidrs(k8s))
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail=(
                f"CIDR {cidr} overlaps existing subnet(s): {', '.join(conflicts)}. "
                "Pick a non-overlapping range, or omit subnet_cidr to have one "
                "allocated."
            ),
        )


async def allocate_vpc_cidr(k8s) -> tuple[str, str]:
    """Allocate the next VPC CIDR using ConfigMap-based counter with optimistic locking.

    Uses replace with resourceVersion for optimistic concurrency control.
    Retries on 409 Conflict with exponential backoff.
    Returns (cidr, gateway_ip) tuple.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await _allocate_vpc_cidr_once(k8s)
        except ApiException as e:
            if e.status == 409 and attempt < MAX_RETRIES - 1:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(f"VPC CIDR allocation conflict (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)
                continue
            raise

    raise HTTPException(status_code=409, detail="VPC CIDR allocation failed after retries")


async def _allocate_vpc_cidr_once(k8s) -> tuple[str, str]:
    """Single attempt to allocate the next VPC CIDR."""
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=VPC_CIDR_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        )
        data = cm.data or {}
        next_index = int(data.get("next_index", "0"))
        resource_version = cm.metadata.resource_version
    except ApiException as e:
        if e.status == 404:
            cm = await k8s.core_api.create_namespaced_config_map(
                namespace=SYSTEM_NAMESPACE,
                body=V1ConfigMap(
                    metadata=V1ObjectMeta(
                        name=VPC_CIDR_CONFIGMAP,
                        labels={"kubevirt-ui.io/managed": "true"},
                    ),
                    data={"next_index": "0"},
                ),
            )
            next_index = 0
            resource_version = cm.metadata.resource_version
        else:
            raise

    # The counter alone is not authoritative: `subnet_cidr` on the create
    # request lets a caller take a range out of this same pool by hand, and
    # the counter knows nothing about it. Walking past occupied indices is
    # what stops the allocator from later handing out a duplicate of one.
    existing = await list_subnet_cidrs(k8s)

    index = next_index
    while True:
        second_octet = VPC_CIDR_BASE + index
        if second_octet > 254:
            raise HTTPException(
                status_code=409, detail="VPC CIDR pool exhausted (max 55 VPCs)",
            )
        cidr = f"10.{second_octet}.0.0/24"
        conflicts = find_cidr_conflicts(cidr, existing)
        if not conflicts:
            break
        logger.info(
            f"VPC CIDR {cidr} already taken by {', '.join(conflicts)}; "
            "skipping to the next index"
        )
        index += 1

    gateway = f"10.{second_octet}.0.1"

    # Increment counter with optimistic lock (resourceVersion).
    # If another request raced us, this will return 409 Conflict.
    await k8s.core_api.replace_namespaced_config_map(
        name=VPC_CIDR_CONFIGMAP,
        namespace=SYSTEM_NAMESPACE,
        body=V1ConfigMap(
            metadata=V1ObjectMeta(
                name=VPC_CIDR_CONFIGMAP,
                namespace=SYSTEM_NAMESPACE,
                resource_version=resource_version,
                labels={"kubevirt-ui.io/managed": "true"},
            ),
            data={"next_index": str(index + 1)},
        ),
    )

    return cidr, gateway


# Link networks for VPC peering. Link-local space (RFC 3927) on purpose: these
# /30s exist only between two VPC routers, are never announced, and must not
# collide with anything routable.
PEERING_LINK_BASE = "169.254.101.0"
PEERING_LINK_MAX = 62  # /24 carved into /30s, minus the all-zeros one
PEERING_LINK_CONFIGMAP = "kubevirt-ui-peering-links"


def peering_link_addresses(index: int) -> tuple[str, str, str]:
    """The (local, remote, cidr) addresses of peering link `index`.

    Each link is a /30: .0 network, .1 local, .2 remote, .3 broadcast.
    """
    base = int(ipaddress.ip_address(PEERING_LINK_BASE))
    net = ipaddress.ip_network(f"{ipaddress.ip_address(base + index * 4)}/30")
    hosts = list(net.hosts())
    return str(hosts[0]), str(hosts[1]), str(net)


async def list_peering_link_cidrs(k8s) -> list[str]:
    """Link /30s already claimed by an existing peering."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
        )
    except ApiException as e:
        logger.warning(f"Could not list VPCs for peering-link allocation: {e}")
        return []

    used: list[str] = []
    for item in result.get("items", []):
        for peering in item.get("spec", {}).get("vpcPeerings", []) or []:
            connect_ip = peering.get("localConnectIP", "")
            if connect_ip:
                # Stored as "169.254.101.1/30" — normalise to the network.
                try:
                    used.append(str(ipaddress.ip_network(connect_ip, strict=False)))
                except ValueError:
                    continue
    return used


async def allocate_peering_link(k8s) -> tuple[str, str, str]:
    """Allocate an unused /30 for a VPC peering link.

    Returns (local_ip, remote_ip, cidr). Skips links already referenced by a
    VPC's `vpcPeerings`, so it stays correct even when one was created by
    hand or the counter ConfigMap is lost.

    Reserved through a counter ConfigMap with optimistic locking, the same way
    VPC CIDRs are. Reading the used set and picking the first gap is not
    enough: two peerings created at the same moment read the same state and
    both chose 169.254.101.8/30 —

        cc1 {'localConnectIP': '169.254.101.9/30',  'remoteVpc': 'cc2'}
        cc3 {'localConnectIP': '169.254.101.9/30',  'remoteVpc': 'cc4'}

    — two unrelated VPC routers holding the same address on what is supposed
    to be a point-to-point link.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await _allocate_peering_link_once(k8s)
        except ApiException as e:
            if e.status == 409 and attempt < MAX_RETRIES - 1:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Peering link allocation conflict "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}), retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
                continue
            raise

    raise HTTPException(
        status_code=409, detail="Peering link allocation failed after retries",
    )


async def _allocate_peering_link_once(k8s) -> tuple[str, str, str]:
    """Single attempt: pick the lowest free /30 and reserve it."""
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=PEERING_LINK_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        )
        next_index = int((cm.data or {}).get("next_index", "0"))
        resource_version = cm.metadata.resource_version
    except ApiException as e:
        if e.status != 404:
            raise
        cm = await k8s.core_api.create_namespaced_config_map(
            namespace=SYSTEM_NAMESPACE,
            body=V1ConfigMap(
                metadata=V1ObjectMeta(
                    name=PEERING_LINK_CONFIGMAP,
                    labels={"kubevirt-ui.io/managed": "true"},
                ),
                data={"next_index": "0"},
            ),
        )
        next_index = 0
        resource_version = cm.metadata.resource_version

    # The counter is not authoritative on its own — a peering can be written
    # by hand, and deleting one frees its link without moving the counter.
    used = set(await list_peering_link_cidrs(k8s))

    index = next_index
    while index < PEERING_LINK_MAX:
        local, remote, cidr = peering_link_addresses(index)
        if cidr not in used:
            break
        index += 1
    else:
        raise HTTPException(
            status_code=409,
            detail=f"VPC peering link pool exhausted (max {PEERING_LINK_MAX} links)",
        )

    # Reserve it. A racing caller that read the same resourceVersion loses
    # here with a 409 and retries against the moved counter.
    await k8s.core_api.replace_namespaced_config_map(
        name=PEERING_LINK_CONFIGMAP,
        namespace=SYSTEM_NAMESPACE,
        body=V1ConfigMap(
            metadata=V1ObjectMeta(
                name=PEERING_LINK_CONFIGMAP,
                namespace=SYSTEM_NAMESPACE,
                resource_version=resource_version,
                labels={"kubevirt-ui.io/managed": "true"},
            ),
            data={"next_index": str(index + 1)},
        ),
    )

    return local, remote, cidr
