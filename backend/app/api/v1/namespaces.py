"""Namespace API endpoints."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from kubernetes_asyncio.client import ApiException

from app.core.auth import User, require_auth
from app.core.groups import get_user_namespaces, is_admin as is_admin_user
from app.models.namespace import NamespaceListResponse, NamespaceResponse

router = APIRouter()
logger = logging.getLogger(__name__)

# Label for enabled namespaces
PROJECT_ENABLED_LABEL = "kubevirt-ui.io/enabled"
# Present only on per-tenant "service" namespaces (tenant-<name>) that host a
# single Kamaji control plane + its worker VMs. These must not appear in
# project/template/VM/new-tenant pickers — this endpoint is the picker source.
TENANT_LABEL = "kubevirt-ui.io/tenant"


@router.get("", response_model=NamespaceListResponse)
async def list_namespaces(
    request: Request,
    include_all: bool = Query(
        False,
        description="Include all namespaces (admin only). By default, only enabled namespaces are shown.",
    ),
    include_tenants: bool = Query(
        False,
        description=(
            "Include per-tenant service namespaces (tenant-<name>). Excluded "
            "by default — they host a single tenant control plane and must "
            "not be selectable as projects. Worker-VM visibility in /vms is "
            "unaffected (that path uses get_user_namespaces, not this endpoint)."
        ),
    ),
    user: User = Depends(require_auth),
) -> NamespaceListResponse:
    """
    List namespaces accessible to the user.

    By default, only namespaces with `kubevirt-ui.io/enabled=true` are shown,
    and per-tenant service namespaces (`kubevirt-ui.io/tenant` label) are
    excluded. Admins can pass `include_all=true` (all namespaces) or
    `include_tenants=true` (also list tenant namespaces).
    """
    k8s_client = request.app.state.k8s_client

    try:
        # Check if user is admin (group name configurable via ADMIN_GROUPS env)
        is_admin = is_admin_user(user.groups)

        if include_all and is_admin:
            # Admin requested all namespaces
            namespaces = await k8s_client.list_namespaces()
        else:
            # Default: enabled namespaces, minus tenant service namespaces.
            # The negative match (`!label`) is evaluated server-side.
            selector = f"{PROJECT_ENABLED_LABEL}=true"
            if not include_tenants:
                selector += f",!{TENANT_LABEL}"
            namespaces = await k8s_client.list_namespaces(label_selector=selector)

        # RBAC: non-admin users only see namespaces they have access to
        if not is_admin and not include_all:
            allowed = set(await get_user_namespaces(k8s_client, user))
            namespaces = [ns for ns in namespaces if ns["name"] in allowed]

        ns_responses = [
            NamespaceResponse(
                name=ns["name"],
                status=ns["status"],
                labels=ns["labels"],
                created=ns["created"],
            )
            for ns in namespaces
        ]

        return NamespaceListResponse(items=ns_responses, total=len(ns_responses))

    except ApiException as e:
        logger.error(f"Failed to list namespaces: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list namespaces: {e.reason}",
        )
