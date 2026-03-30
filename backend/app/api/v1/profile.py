"""User Profile API endpoints.

Stores per-user settings (SSH keys, preferences) in a shared ConfigMap
in the kubevirt-ui-system namespace.
"""

import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request, Depends, status
from kubernetes_asyncio.client.rest import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth

logger = logging.getLogger(__name__)
router = APIRouter()

# Storage config
PROFILE_CONFIGMAP = "kubevirt-ui-user-settings"
PROFILE_NAMESPACE = "kubevirt-ui-system"

# Valid SSH key prefixes
SSH_KEY_PREFIXES = (
    "ssh-rsa",
    "ssh-ed25519",
    "ssh-dss",
    "ecdsa-sha2-",
    "sk-ssh-ed25519",
    "sk-ecdsa-sha2-",
)


class ProfileResponse(BaseModel):
    """User profile data."""

    email: str
    ssh_public_keys: list[str] = []


class UpdateSSHKeysRequest(BaseModel):
    """Request to update SSH public keys."""

    ssh_public_keys: list[str] = Field(
        default=[],
        description="List of SSH public keys (one per entry)",
    )


def _validate_ssh_key(key: str) -> bool:
    """Validate that a string looks like an SSH public key."""
    key = key.strip()
    if not key or key.startswith("#"):
        return True  # Empty lines and comments are OK (will be filtered)
    return any(key.startswith(prefix) for prefix in SSH_KEY_PREFIXES)


def _user_key(user: User) -> str:
    """Get ConfigMap data key for a user."""
    # Sanitize email for use as ConfigMap key (replace @ and . with -)
    return re.sub(r"[^a-zA-Z0-9_-]", "-", user.email)


async def _ensure_configmap(k8s_client) -> dict:
    """Get or create the user settings ConfigMap."""
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=PROFILE_CONFIGMAP,
            namespace=PROFILE_NAMESPACE,
        )
        return cm
    except ApiException as e:
        if e.status == 404:
            # Create the ConfigMap
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": PROFILE_CONFIGMAP,
                    "namespace": PROFILE_NAMESPACE,
                    "labels": {"kubevirt-ui.io/managed": "true"},
                },
                "data": {},
            }
            cm = await k8s_client.core_api.create_namespaced_config_map(
                namespace=PROFILE_NAMESPACE, body=body
            )
            logger.info(f"Created user settings ConfigMap: {PROFILE_CONFIGMAP}")
            return cm
        raise


async def _get_user_data(k8s_client, user: User) -> dict:
    """Get user data from ConfigMap."""
    cm = await _ensure_configmap(k8s_client)
    data = cm.data or {}
    key = _user_key(user)
    raw = data.get(key)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON for user {key}, resetting")
    return {}


async def _set_user_data(k8s_client, user: User, user_data: dict) -> None:
    """Set user data in ConfigMap."""
    await _ensure_configmap(k8s_client)
    key = _user_key(user)
    patch = {
        "data": {key: json.dumps(user_data)},
    }
    await k8s_client.core_api.patch_namespaced_config_map(
        name=PROFILE_CONFIGMAP,
        namespace=PROFILE_NAMESPACE,
        body=patch,
    )


@router.get("", response_model=ProfileResponse)
async def get_profile(request: Request, user: User = Depends(require_auth)):
    """Get current user's profile."""
    k8s_client = request.app.state.k8s_client

    try:
        data = await _get_user_data(k8s_client, user)
        return ProfileResponse(
            email=user.email,
            ssh_public_keys=data.get("ssh_public_keys", []),
        )
    except ApiException as e:
        logger.error(f"Failed to get profile: {e}")
        raise HTTPException(status_code=e.status, detail=str(e.reason))


@router.put("/ssh-keys", response_model=ProfileResponse)
async def update_ssh_keys(
    request: Request,
    body: UpdateSSHKeysRequest,
    user: User = Depends(require_auth),
):
    """Update current user's SSH public keys."""
    k8s_client = request.app.state.k8s_client

    # Validate and clean keys
    clean_keys = []
    for key in body.ssh_public_keys:
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        if not _validate_ssh_key(key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid SSH public key: must start with one of {', '.join(SSH_KEY_PREFIXES)}",
            )
        clean_keys.append(key)

    try:
        data = await _get_user_data(k8s_client, user)
        data["ssh_public_keys"] = clean_keys
        await _set_user_data(k8s_client, user, data)

        return ProfileResponse(
            email=user.email,
            ssh_public_keys=clean_keys,
        )
    except ApiException as e:
        logger.error(f"Failed to update SSH keys: {e}")
        raise HTTPException(status_code=e.status, detail=str(e.reason))


async def get_user_ssh_keys(k8s_client, user: User) -> list[str]:
    """Get SSH keys for a user (used by VM creation).
    
    Returns empty list if no keys configured or on error.
    """
    try:
        data = await _get_user_data(k8s_client, user)
        return data.get("ssh_public_keys", [])
    except Exception as e:
        logger.warning(f"Failed to get SSH keys for user {user.email}: {e}")
        return []
