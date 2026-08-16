"""VPC peering helpers for kube-ovn.

kube-ovn has **no VpcPeering CRD**. A peering is expressed as an entry under
`vpc.spec.vpcPeerings[]` on *each* of the two VPCs, every side carrying its own
`localConnectIP` out of a shared transit CIDR:

    # vpc-a                          # vpc-b
    spec:                            spec:
      vpcPeerings:                     vpcPeerings:
        - remoteVpc: vpc-b                 - remoteVpc: vpc-a
          localConnectIP: 10.255.0.1/24      localConnectIP: 10.255.0.2/24

Talking to a `vpc-peerings` collection instead returns 404 from the API server
("the server doesn't have a resource type"), which on a read path is
indistinguishable from "no peerings exist" — so the mistake reads as an empty
list rather than as an error.
"""

import ipaddress
import logging

from kubernetes_asyncio.client import ApiException

from app.core.cas import patch_spec_with_retry, upsert
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

logger = logging.getLogger(__name__)


def transit_prefix_len(transit_cidr: str) -> int:
    """Prefix length the peering connect IPs carry, taken from the transit CIDR."""
    return ipaddress.IPv4Network(transit_cidr, strict=False).prefixlen


async def _get_vpc(k8s, vpc_name: str) -> dict | None:
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="vpcs",
            name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


async def ensure_vpc_peering(
    k8s, vpc_name: str, remote_vpc: str, local_connect_ip: str,
) -> bool:
    """Declare one side of a peering on `vpc_name` (idempotent).

    Returns False if the VPC does not exist, True once the entry is in place.
    Call once per side — kube-ovn will not build the link from one half.

    The write goes through compare-and-set: `spec.vpcPeerings` is a list that
    several flows append to (a tenant's link to `ovn-cluster`, an egress
    gateway's link to its tenant), and a plain merge patch computed from a
    stale read would drop whichever entry landed in between. See `core.cas`.
    """
    entry = {"remoteVpc": remote_vpc, "localConnectIP": local_connect_ip}

    def mutate(spec: dict) -> dict | None:
        peerings = spec.get("vpcPeerings") or []
        if any(p.get("remoteVpc") == remote_vpc
               and p.get("localConnectIP") == local_connect_ip for p in peerings):
            return None  # already in the desired state
        return {"vpcPeerings": upsert(peerings, entry, "remoteVpc")}

    if await patch_spec_with_retry(k8s, "vpcs", vpc_name, mutate):
        return True

    # `mutate` returning None (already correct) and a missing VPC both come back
    # False; tell them apart so callers keep their "does the VPC exist" contract.
    return await _get_vpc(k8s, vpc_name) is not None


async def remove_vpc_peering(k8s, vpc_name: str, remote_vpc: str) -> None:
    """Drop one side of a peering. A missing VPC or entry is not an error."""

    def mutate(spec: dict) -> dict | None:
        peerings = spec.get("vpcPeerings") or []
        remaining = [p for p in peerings if p.get("remoteVpc") != remote_vpc]
        if len(remaining) == len(peerings):
            return None
        return {"vpcPeerings": remaining}

    await patch_spec_with_retry(k8s, "vpcs", vpc_name, mutate)


async def list_vpc_peerings(k8s, vpc_name: str) -> list[tuple[str, str]]:
    """Every peering touching `vpc_name`, as (local_vpc, remote_vpc) pairs.

    Both directions are reported, so a half-declared peering stays visible
    instead of silently looking like none at all. Each VPC only knows its own
    side, hence the full listing.
    """
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="vpcs",
        )
    except ApiException as e:
        logger.warning(f"Failed to list VPCs while collecting peerings: {e}")
        return []

    pairs = []
    for item in result.get("items", []):
        local = item.get("metadata", {}).get("name", "")
        for p in item.get("spec", {}).get("vpcPeerings") or []:
            remote = p.get("remoteVpc", "")
            if vpc_name in (local, remote):
                pairs.append((local, remote))
    return pairs
