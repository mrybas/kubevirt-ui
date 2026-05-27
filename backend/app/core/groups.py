"""Group management for KubeVirt UI.

Groups come from LLDAP (bundled mode) or OIDC provider (external IdP mode).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from app.core.lldap_client import LLDAP_ENABLED, get_lldap_client

if TYPE_CHECKING:  # avoid runtime import cycle (auth.py → groups.py via require_admin)
    from app.core.auth import User

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
    """Check if user is platform admin.

    Two admin signals are honored:
    - Membership in any group from settings.admin_groups (env var ADMIN_GROUPS,
      comma-separated; defaults to "kubevirt-ui-admins").
    - Presence of "system:masters" (Kubernetes' built-in cluster-admin group,
      typically only set for SA/cert tokens — mirrors the kubeconfig endpoint).
    """
    from app.config import get_settings
    admin_groups = set(get_settings().admin_groups_list)
    if "system:masters" in groups:
        return True
    return any(g in admin_groups for g in groups)


# ---------------------------------------------------------------------------
# Phase 2 — folder-level authorization helpers
# ---------------------------------------------------------------------------
#
# Operate on the *parsed folder ConfigMap entry* (a dict) — same shape that
# `_parse_all_folders` in api/v1/folders.py produces. The `access` block is
# optional: missing or empty → only global admins can act on the folder.
#
# Pattern: a higher role implies lower roles (admin → member → viewer).
# Env-level adds onto folder-level (union of groups).

# Labels on env namespaces — duplicated here to avoid importing the
# folders module (would create an import cycle).
ENV_FOLDER_LABEL = "kubevirt-ui.io/folder"
ENV_ENVIRONMENT_LABEL = "kubevirt-ui.io/environment"
FOLDERS_CONFIGMAP = "kubevirt-ui-folders"
SYSTEM_NAMESPACE = "kubevirt-ui-system"


def _access_block(folder_meta: dict) -> dict:
    """Return the `access` sub-dict (or an empty dict if missing)."""
    return folder_meta.get("access") or {}


def _env_access_block(folder_meta: dict, env: str) -> dict:
    """Return the env-level access overrides for `env` (or empty dict)."""
    env_access = _access_block(folder_meta).get("env_access") or {}
    return env_access.get(env) or {}


def _has_group(user_groups: list[str], names: list[str] | None) -> bool:
    if not names:
        return False
    return bool(set(user_groups) & set(names))


def is_folder_admin(user: "User", folder_meta: dict) -> bool:
    """User is global admin OR in folder-level admins list."""
    if is_admin(user.groups):
        return True
    return _has_group(user.groups, _access_block(folder_meta).get("admins"))


def is_folder_member(user: "User", folder_meta: dict) -> bool:
    """User is folder admin OR in folder-level members list."""
    if is_folder_admin(user, folder_meta):
        return True
    return _has_group(user.groups, _access_block(folder_meta).get("members"))


def is_folder_viewer(user: "User", folder_meta: dict) -> bool:
    """User is folder member OR in folder-level viewers list."""
    if is_folder_member(user, folder_meta):
        return True
    return _has_group(user.groups, _access_block(folder_meta).get("viewers"))


def is_env_admin(user: "User", folder_meta: dict, env: str) -> bool:
    """Folder admin OR in `env_access[env].admins`."""
    if is_folder_admin(user, folder_meta):
        return True
    return _has_group(user.groups, _env_access_block(folder_meta, env).get("admins"))


def is_env_member(user: "User", folder_meta: dict, env: str) -> bool:
    """Env admin OR folder member OR in `env_access[env].members`."""
    if is_env_admin(user, folder_meta, env):
        return True
    if is_folder_member(user, folder_meta):
        return True
    return _has_group(user.groups, _env_access_block(folder_meta, env).get("members"))


def is_env_viewer(user: "User", folder_meta: dict, env: str) -> bool:
    """Env member OR folder viewer OR in `env_access[env].viewers`."""
    if is_env_member(user, folder_meta, env):
        return True
    if is_folder_viewer(user, folder_meta):
        return True
    return _has_group(user.groups, _env_access_block(folder_meta, env).get("viewers"))


# ---------------------------------------------------------------------------
# Folder lookup helpers (used by FastAPI deps in core/auth.py — Phase 2)
# ---------------------------------------------------------------------------

async def load_folders(k8s_client: Any) -> dict[str, dict]:
    """Load and parse the folders ConfigMap. Returns {folder_name: meta}.

    Each meta dict has an injected `_name` key to ease downstream use.
    Raises 404 if the ConfigMap does not exist (cluster not bootstrapped).
    """
    from fastapi import HTTPException
    from kubernetes_asyncio.client.rest import ApiException

    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=FOLDERS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail="No folders configured")
        raise

    data = cm.data or {}
    folders: dict[str, dict] = {}
    for name, raw in data.items():
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta["_name"] = name
        folders[name] = meta
    return folders


async def load_folder(k8s_client: Any, folder_name: str) -> dict:
    """Load a single folder's parsed metadata. 404 if it does not exist."""
    from fastapi import HTTPException

    folders = await load_folders(k8s_client)
    if folder_name not in folders:
        raise HTTPException(status_code=404, detail=f"Folder '{folder_name}' not found")
    return folders[folder_name]


async def resolve_env(k8s_client: Any, namespace: str) -> tuple[str, str]:
    """Resolve a namespace name to its (folder, environment) pair.

    Reads the namespace's labels — `kubevirt-ui.io/folder` and
    `kubevirt-ui.io/environment`. Raises HTTPException 404 if the namespace
    is not managed by the folder system (label missing).
    """
    from fastapi import HTTPException
    from kubernetes_asyncio.client.rest import ApiException

    try:
        ns = await k8s_client.core_api.read_namespace(name=namespace)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Namespace '{namespace}' not found")
        raise

    labels = ns.metadata.labels or {}
    folder = labels.get(ENV_FOLDER_LABEL)
    env = labels.get(ENV_ENVIRONMENT_LABEL)
    if not folder or not env:
        raise HTTPException(
            status_code=404,
            detail=f"Namespace '{namespace}' is not a managed folder environment",
        )
    return folder, env


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
