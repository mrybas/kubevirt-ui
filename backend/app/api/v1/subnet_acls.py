"""Subnet ACL Management API endpoints (Kube-OVN).

Manages ACL rules on Kube-OVN subnets. Each ACL is a rule in subnet.spec.acls[].
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.core.auth import User, require_auth, require_admin
from app.core.cas import patch_spec_with_retry
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.core.errors import k8s_error_to_http

logger = logging.getLogger(__name__)
router = APIRouter()

KUBEOVN_GROUP = KUBEOVN_API_GROUP
KUBEOVN_VERSION = KUBEOVN_API_VERSION


# ============================================================================
# Models (inline — small enough to not need a separate file)
# ============================================================================

from pydantic import BaseModel, Field


class SubnetAcl(BaseModel):
    action: str = Field(..., description="allow-related, allow, drop, reject")
    direction: str = Field(..., description="from-lport (egress from pod), to-lport (ingress to pod)")
    match: str = Field(..., description="OVN match expression")
    priority: int = Field(..., ge=0, le=32767, description="0-32767, higher = evaluated first")


class SubnetAclListResponse(BaseModel):
    subnet: str
    cidr_block: str = ""
    acls: list[SubnetAcl] = []
    total: int = 0


class SubnetAclUpdateRequest(BaseModel):
    acls: list[SubnetAcl]


class SubnetAclAddRequest(BaseModel):
    action: str = Field(..., description="allow-related, allow, drop, reject")
    direction: str = Field(..., description="from-lport or to-lport")
    match: str = Field(..., description="OVN match expression")
    priority: int = Field(..., ge=0, le=32767)


class AclPresetTemplate(BaseModel):
    name: str
    description: str
    acls: list[SubnetAcl]


# ============================================================================
# Helpers
# ============================================================================

async def _get_subnet(k8s, name: str) -> dict[str, Any]:
    """Get a subnet CR or raise 404."""
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Subnet '{name}' not found")
        raise k8s_error_to_http(e, "subnet ACL operation")


def _parse_acls(raw_acls: list[dict]) -> list[SubnetAcl]:
    """Parse raw ACL dicts from subnet spec."""
    return [
        SubnetAcl(
            action=a.get("action", ""),
            direction=a.get("direction", ""),
            match=a.get("match", ""),
            priority=a.get("priority", 0),
        )
        for a in raw_acls
    ]


def _acls_to_spec(acls: list[SubnetAcl]) -> list[dict[str, Any]]:
    """Convert Pydantic ACLs to K8s spec format."""
    return [
        {
            "action": a.action,
            "direction": a.direction,
            "match": a.match,
            "priority": a.priority,
        }
        for a in acls
    ]


# Priorities for the tenant-isolation rule set. Ordering is the whole trick:
# the VPC's own range first, then anything explicitly shared, then a catch-all
# drop that covers only tenant space.
ISOLATION_PRIORITY_OWN = 3200
ISOLATION_PRIORITY_SHARED = 3100
ISOLATION_PRIORITY_DROP = 3000


def build_isolation_acls(
    subnet_cidr: str,
    tenant_supernet: str,
    shared_cidrs: list[str] | None = None,
    peer_cidrs: list[str] | None = None,
) -> list[SubnetAcl]:
    """ACLs that make one VPC unreachable from the other tenants.

    Separate VPCs are separate routing domains, but every tenant prefix is
    announced to the same upstream router, and that router forwards between
    them quite happily — so by default a tenant reaches every other tenant via
    a hairpin through the lab gateway. Isolation has to be switched on when
    the VPC is created, not remembered afterwards.

    The catch-all drop is scoped to `tenant_supernet` — the aggregate all
    tenant VPCs are carved from — so traffic to anything outside it (the
    internet) matches no rule and stays allowed:

        t1 -> internet OK     t1 -> shared OK     t1 -> t2 BLOCKED

    Do NOT reach for `Subnet.spec.private: true` instead. It is the
    obvious-looking knob and it does block tenant-to-tenant, but it also drops
    return traffic from the internet (8.8.8.8 is not in `allowSubnets`), so
    the tenant silently loses egress.

    `peer_cidrs` — the prefixes of the other tenant VPCs — is what the drop is
    actually scoped to. `tenant_supernet` remains as a fallback aggregate.

    Scoping the drop to the supernet alone was a rule about a range no tenant
    was ever in: the allocator hands out `10.{200+N}.0.0/24` while the
    deployment's `TENANT_SUPERNET` was `10.198.192.0/18`. Measured on the
    cluster — `acme-net` 10.100.0.0/24, `beta-net` 10.205.0.0/24, both
    "Isolated", and the only ACL between them dropping 10.198.192.0/18, which
    contains neither. Nothing blocked them; they merely had no route yet, and
    the first BGP announcement would have made "Isolated" a caption.

    Returns an empty list when there is nothing to scope the drop to — a drop
    without one would take the internet with it.
    """
    drop_targets = [c for c in (peer_cidrs or []) if c and c != subnet_cidr]
    if not drop_targets and tenant_supernet:
        drop_targets = [tenant_supernet]
    if not subnet_cidr or not drop_targets:
        return []

    acls = [
        SubnetAcl(
            action="allow-related", direction="from-lport",
            match=f"ip4.dst == {subnet_cidr}", priority=ISOLATION_PRIORITY_OWN,
        ),
        SubnetAcl(
            action="allow-related", direction="to-lport",
            match=f"ip4.src == {subnet_cidr}", priority=ISOLATION_PRIORITY_OWN,
        ),
    ]

    # Each shared prefix gets its own priority so they stay individually
    # identifiable (and removable) rather than collapsing into one band.
    priority = ISOLATION_PRIORITY_SHARED
    for shared in shared_cidrs or []:
        if not shared:
            continue
        acls.append(SubnetAcl(
            action="allow-related", direction="from-lport",
            match=f"ip4.dst == {shared}", priority=priority,
        ))
        acls.append(SubnetAcl(
            action="allow-related", direction="to-lport",
            match=f"ip4.src == {shared}", priority=priority,
        ))
        priority += 1

    for target in drop_targets:
        acls.append(SubnetAcl(
            action="drop", direction="from-lport",
            match=f"ip4.dst == {target}", priority=ISOLATION_PRIORITY_DROP,
        ))
        acls.append(SubnetAcl(
            action="drop", direction="to-lport",
            match=f"ip4.src == {target}", priority=ISOLATION_PRIORITY_DROP,
        ))
    return acls


# Above the isolation band: this one is not an exception anybody carves out.
MGMT_DENY_PRIORITY = 3300


def build_mgmt_deny_acls(mgmt_cidr: str) -> list[SubnetAcl]:
    """Keep the management network from opening connections into a tenant.

    Before B3 this cost nothing to state, because it was already true by
    accident: nothing routed from the node network into a tenant VPC, so a
    kubelet probe into a pod on a custom VPC simply never worked.

    B3 ends that accident. The tenant's prefix is announced and the border
    routes to it, so a node reaches a tenant pod directly — measured, a plain
    `curl` from a node to a pod returned 200. NAT used to do this work
    implicitly; with real addresses in both directions the only thing left
    holding the boundary is this rule.

    One direction only, and deliberately: `to-lport` drops connections coming
    *at* the tenant. Traffic the tenant itself starts toward the management
    network is a different question and is not answered here — dropping the
    replies to its own flows would be the easiest way to break the control
    plane path, which lives on a different network but is reached through the
    same egress.
    """
    if not mgmt_cidr:
        return []
    return [
        SubnetAcl(
            action="drop", direction="to-lport",
            match=f"ip4.src == {mgmt_cidr}", priority=MGMT_DENY_PRIORITY,
        ),
    ]


def _get_preset_templates(subnet_cidr: str = "") -> list[AclPresetTemplate]:
    """Return static ACL preset templates."""
    templates = [
        AclPresetTemplate(
            name="allow-intra-subnet",
            description="Allow traffic within the subnet",
            acls=[SubnetAcl(
                action="allow-related", direction="from-lport",
                match=f"ip4.src == {subnet_cidr}" if subnet_cidr else "ip4.src == $SUBNET_CIDR",
                priority=3000,
            )],
        ),
        AclPresetTemplate(
            name="block-private-nets",
            description="Block traffic to all RFC1918 private networks",
            acls=[
                SubnetAcl(action="drop", direction="from-lport", match="ip4.dst == 10.0.0.0/8", priority=2900),
                SubnetAcl(action="drop", direction="from-lport", match="ip4.dst == 172.16.0.0/12", priority=2900),
                SubnetAcl(action="drop", direction="from-lport", match="ip4.dst == 192.168.0.0/16", priority=2900),
            ],
        ),
        AclPresetTemplate(
            name="allow-dns",
            description="Allow DNS queries (UDP port 53)",
            acls=[SubnetAcl(
                action="allow", direction="from-lport",
                match="udp.dst == 53", priority=2500,
            )],
        ),
        AclPresetTemplate(
            name="default-allow",
            description="Allow all IP traffic (low priority fallback)",
            acls=[SubnetAcl(
                action="allow", direction="from-lport",
                match="ip", priority=1000,
            )],
        ),
    ]
    return templates


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/{name}/acls", response_model=SubnetAclListResponse)
async def get_subnet_acls(
    request: Request, name: str, user: User = Depends(require_auth),
) -> SubnetAclListResponse:
    """Get ACL rules for a subnet."""
    k8s = request.app.state.k8s_client
    subnet = await _get_subnet(k8s, name)

    spec = subnet.get("spec", {})
    raw_acls = spec.get("acls", [])
    acls = _parse_acls(raw_acls)

    return SubnetAclListResponse(
        subnet=name,
        cidr_block=spec.get("cidrBlock", ""),
        acls=acls,
        total=len(acls),
    )


@router.put("/{name}/acls", response_model=SubnetAclListResponse)
async def replace_subnet_acls(
    request: Request, name: str, data: SubnetAclUpdateRequest,
    user: User = Depends(require_admin),
) -> SubnetAclListResponse:
    """Replace all ACL rules on a subnet."""
    k8s = request.app.state.k8s_client
    subnet = await _get_subnet(k8s, name)
    cidr_block = subnet.get("spec", {}).get("cidrBlock", "")

    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            name=name, body={"spec": {"acls": _acls_to_spec(data.acls)}},
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "subnet ACL operation")

    return SubnetAclListResponse(
        subnet=name, cidr_block=cidr_block,
        acls=data.acls, total=len(data.acls),
    )


@router.post("/{name}/acls", response_model=SubnetAclListResponse, status_code=201)
async def add_subnet_acl(
    request: Request, name: str, data: SubnetAclAddRequest,
    user: User = Depends(require_admin),
) -> SubnetAclListResponse:
    """Add a single ACL rule to a subnet."""
    k8s = request.app.state.k8s_client
    subnet = await _get_subnet(k8s, name)

    spec = subnet.get("spec", {})
    cidr_block = spec.get("cidrBlock", "")
    raw_acls = spec.get("acls", [])

    new_acl = {
        "action": data.action,
        "direction": data.direction,
        "match": data.match,
        "priority": data.priority,
    }

    # Append under compare-and-set: `spec.acls` is a list and a merge patch
    # replaces it wholesale, so two rules added at the same time would leave
    # only the second one — with both calls reporting success.
    def mutate(spec: dict) -> dict:
        return {"acls": [*(spec.get("acls") or []), new_acl]}

    try:
        await patch_spec_with_retry(k8s, "subnets", name, mutate)
    except ApiException as e:
        raise k8s_error_to_http(e, "subnet ACL operation")

    raw_acls = (await _get_subnet(k8s, name)).get("spec", {}).get("acls", [])
    acls = _parse_acls(raw_acls)
    return SubnetAclListResponse(
        subnet=name, cidr_block=cidr_block,
        acls=acls, total=len(acls),
    )


@router.delete("/{name}/acls/{index}")
async def delete_subnet_acl(
    request: Request, name: str, index: int,
    user: User = Depends(require_admin),
) -> dict:
    """Remove an ACL rule by index."""
    k8s = request.app.state.k8s_client
    subnet = await _get_subnet(k8s, name)

    raw_acls = subnet.get("spec", {}).get("acls", [])

    if index < 0 or index >= len(raw_acls):
        raise HTTPException(
            status_code=404,
            detail=f"ACL index {index} out of range (subnet has {len(raw_acls)} ACLs)",
        )

    removed = raw_acls[index]

    # Delete by identity, not by index: under compare-and-set the list may have
    # been re-read, and an index computed against the old copy would remove
    # somebody else's rule.
    def mutate(spec: dict) -> dict | None:
        current = list(spec.get("acls") or [])
        if removed not in current:
            return None
        current.remove(removed)
        return {"acls": current}

    try:
        await patch_spec_with_retry(k8s, "subnets", name, mutate)
    except ApiException as e:
        raise k8s_error_to_http(e, "subnet ACL operation")

    return {"status": "deleted", "subnet": name, "removed_acl": removed}


@router.get("/{name}/acls/presets", response_model=list[AclPresetTemplate])
async def get_acl_presets(
    request: Request, name: str, user: User = Depends(require_auth),
) -> list[AclPresetTemplate]:
    """Get ACL preset templates with subnet CIDR substituted."""
    k8s = request.app.state.k8s_client
    subnet = await _get_subnet(k8s, name)
    cidr_block = subnet.get("spec", {}).get("cidrBlock", "")
    return _get_preset_templates(cidr_block)
