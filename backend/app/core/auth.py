"""Authentication module for KubeVirt UI."""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

# Auth configuration from environment
AUTH_TYPE = os.getenv("AUTH_TYPE", "none")  # none, oidc, token
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")  # Public URL (for frontend/token validation)
OIDC_INTERNAL_URL = os.getenv("OIDC_INTERNAL_URL", "")  # Internal URL for backend->DEX communication
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "kubevirt-ui")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")


@dataclass
class User:
    """Authenticated user."""

    id: str
    email: str
    username: str
    groups: list[str]
    raw_token: str | None = None


@dataclass
class AuthConfig:
    """Authentication configuration for frontend."""

    type: str
    issuer: str | None = None
    client_id: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    user_management: str = "none"  # "lldap", "external", "none"


# OIDC discovery cache
_oidc_config: dict[str, Any] | None = None
_oidc_config_fetched_at: float = 0
OIDC_CACHE_TTL = 3600  # 1 hour


def get_internal_url(public_url: str) -> str:
    """Get internal URL for OIDC provider communication.
    
    If OIDC_INTERNAL_URL is set, replace the host in the URL.
    This allows backend to communicate with DEX internally (http://dex:5556)
    while using the public issuer URL (http://localhost:5556) for validation.
    """
    if not OIDC_INTERNAL_URL:
        return public_url
    
    # Replace the base URL with internal URL
    from urllib.parse import urlparse, urlunparse
    
    public_parsed = urlparse(public_url)
    internal_parsed = urlparse(OIDC_INTERNAL_URL)
    
    # Keep the path from the public URL, use host/port from internal
    internal_url = urlunparse((
        internal_parsed.scheme,
        internal_parsed.netloc,
        public_parsed.path,
        public_parsed.params,
        public_parsed.query,
        public_parsed.fragment,
    ))
    
    return internal_url


