"""Namespace API endpoints."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from kubernetes_asyncio.client import ApiException

from app.core.auth import User, require_auth
from app.core.groups import get_user_namespaces
from app.models.namespace import NamespaceListResponse, NamespaceResponse

router = APIRouter()
logger = logging.getLogger(__name__)

# Label for enabled namespaces
PROJECT_ENABLED_LABEL = "kubevirt-ui.io/enabled"


@router.get("", response_model=NamespaceListResponse)
async def list_namespaces(
    request: Request,
    include_all: bool = Query(
        False,
        description="Include all namespaces (admin only). By default, only enabled namespaces are shown.",
    ),
    user: User = Depends(require_auth),
) -> NamespaceListResponse:
    """
    List namespaces accessible to the user.
    
    By default, only namespaces with `kubevirt-ui.io/enabled=true` label are shown.
    Admins can use `include_all=true` to see all namespaces.
    """
    k8s_client = request.app.state.k8s_client

    try:
        # Check if user is admin (has admin group)
        is_admin = "kubevirt-ui-admins" in user.groups
        
        if include_all and is_admin:
            # Admin requested all namespaces
            namespaces = await k8s_client.list_namespaces()
        else:
            # Default: only show enabled namespaces
            namespaces = await k8s_client.list_namespaces(
                label_selector=f"{PROJECT_ENABLED_LABEL}=true"
            )

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
