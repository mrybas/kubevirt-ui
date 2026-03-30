"""LLDAP GraphQL client for user/group management.

When LLDAP_ENABLED=true (bundled mode), the backend manages users and groups
via LLDAP's GraphQL API.  When false (external IdP mode), all write operations
return 501.
"""

import asyncio
import logging
import os
from functools import partial
from typing import Any

import httpx
import ldap3

logger = logging.getLogger(__name__)

# Configuration from environment
LLDAP_ENABLED = os.getenv("LLDAP_ENABLED", "false").lower() in ("true", "1", "yes")
LLDAP_URL = os.getenv("LLDAP_URL", "http://lldap:17170")
LLDAP_ADMIN_USER = os.getenv("LLDAP_ADMIN_USER", "admin")
LLDAP_ADMIN_PASSWORD = os.getenv("LLDAP_ADMIN_PASSWORD", "admin_password")
LLDAP_BASE_DN = os.getenv("LLDAP_LDAP_BASE_DN", "dc=kubevirt,dc=local")

LLDAP_LDAP_HOST = os.getenv("LLDAP_LDAP_HOST", "lldap")
LLDAP_LDAP_PORT = int(os.getenv("LLDAP_LDAP_PORT", "3890"))

GRAPHQL_ENDPOINT = f"{LLDAP_URL}/api/graphql"
LOGIN_ENDPOINT = f"{LLDAP_URL}/auth/simple/login"


