"""Subnet ACL Management API endpoints (Kube-OVN).

Manages ACL rules on Kube-OVN subnets. Each ACL is a rule in subnet.spec.acls[].
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.core.auth import User, require_auth
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
    user: User = Depends(require_auth),
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
    user: User = Depends(require_auth),
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
    raw_acls.append(new_acl)

    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            name=name, body={"spec": {"acls": raw_acls}},
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "subnet ACL operation")

    acls = _parse_acls(raw_acls)
    return SubnetAclListResponse(
        subnet=name, cidr_block=cidr_block,
        acls=acls, total=len(acls),
    )


@router.delete("/{name}/acls/{index}")
async def delete_subnet_acl(
    request: Request, name: str, index: int,
    user: User = Depends(require_auth),
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

    removed = raw_acls.pop(index)

    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            name=name, body={"spec": {"acls": raw_acls}},
            _content_type="application/merge-patch+json",
        )
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