async def get_oidc_config() -> dict[str, Any]:
    """Fetch and cache OIDC discovery document."""
    global _oidc_config, _oidc_config_fetched_at

    if _oidc_config is not None and (time.time() - _oidc_config_fetched_at) < OIDC_CACHE_TTL:
        return _oidc_config

    if not OIDC_ISSUER:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OIDC issuer not configured",
        )

    # Use internal URL for discovery if configured
    discovery_base = OIDC_INTERNAL_URL or OIDC_ISSUER
    discovery_url = f"{discovery_base}/.well-known/openid-configuration"
    logger.info(f"Fetching OIDC discovery from {discovery_url}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            _oidc_config = response.json()
            _oidc_config_fetched_at = time.time()
            return _oidc_config
    except Exception as e:
        logger.error(f"Failed to fetch OIDC config: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to OIDC provider: {e}",
        )


async def get_auth_config() -> AuthConfig:
    """Get authentication configuration for frontend."""
    from app.core.lldap_client import LLDAP_ENABLED
    user_mgmt = "lldap" if LLDAP_ENABLED else "external" if AUTH_TYPE == "oidc" else "none"

    if AUTH_TYPE == "none":
        return AuthConfig(type="none", user_management=user_mgmt)

    if AUTH_TYPE == "token":
        return AuthConfig(type="token", user_management=user_mgmt)

    if AUTH_TYPE == "oidc":
        try:
            oidc_config = await get_oidc_config()
            return AuthConfig(
                type="oidc",
                issuer=OIDC_ISSUER,
                client_id=OIDC_CLIENT_ID,
                authorization_endpoint=oidc_config.get("authorization_endpoint"),
                token_endpoint=oidc_config.get("token_endpoint"),
                userinfo_endpoint=oidc_config.get("userinfo_endpoint"),
                user_management=user_mgmt,
            )
        except HTTPException:
            return AuthConfig(
                type="oidc",
                issuer=OIDC_ISSUER,
                client_id=OIDC_CLIENT_ID,
                user_management=user_mgmt,
            )

    return AuthConfig(type="none", user_management=user_mgmt)


# Security scheme
security = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> User | None:
    """Get current authenticated user from request."""
    if AUTH_TYPE == "none":
        # No auth - return anonymous admin for development
        return User(
            id="anonymous",
            email="anonymous@local",
            username="anonymous",
            groups=["kubevirt-ui-admins"],
        )

    if credentials is None:
        return None

    token = credentials.credentials

    if AUTH_TYPE == "token":
        # Validate ServiceAccount token against K8s API
        return await validate_k8s_token(request, token)

    if AUTH_TYPE == "oidc":
        # Validate OIDC token
        return await validate_oidc_token(token)

    return None


async def require_auth(
    user: User | None = Depends(get_current_user),
) -> User:
    """Require authenticated user."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if "disabled-users" in user.groups:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return user


async def validate_k8s_token(request: Request, token: str) -> User | None:
    """Validate Kubernetes ServiceAccount token."""
    try:
        k8s_client = request.app.state.k8s_client

        # Use TokenReview to validate token
        from kubernetes_asyncio.client import AuthenticationV1Api, V1TokenReview, V1TokenReviewSpec

        auth_api = AuthenticationV1Api(k8s_client._api_client)
        review = V1TokenReview(spec=V1TokenReviewSpec(token=token))
        result = await auth_api.create_token_review(review)

        if not result.status.authenticated:
            return None

        user_info = result.status.user
        return User(
            id=user_info.uid or user_info.username,
            email=f"{user_info.username}@serviceaccount",
            username=user_info.username,
            groups=user_info.groups or [],
            raw_token=token,
        )
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        return None


async def validate_oidc_token(token: str) -> User | None:
    """Validate OIDC token."""
    try:
        oidc_config = await get_oidc_config()
        userinfo_endpoint = oidc_config.get("userinfo_endpoint")

        if not userinfo_endpoint:
            logger.error("No userinfo endpoint in OIDC config")
            return None

        # Use internal URL if configured
        internal_userinfo = get_internal_url(userinfo_endpoint)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                internal_userinfo,
                headers={"Authorization": f"Bearer {token}"},
            )

            if response.status_code != 200:
                logger.warning(f"OIDC userinfo failed: {response.status_code}")
                return None

            user_info = response.json()

            return User(
                id=user_info.get("sub", ""),
                email=user_info.get("email", ""),
                username=user_info.get("preferred_username") or user_info.get("name", ""),
                groups=user_info.get("groups", []),
                raw_token=token,
            )
    except Exception as e:
        logger.error(f"OIDC token validation failed: {e}")
        return None


async def check_namespace_access(
    request: Request, user: User, namespace: str, verb: str = "get"
) -> bool:
    """Check if user has access to namespace using SubjectAccessReview."""
    if AUTH_TYPE == "none":
        return True  # No auth = full access

    try:
        k8s_client = request.app.state.k8s_client
        from kubernetes_asyncio.client import (
            AuthorizationV1Api,
            V1ResourceAttributes,
            V1SubjectAccessReview,
            V1SubjectAccessReviewSpec,
        )

        auth_api = AuthorizationV1Api(k8s_client._api_client)

        spec = V1SubjectAccessReviewSpec(
            user=user.email,
            groups=user.groups,
            resource_attributes=V1ResourceAttributes(
                namespace=namespace,
                verb=verb,
                resource="virtualmachines",
                group="kubevirt.io",
            ),
        )

        review = V1SubjectAccessReview(spec=spec)
        result = await auth_api.create_subject_access_review(review)

        return result.status.allowed
    except Exception as e:
        logger.error(f"SubjectAccessReview failed: {e}")
        return False


async def check_folder_access(
    request: Request, user: User, folder_name: str, verb: str = "get",
) -> bool:
    """Check if user has access to a folder by walking up the folder tree.

    Access is granted if the user has a RoleBinding in any environment namespace
    belonging to this folder or any of its ancestors.
    """
    if AUTH_TYPE == "none":
        return True

    from app.core.groups import is_admin
    if is_admin(user.groups):
        return True

    try:
        k8s_client = request.app.state.k8s_client
        from kubernetes_asyncio.client.rest import ApiException
        import json

        # Load folder tree from ConfigMap
        FOLDERS_CONFIGMAP = "kubevirt-ui-folders"
        SYSTEM_NAMESPACE = "kubevirt-ui-system"
        try:
            cm = await k8s_client.core_api.read_namespaced_config_map(
                name=FOLDERS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
            )
            data = cm.data or {}
        except ApiException:
            return False

        # Parse folders
        folders: dict[str, dict] = {}
        for name, raw in data.items():
            try:
                folders[name] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                folders[name] = {}

        if folder_name not in folders:
            return False

        # Build ancestor chain (from folder up to root)
        chain = [folder_name]
        visited: set[str] = {folder_name}
        current = folder_name
        while True:
            parent = folders.get(current, {}).get("parent_id")
            if not parent or parent in visited:
                break
            chain.append(parent)
            visited.add(parent)
            current = parent

        # Check SAR in any namespace belonging to any folder in the chain
        for fname in chain:
            try:
                ns_list = await k8s_client.core_api.list_namespace(
                    label_selector=f"kubevirt-ui.io/folder={fname},kubevirt-ui.io/managed=true",
                )
                for ns in ns_list.items:
                    if await check_namespace_access(request, user, ns.metadata.name, verb):
                        return True
            except ApiException:
                continue

        return False
    except Exception as e:
        logger.warning(f"Folder access check failed for user: {e}")
        return False  # Deny access on check failure (fail-closed)