class LLDAPClient:
    """Async client for LLDAP GraphQL API."""

    def __init__(self) -> None:
        self._token: str | None = None

    async def _authenticate(self) -> str:
        """Get JWT token from LLDAP."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                LOGIN_ENDPOINT,
                json={"username": LLDAP_ADMIN_USER, "password": LLDAP_ADMIN_PASSWORD},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["token"]
            return self._token

    async def _get_token(self) -> str:
        """Return cached token or authenticate."""
        if self._token:
            return self._token
        return await self._authenticate()

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query/mutation."""
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                GRAPHQL_ENDPOINT,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401:
                # Token expired — re-authenticate and retry
                token = await self._authenticate()
                resp = await client.post(
                    GRAPHQL_ENDPOINT,
                    json={"query": query, "variables": variables or {}},
                    headers={"Authorization": f"Bearer {token}"},
                )
            resp.raise_for_status()
            result = resp.json()
            if "errors" in result:
                raise LLDAPError(result["errors"])
            return result.get("data", {})

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def list_users(self) -> list[dict[str, Any]]:
        """List all users."""
        query = """
        {
            users {
                id
                email
                displayName
                creationDate
                groups {
                    id
                    displayName
                }
            }
        }
        """
        data = await self._graphql(query)
        return data.get("users", [])

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """Get a single user by ID."""
        query = """
        query GetUser($userId: String!) {
            user(userId: $userId) {
                id
                email
                displayName
                firstName
                lastName
                creationDate
                groups {
                    id
                    displayName
                }
            }
        }
        """
        data = await self._graphql(query, {"userId": user_id})
        return data.get("user", {})

    async def create_user(
        self,
        user_id: str,
        email: str,
        display_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a new user."""
        mutation = """
        mutation CreateUser($user: CreateUserInput!) {
            createUser(user: $user) {
                id
                email
                displayName
                creationDate
            }
        }
        """
        user_input: dict[str, Any] = {"id": user_id, "email": email}
        if display_name:
            user_input["displayName"] = display_name
        if first_name:
            user_input["firstName"] = first_name
        if last_name:
            user_input["lastName"] = last_name
        data = await self._graphql(mutation, {"user": user_input})
        return data.get("createUser", {})

    async def update_user(
        self,
        user_id: str,
        email: str | None = None,
        display_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> bool:
        """Update user fields."""
        mutation = """
        mutation UpdateUser($user: UpdateUserInput!) {
            updateUser(user: $user) {
                ok
            }
        }
        """
        user_input: dict[str, Any] = {"id": user_id}
        if email is not None:
            user_input["email"] = email
        if display_name is not None:
            user_input["displayName"] = display_name
        if first_name is not None:
            user_input["firstName"] = first_name
        if last_name is not None:
            user_input["lastName"] = last_name
        data = await self._graphql(mutation, {"user": user_input})
        return data.get("updateUser", {}).get("ok", False)

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user."""
        mutation = """
        mutation DeleteUser($userId: String!) {
            deleteUser(userId: $userId) {
                ok
            }
        }
        """
        data = await self._graphql(mutation, {"userId": user_id})
        return data.get("deleteUser", {}).get("ok", False)

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    async def list_groups(self) -> list[dict[str, Any]]:
        """List all groups."""
        query = """
        {
            groups {
                id
                displayName
                creationDate
                users {
                    id
                    email
                    displayName
                }
            }
        }
        """
        data = await self._graphql(query)
        return data.get("groups", [])

    async def get_group(self, group_id: int) -> dict[str, Any]:
        """Get a single group by ID."""
        query = """
        query GetGroup($groupId: Int!) {
            group(groupId: $groupId) {
                id
                displayName
                creationDate
                users {
                    id
                    email
                    displayName
                }
            }
        }
        """
        data = await self._graphql(query, {"groupId": group_id})
        return data.get("group", {})

    async def create_group(self, name: str) -> dict[str, Any]:
        """Create a new group."""
        mutation = """
        mutation CreateGroup($name: String!) {
            createGroup(name: $name) {
                id
                displayName
                creationDate
            }
        }
        """
        data = await self._graphql(mutation, {"name": name})
        return data.get("createGroup", {})

    async def delete_group(self, group_id: int) -> bool:
        """Delete a group."""
        mutation = """
        mutation DeleteGroup($groupId: Int!) {
            deleteGroup(groupId: $groupId) {
                ok
            }
        }
        """
        data = await self._graphql(mutation, {"groupId": group_id})
        return data.get("deleteGroup", {}).get("ok", False)

    async def add_user_to_group(self, user_id: str, group_id: int) -> bool:
        """Add a user to a group."""
        mutation = """
        mutation AddUserToGroup($userId: String!, $groupId: Int!) {
            addUserToGroup(userId: $userId, groupId: $groupId) {
                ok
            }
        }
        """
        data = await self._graphql(mutation, {"userId": user_id, "groupId": group_id})
        return data.get("addUserToGroup", {}).get("ok", False)

    async def remove_user_from_group(self, user_id: str, group_id: int) -> bool:
        """Remove a user from a group."""
        mutation = """
        mutation RemoveUserFromGroup($userId: String!, $groupId: Int!) {
            removeUserFromGroup(userId: $userId, groupId: $groupId) {
                ok
            }
        }
        """
        data = await self._graphql(
            mutation, {"userId": user_id, "groupId": group_id}
        )
        return data.get("removeUserFromGroup", {}).get("ok", False)

    # ------------------------------------------------------------------
    # Password management (via LDAP protocol)
    # ------------------------------------------------------------------

    def _ldap_set_password_sync(self, user_id: str, password: str) -> bool:
        """Set password via LDAP modify (synchronous, run in thread)."""
        admin_dn = f"uid={LLDAP_ADMIN_USER},ou=people,{LLDAP_BASE_DN}"
        user_dn = f"uid={user_id},ou=people,{LLDAP_BASE_DN}"
        server = ldap3.Server(LLDAP_LDAP_HOST, port=LLDAP_LDAP_PORT, get_info=ldap3.NONE)
        conn = ldap3.Connection(server, user=admin_dn, password=LLDAP_ADMIN_PASSWORD, auto_bind=True)
        try:
            result = conn.modify(
                user_dn,
                {"userPassword": [(ldap3.MODIFY_REPLACE, [password])]},
            )
            if not result:
                logger.warning(f"LDAP password modify failed for {user_id}: {conn.result}")
            return result
        finally:
            conn.unbind()

    async def set_password(self, user_id: str, password: str) -> bool:
        """Set a user's password via LDAP protocol."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._ldap_set_password_sync, user_id, password)
        )

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    async def seed_initial_data(self) -> None:
        """Create default groups and admin user if they don't exist."""
        logger.info("Seeding LLDAP with initial data...")

        # Default groups
        default_groups = [
            "kubevirt-ui-admins",
            "developers",
            "disabled-users",
        ]

        existing_groups = await self.list_groups()
        existing_names = {g["displayName"] for g in existing_groups}
        group_map: dict[str, int] = {g["displayName"]: g["id"] for g in existing_groups}

        for group_name in default_groups:
            if group_name not in existing_names:
                try:
                    created = await self.create_group(group_name)
                    group_map[group_name] = created["id"]
                    logger.info(f"Created LLDAP group: {group_name}")
                except Exception as e:
                    logger.warning(f"Failed to create group {group_name}: {e}")

        # Ensure admin user has email and is in kubevirt-ui-admins group
        admin_group_id = group_map.get("kubevirt-ui-admins")
        try:
            admin_user = await self.get_user(LLDAP_ADMIN_USER)
            # LLDAP creates admin without email — DEX LDAP connector requires mail attr
            if not admin_user.get("email"):
                await self.update_user(LLDAP_ADMIN_USER, email="admin@kubevirt.local")
                logger.info("Set email for admin user")
            if admin_group_id:
                admin_groups = {g["displayName"] for g in admin_user.get("groups", [])}
                if "kubevirt-ui-admins" not in admin_groups:
                    await self.add_user_to_group(LLDAP_ADMIN_USER, admin_group_id)
                    logger.info(f"Added {LLDAP_ADMIN_USER} to kubevirt-ui-admins")
        except Exception as e:
            logger.warning(f"Failed to configure admin user: {e}")

        logger.info("LLDAP seeding complete")


class LLDAPError(Exception):
    """Error from LLDAP GraphQL API."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        messages = [e.get("message", str(e)) for e in errors]
        super().__init__(f"LLDAP GraphQL errors: {'; '.join(messages)}")


# Singleton instance
_client: LLDAPClient | None = None


def get_lldap_client() -> LLDAPClient:
    """Get or create the LLDAP client singleton."""
    global _client
    if _client is None:
        _client = LLDAPClient()
    return _client
