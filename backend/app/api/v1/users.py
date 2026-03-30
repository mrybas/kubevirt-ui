"""Users and Groups API endpoints.

Manages users and groups via LLDAP GraphQL API (bundled mode).
When LLDAP is disabled (external IdP), write operations return 501.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import User, require_auth
from app.core.groups import is_admin
from app.core.lldap_client import LLDAP_ENABLED, get_lldap_client, LLDAPError

logger = logging.getLogger(__name__)

users_router = APIRouter()
groups_router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    created: str | None = None
    groups: list[dict[str, Any]] = []


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    lldap_enabled: bool = LLDAP_ENABLED


class CreateUserRequest(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    password: str


class UpdateUserRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class SetPasswordRequest(BaseModel):
    password: str


class GroupResponse(BaseModel):
    id: int
    display_name: str
    created: str | None = None
    member_count: int = 0
    members: list[dict[str, Any]] = []


class GroupListResponse(BaseModel):
    items: list[GroupResponse]
    total: int
    lldap_enabled: bool = LLDAP_ENABLED


class CreateGroupRequest(BaseModel):
    name: str


class AddMemberRequest(BaseModel):
    user_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_admin(user: User) -> None:
    """Raise 403 if user is not an admin."""
    if not is_admin(user.groups):
        raise HTTPException(status_code=403, detail="Admin access required")


def _require_lldap() -> None:
    """Raise 501 if LLDAP is not enabled."""
    if not LLDAP_ENABLED:
        raise HTTPException(
            status_code=501,
            detail="User management not available — using external IdP",
        )


def _user_response(u: dict) -> UserResponse:
    return UserResponse(
        id=u.get("id", ""),
        email=u.get("email", ""),
        display_name=u.get("displayName", u.get("display_name", "")),
        created=u.get("creationDate"),
        groups=[
            {"id": g.get("id"), "display_name": g.get("displayName", "")}
            for g in u.get("groups", [])
        ],
    )


def _group_response(g: dict) -> GroupResponse:
    members = g.get("users", [])
    return GroupResponse(
        id=g.get("id", 0),
        display_name=g.get("displayName", ""),
        created=g.get("creationDate"),
        member_count=len(members),
        members=[
            {"id": m.get("id", ""), "email": m.get("email", ""), "display_name": m.get("displayName", "")}
            for m in members
        ],
    )


# ---------------------------------------------------------------------------
# Users endpoints
# ---------------------------------------------------------------------------

@users_router.get("", response_model=UserListResponse)
async def list_users(user: User = Depends(require_auth)):
    """List all users."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        users = await client.list_users()
        items = [_user_response(u) for u in users]
        return UserListResponse(items=items, total=len(items))
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=502, detail="Directory service error")
    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to LLDAP")


@users_router.get("/{user_id}", response_model=UserResponse)
async def get_user_detail(user_id: str, user: User = Depends(require_auth)):
    """Get a single user."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        u = await client.get_user(user_id)
        return _user_response(u)
    except LLDAPError as e:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
    except Exception as e:
        logger.error(f"Failed to get user {user_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to LLDAP")


@users_router.post("", response_model=UserResponse, status_code=201)
async def create_user(body: CreateUserRequest, user: User = Depends(require_auth)):
    """Create a new user."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        created = await client.create_user(
            user_id=body.id,
            email=body.email,
            display_name=body.display_name,
            first_name=body.first_name,
            last_name=body.last_name,
        )
        # Set password via LDAP protocol (GraphQL doesn't support it)
        pwd_ok = await client.set_password(body.id, body.password)
        if not pwd_ok:
            logger.warning(f"User {body.id} created but password set failed")
        logger.info(f"Created user: {body.id}")
        return _user_response(created)
    except LLDAPError as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"User '{body.id}' already exists")
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")


