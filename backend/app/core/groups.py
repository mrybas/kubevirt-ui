"""Group management for KubeVirt UI.

Groups come from LLDAP (bundled mode) or OIDC provider (external IdP mode).
"""

import logging
from typing import Any

from app.core.lldap_client import LLDAP_ENABLED, get_lldap_client

logger = logging.getLogger(__name__)

# Fallback teams when LLDAP is unavailable
_FALLBACK_TEAMS = [
    {
        "name": "kubevirt-ui-admins",
        "display_name": "Platform Admins",
        "description": "Full cluster access",
    },
    {
        "name": "developers",
        "display_name": "Developers",
        "description": "Development staff",
    },
]


def get_user_groups(email: str, oidc_groups: list[str] | None = None) -> list[str]:
    """Get groups for a user.
    
    Groups come from the OIDC token (populated by DEX from LLDAP/external LDAP).
    """
    if oidc_groups:
        return oidc_groups
    return []


async def get_known_teams_async() -> list[dict[str, Any]]:
    """Get list of known teams from LLDAP (async version)."""
    if not LLDAP_ENABLED:
        return _FALLBACK_TEAMS

    try:
        client = get_lldap_client()
        groups = await client.list_groups()
        return [
            {
                "name": g.get("displayName", ""),
                "display_name": g.get("displayName", ""),
                "description": f"{len(g.get('users', []))} members",
            }
            for g in groups
            if g.get("displayName") != "lldap_admin"
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch teams from LLDAP: {e}")
        return _FALLBACK_TEAMS


def get_known_teams() -> list[dict[str, Any]]:
    """Get list of known teams (sync fallback)."""
    return _FALLBACK_TEAMS


def is_admin(groups: list[str]) -> bool:
    """Check if user is platform admin."""
    return "kubevirt-ui-admins" in groups


async def get_user_namespaces(k8s_client: Any, user: Any) -> list[str]:
    """Return list of enabled namespaces accessible to the user.

    Admins get all enabled namespaces.  Regular users get only those where
    a managed RoleBinding references their email or one of their groups.
    """
    from kubernetes_asyncio.client import RbacAuthorizationV1Api

    # All enabled project namespaces
    all_ns = await k8s_client.list_namespaces(
        label_selector="kubevirt-ui.io/enabled=true"
    )
    all_ns_names = [ns["name"] for ns in all_ns]

    if is_admin(user.groups):
        return all_ns_names

    import asyncio
    rbac_api = RbacAuthorizationV1Api(k8s_client._api_client)

    async def _check_ns(ns: str) -> str | None:
        try:
            bindings = await rbac_api.list_namespaced_role_binding(
                namespace=ns,
                label_selector="kubevirt-ui.io/managed=true",
            )
            for b in bindings.items:
                for subj in (b.subjects or []):
                    if subj.kind == "User" and subj.name == user.email:
                        return ns
                    if subj.kind == "Group" and subj.name in user.groups:
                        return ns
        except Exception as e:
            logger.debug(f"Failed to list RoleBindings in {ns}: {e}")
        return None

    results = await asyncio.gather(*[_check_ns(ns) for ns in all_ns_names])
    return sorted(ns for ns in results if ns is not None)