@users_router.put("/{user_id}", response_model=UserResponse)
async def update_user(user_id: str, body: UpdateUserRequest, user: User = Depends(require_auth)):
    """Update a user."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        await client.update_user(
            user_id=user_id,
            email=body.email,
            display_name=body.display_name,
            first_name=body.first_name,
            last_name=body.last_name,
        )
        updated = await client.get_user(user_id)
        logger.info(f"Updated user: {user_id}")
        return _user_response(updated)
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")


@users_router.post("/{user_id}/password", status_code=204)
async def reset_password(user_id: str, body: SetPasswordRequest, user: User = Depends(require_auth)):
    """Reset a user's password."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        ok = await client.set_password(user_id, body.password)
        if not ok:
            raise HTTPException(status_code=502, detail="Failed to set password via LDAP")
        logger.info(f"Password reset for user: {user_id}")
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reset password for {user_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to LLDAP")


@users_router.post("/{user_id}/disable", status_code=204)
async def disable_user(user_id: str, user: User = Depends(require_auth)):
    """Disable a user by adding to disabled-users group."""
    _require_admin(user)
    _require_lldap()

    if user_id == "admin":
        raise HTTPException(status_code=400, detail="Cannot disable the admin user")

    try:
        client = get_lldap_client()
        groups = await client.list_groups()
        disabled_group = next((g for g in groups if g["displayName"] == "disabled-users"), None)
        if not disabled_group:
            raise HTTPException(status_code=500, detail="disabled-users group not found")
        await client.add_user_to_group(user_id, disabled_group["id"])
        logger.info(f"Disabled user: {user_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to disable user {user_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to disable user")


@users_router.post("/{user_id}/enable", status_code=204)
async def enable_user(user_id: str, user: User = Depends(require_auth)):
    """Enable a user by removing from disabled-users group."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        groups = await client.list_groups()
        disabled_group = next((g for g in groups if g["displayName"] == "disabled-users"), None)
        if not disabled_group:
            raise HTTPException(status_code=500, detail="disabled-users group not found")
        await client.remove_user_from_group(user_id, disabled_group["id"])
        logger.info(f"Enabled user: {user_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to enable user {user_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to enable user")


@users_router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: str, user: User = Depends(require_auth)):
    """Delete a user."""
    _require_admin(user)
    _require_lldap()

    if user_id == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the admin user")

    try:
        client = get_lldap_client()
        await client.delete_user(user_id)
        logger.info(f"Deleted user: {user_id}")
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")


# ---------------------------------------------------------------------------
# Groups endpoints
# ---------------------------------------------------------------------------

@groups_router.get("", response_model=GroupListResponse)
async def list_groups(user: User = Depends(require_auth)):
    """List all groups."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        groups = await client.list_groups()
        items = [_group_response(g) for g in groups]
        return GroupListResponse(items=items, total=len(items))
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=502, detail="Directory service error")
    except Exception as e:
        logger.error(f"Failed to list groups: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to LLDAP")


@groups_router.get("/{group_id}", response_model=GroupResponse)
async def get_group_detail(group_id: int, user: User = Depends(require_auth)):
    """Get a single group."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        g = await client.get_group(group_id)
        return _group_response(g)
    except LLDAPError as e:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")


@groups_router.post("", response_model=GroupResponse, status_code=201)
async def create_group(body: CreateGroupRequest, user: User = Depends(require_auth)):
    """Create a new group."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        created = await client.create_group(body.name)
        logger.info(f"Created group: {body.name}")
        return _group_response(created)
    except LLDAPError as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Group '{body.name}' already exists")
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")


@groups_router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: int, user: User = Depends(require_auth)):
    """Delete a group."""
    _require_admin(user)
    _require_lldap()

    # Protect the lldap_admin group (id=1 typically)
    if group_id == 1:
        raise HTTPException(status_code=400, detail="Cannot delete the system admin group")

    try:
        client = get_lldap_client()
        await client.delete_group(group_id)
        logger.info(f"Deleted group: {group_id}")
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")


@groups_router.post("/{group_id}/members", status_code=201)
async def add_member(group_id: int, body: AddMemberRequest, user: User = Depends(require_auth)):
    """Add a user to a group."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        await client.add_user_to_group(body.user_id, group_id)
        logger.info(f"Added {body.user_id} to group {group_id}")
        return {"ok": True}
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")


@groups_router.delete("/{group_id}/members/{member_id}", status_code=204)
async def remove_member(group_id: int, member_id: str, user: User = Depends(require_auth)):
    """Remove a user from a group."""
    _require_admin(user)
    _require_lldap()

    try:
        client = get_lldap_client()
        await client.remove_user_from_group(member_id, group_id)
        logger.info(f"Removed {member_id} from group {group_id}")
    except LLDAPError as e:
        logger.warning(f"LLDAP error: {e}")
        raise HTTPException(status_code=400, detail="Operation failed")
