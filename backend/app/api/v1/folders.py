"""Folders API endpoints.

Architecture:
  - Folder = hierarchical grouping stored in ConfigMap (replaces flat Projects)
  - Environment = K8s namespace belonging to a folder
  - Access = RBAC at folder level (all descendant envs) or environment level
  - Tree is stored flat in ConfigMap, reconstructed in memory via parent_id
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client.rest import ApiException
from kubernetes_asyncio.client import RbacAuthorizationV1Api

from app.core.auth import (
    User,
    require_auth,
    require_admin,
    require_folder_admin,
    check_folder_access,
)

from app.models.folder import (
    FolderCreateRequest,
    FolderUpdateRequest,
    FolderMoveRequest,
    FolderResponse,
    FolderTreeResponse,
    FolderListResponse,
    FolderQuota,
    FolderEnvironmentResponse,
    AddFolderEnvironmentRequest,
    SetEnvironmentQuotaRequest,
    FolderAccessEntry,
    FolderAccessListResponse,
    AddFolderAccessRequest,
    FolderAccessSpec,
    FolderAccessPatchRequest,
)
from app.models.project import ROLE_TO_CLUSTERROLE, CLUSTERROLE_TO_ROLE

logger = logging.getLogger(__name__)
router = APIRouter()

# ConfigMap storing folder metadata
FOLDERS_CONFIGMAP = "kubevirt-ui-folders"
SYSTEM_NAMESPACE = "kubevirt-ui-system"

# Labels for managed namespaces (environments)
ENV_ENABLED_LABEL = "kubevirt-ui.io/enabled"
ENV_MANAGED_LABEL = "kubevirt-ui.io/managed"
ENV_FOLDER_LABEL = "kubevirt-ui.io/folder"
ENV_ENVIRONMENT_LABEL = "kubevirt-ui.io/environment"
# Tenant control-plane namespaces (tenant-<name>) carry this label. They also
# carry folder/environment labels (for Phase 2 authz), so they'd otherwise leak
# into a folder's environment list and show up as selectable envs in the tenant
# wizard. Excluded from user-facing environment listings (NOT from RBAC
# reconciliation, which must still reach tenant namespaces).
TENANT_LABEL = "kubevirt-ui.io/tenant"


def _is_tenant_ns(ns_obj: Any) -> bool:
    return bool((ns_obj.metadata.labels or {}).get(TENANT_LABEL))

# Labels for managed RoleBindings
ACCESS_MANAGED_LABEL = "kubevirt-ui.io/managed"
ACCESS_TYPE_LABEL = "kubevirt-ui.io/access-type"
ACCESS_SCOPE_LABEL = "kubevirt-ui.io/access-scope"  # "folder" or "environment"
ACCESS_FOLDER_LABEL = "kubevirt-ui.io/folder"


# ---------------------------------------------------------------------------
# Helpers — ConfigMap storage
# ---------------------------------------------------------------------------

async def _ensure_folders_configmap(k8s_client: Any) -> dict:
    """Read or create the folders ConfigMap. Returns data dict."""
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=FOLDERS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        )
        return cm.data or {}
    except ApiException as e:
        if e.status == 404:
            # Check if the namespace itself is missing
            try:
                await k8s_client.core_api.read_namespace(name=SYSTEM_NAMESPACE)
            except ApiException as ns_err:
                if ns_err.status == 404:
                    raise HTTPException(
                        status_code=503,
                        detail=f"System namespace '{SYSTEM_NAMESPACE}' does not exist. "
                               "It should be created during cluster bootstrap.",
                    )
                raise
            # Namespace exists but ConfigMap doesn't — create it
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": FOLDERS_CONFIGMAP,
                    "namespace": SYSTEM_NAMESPACE,
                    "labels": {"kubevirt-ui.io/managed": "true"},
                },
                "data": {},
            }
            await k8s_client.core_api.create_namespaced_config_map(
                namespace=SYSTEM_NAMESPACE, body=body,
            )
            logger.info("Created folders ConfigMap")
            return {}
        raise


async def _save_folder_meta(k8s_client: Any, name: str, meta: dict):
    """Save folder metadata to ConfigMap."""
    patch = {"data": {name: json.dumps(meta)}}
    await k8s_client.core_api.patch_namespaced_config_map(
        name=FOLDERS_CONFIGMAP, namespace=SYSTEM_NAMESPACE, body=patch,
    )


async def _delete_folder_meta(k8s_client: Any, name: str):
    """Remove folder entry from ConfigMap."""
    cm = await k8s_client.core_api.read_namespaced_config_map(
        name=FOLDERS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
    )
    data = dict(cm.data or {})
    data.pop(name, None)
    await k8s_client.core_api.replace_namespaced_config_map(
        name=FOLDERS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        body={
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": cm.metadata,
            "data": data if data else {},
        },
    )


# ---------------------------------------------------------------------------
# Helpers — tree operations
# ---------------------------------------------------------------------------

def _parse_all_folders(data: dict) -> dict[str, dict]:
    """Parse all folder entries from ConfigMap data."""
    folders: dict[str, dict] = {}
    for name, raw in data.items():
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta["_name"] = name
        folders[name] = meta
    return folders


def _build_access_spec(meta: dict) -> FolderAccessSpec | None:
    """Build the Phase 2 access block from a folder ConfigMap entry.

    Returns None when no `access` block is set — preserves legacy
    "global admins only" semantics (no breakage for existing folders).
    """
    raw = meta.get("access")
    if not raw:
        return None
    try:
        return FolderAccessSpec(**raw)
    except Exception as e:  # pragma: no cover — defensive: tolerate malformed data
        logger.warning(f"Malformed folder access block on '{meta.get('_name')}': {e}")
        return None


def _get_ancestor_chain(folders: dict[str, dict], folder_name: str) -> list[str]:
    """Walk up parent_id chain, return list from root to folder (exclusive)."""
    chain: list[str] = []
    visited: set[str] = set()
    current = folder_name
    while True:
        meta = folders.get(current)
        if not meta:
            break
        parent = meta.get("parent_id")
        if not parent or parent in visited:
            break
        visited.add(parent)
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def _get_descendant_names(folders: dict[str, dict], folder_name: str) -> list[str]:
    """Get all descendant folder names (recursive)."""
    children_index: dict[str | None, list[str]] = {}
    for name, meta in folders.items():
        pid = meta.get("parent_id")
        children_index.setdefault(pid, []).append(name)

    result: list[str] = []
    stack = list(children_index.get(folder_name, []))
    while stack:
        child = stack.pop()
        result.append(child)
        stack.extend(children_index.get(child, []))
    return result


def _would_create_cycle(
    folders: dict[str, dict], folder_name: str, new_parent: str | None,
) -> bool:
    """Check if moving folder_name under new_parent would create a cycle."""
    if new_parent is None:
        return False
    if new_parent == folder_name:
        return True
    # Check if new_parent is a descendant of folder_name
    descendants = _get_descendant_names(folders, folder_name)
    return new_parent in descendants


# ---------------------------------------------------------------------------
# Helpers — namespace / stats
# ---------------------------------------------------------------------------

def _ns_name(folder: str, environment: str) -> str:
    """Build namespace name from folder + environment."""
    return f"{folder}-{environment}"


def _parse_storage(value: str) -> int:
    units = {
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
        "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    }
    for unit, mult in units.items():
        if value.endswith(unit):
            try:
                return int(float(value[: -len(unit)]) * mult)
            except (ValueError, TypeError):
                return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _format_storage(b: int) -> str:
    if b >= 1024**4:
        return f"{b / 1024**4:.1f}Ti"
    if b >= 1024**3:
        return f"{b / 1024**3:.1f}Gi"
    if b >= 1024**2:
        return f"{b / 1024**2:.1f}Mi"
    return f"{b / 1024:.1f}Ki"


async def _get_env_stats(k8s_client: Any, namespace: str) -> dict[str, Any]:
    """Get VM count and storage for a namespace."""
    stats: dict[str, Any] = {"vm_count": 0, "storage_used": None}
    try:
        vms = await k8s_client.custom_api.list_namespaced_custom_object(
            group="kubevirt.io", version="v1",
            namespace=namespace, plural="virtualmachines",
        )
        stats["vm_count"] = len(vms.get("items", []))
    except ApiException:
        pass
    try:
        pvcs = await k8s_client.core_api.list_namespaced_persistent_volume_claim(
            namespace=namespace,
        )
        total = 0
        for pvc in pvcs.items:
            if pvc.status and pvc.status.capacity:
                total += _parse_storage(pvc.status.capacity.get("storage", "0"))
        if total > 0:
            stats["storage_used"] = _format_storage(total)
    except ApiException:
        pass
    return stats


async def _get_env_quotas(k8s_client: Any, ns: str) -> dict:
    q: dict[str, str | None] = {
        "quota_cpu": None, "quota_memory": None, "quota_storage": None,
    }
    try:
        quotas = await k8s_client.core_api.list_namespaced_resource_quota(namespace=ns)
        for quota in quotas.items:
            if quota.spec.hard:
                q["quota_cpu"] = q["quota_cpu"] or quota.spec.hard.get("requests.cpu")
                q["quota_memory"] = q["quota_memory"] or quota.spec.hard.get("requests.memory")
                q["quota_storage"] = q["quota_storage"] or quota.spec.hard.get("requests.storage")
    except ApiException:
        pass
    return q


async def _build_env_response(
    k8s_client: Any, ns_obj: Any, folder_name: str,
) -> FolderEnvironmentResponse:
    """Build FolderEnvironmentResponse from a namespace object."""
    labels = ns_obj.metadata.labels or {}
    env_name = labels.get(ENV_ENVIRONMENT_LABEL, ns_obj.metadata.name)
    stats = await _get_env_stats(k8s_client, ns_obj.metadata.name)
    quotas = await _get_env_quotas(k8s_client, ns_obj.metadata.name)
    return FolderEnvironmentResponse(
        name=ns_obj.metadata.name,
        environment=env_name,
        folder=folder_name,
        created=(
            ns_obj.metadata.creation_timestamp.isoformat()
            if ns_obj.metadata.creation_timestamp
            else None
        ),
        vm_count=stats["vm_count"],
        storage_used=stats["storage_used"],
        **quotas,
    )


async def _get_rbac_api(k8s_client: Any) -> RbacAuthorizationV1Api:
    return RbacAuthorizationV1Api(k8s_client._api_client)


async def _get_folder_namespaces(
    k8s_client: Any, folder_name: str,
) -> list:
    """Get all environment namespaces for a folder."""
    try:
        ns_list = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_FOLDER_LABEL}={folder_name},{ENV_MANAGED_LABEL}=true",
        )
        return ns_list.items
    except ApiException:
        return []


async def _get_descendant_namespaces(
    k8s_client: Any, folders: dict[str, dict], folder_name: str,
) -> list:
    """Get all namespaces for a folder and all its descendants."""
    all_folder_names = [folder_name] + _get_descendant_names(folders, folder_name)
    all_ns = []
    for fname in all_folder_names:
        ns_items = await _get_folder_namespaces(k8s_client, fname)
        all_ns.extend(ns_items)
    return all_ns


async def _get_folder_access_summary(
    rbac_api: RbacAuthorizationV1Api,
    folder_name: str,
    env_namespaces: list[str],
) -> tuple[list[str], list[str]]:
    """Aggregate unique teams and users across all environments of a folder."""
    teams: list[str] = []
    users: list[str] = []
    for ns in env_namespaces:
        try:
            bindings = await rbac_api.list_namespaced_role_binding(
                namespace=ns,
                label_selector=f"{ACCESS_MANAGED_LABEL}=true,{ACCESS_FOLDER_LABEL}={folder_name}",
            )
            for b in bindings.items:
                atype = (b.metadata.labels or {}).get(ACCESS_TYPE_LABEL)
                for s in b.subjects or []:
                    if atype == "team" and s.kind == "Group" and s.name not in teams:
                        teams.append(s.name)
                    elif atype == "user" and s.kind == "User" and s.name not in users:
                        users.append(s.name)
        except ApiException:
            pass
    return teams, users


# ---------------------------------------------------------------------------
# Phase 2 — RoleBindings reconciler (folder access block → K8s)
# ---------------------------------------------------------------------------
#
# Materialises the `access` block of a folder ConfigMap entry into 3
# managed RoleBindings per env namespace (admin / editor / viewer), with
# the UNION of folder-level + env_access[env]-level group subjects.
#
# Existing Phase 1 RoleBindings (per-subject, name `team-...`) are left
# alone — both flows coexist during transition.  Phase 2 RBs are
# identified by `kubevirt-ui.io/access-source=folder-access-block`.

# Fixed RB names — one per role per env namespace.
RB_NAME_ADMIN   = "kubevirt-ui-folder-admins"
RB_NAME_MEMBER  = "kubevirt-ui-folder-members"
RB_NAME_VIEWER  = "kubevirt-ui-folder-viewers"

# Marker label so we never touch RBs created by other systems / Phase 1.
ACCESS_SOURCE_LABEL = "kubevirt-ui.io/access-source"
ACCESS_SOURCE_PHASE2 = "folder-access-block"

# RoleRefs (ClusterRole names) — same as ROLE_TO_CLUSTERROLE.
_RBAC_ROLE_NAMES: list[tuple[str, str, str]] = [
    # (role, rb_name, cluster_role)
    ("admin",   RB_NAME_ADMIN,   ROLE_TO_CLUSTERROLE["admin"]),
    ("member",  RB_NAME_MEMBER,  ROLE_TO_CLUSTERROLE["editor"]),
    ("viewer",  RB_NAME_VIEWER,  ROLE_TO_CLUSTERROLE["viewer"]),
]


def _collect_subjects(
    folder_meta: dict, env: str, role: str,
) -> list[str]:
    """Union of folder-level + env-level group names for `role` on `env`.

    `role` ∈ {"admin", "member", "viewer"}.  Returned list is sorted and
    deduplicated so the rendered RoleBinding is stable across reconciles.
    """
    access = folder_meta.get("access") or {}
    folder_groups = access.get(f"{role}s") or []
    env_access = (access.get("env_access") or {}).get(env) or {}
    env_groups = env_access.get(f"{role}s") or []
    merged = set(folder_groups) | set(env_groups)
    return sorted(g for g in merged if g)


def _build_phase2_rb_body(
    rb_name: str,
    namespace: str,
    folder: str,
    env: str,
    cluster_role: str,
    groups: list[str],
) -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": rb_name,
            "namespace": namespace,
            "labels": {
                ACCESS_MANAGED_LABEL: "true",
                ACCESS_SOURCE_LABEL: ACCESS_SOURCE_PHASE2,
                ACCESS_FOLDER_LABEL: folder,
                ENV_ENVIRONMENT_LABEL: env,
            },
        },
        "roleRef": {
            "kind": "ClusterRole",
            "name": cluster_role,
            "apiGroup": "rbac.authorization.k8s.io",
        },
        "subjects": [
            {
                "kind": "Group",
                "name": g,
                "apiGroup": "rbac.authorization.k8s.io",
            }
            for g in groups
        ],
    }


async def _apply_phase2_rb(
    rbac_api: RbacAuthorizationV1Api,
    rb_name: str,
    namespace: str,
    folder: str,
    env: str,
    cluster_role: str,
    groups: list[str],
):
    """Idempotently create or update a Phase 2 RoleBinding.

    Empty subjects → delete the RB if it exists (avoid K8s validation
    errors on subjects=[] and remove stale access).
    """
    if not groups:
        try:
            await rbac_api.delete_namespaced_role_binding(
                name=rb_name, namespace=namespace,
            )
            logger.info(f"Deleted empty Phase 2 RB {namespace}/{rb_name}")
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete RB {namespace}/{rb_name}: {e}")
        return

    body = _build_phase2_rb_body(
        rb_name, namespace, folder, env, cluster_role, groups,
    )
    try:
        await rbac_api.create_namespaced_role_binding(
            namespace=namespace, body=body,
        )
        logger.info(f"Created Phase 2 RB {namespace}/{rb_name} (subjects={len(groups)})")
        return
    except ApiException as e:
        if e.status != 409:
            logger.warning(f"Failed to create RB {namespace}/{rb_name}: {e}")
            return

    # 409 — already exists; replace.  Use replace to overwrite subjects.
    try:
        existing = await rbac_api.read_namespaced_role_binding(
            name=rb_name, namespace=namespace,
        )
        # Preserve resourceVersion for optimistic concurrency.
        body["metadata"]["resourceVersion"] = existing.metadata.resource_version
        # K8s rejects roleRef updates on RoleBinding; if the existing RB has a
        # different roleRef we must delete-and-create.
        if existing.role_ref.name != cluster_role:
            try:
                await rbac_api.delete_namespaced_role_binding(
                    name=rb_name, namespace=namespace,
                )
            except ApiException:
                pass
            body["metadata"].pop("resourceVersion", None)
            await rbac_api.create_namespaced_role_binding(
                namespace=namespace, body=body,
            )
        else:
            await rbac_api.replace_namespaced_role_binding(
                name=rb_name, namespace=namespace, body=body,
            )
        logger.info(f"Updated Phase 2 RB {namespace}/{rb_name} (subjects={len(groups)})")
    except ApiException as e:
        logger.warning(f"Failed to update RB {namespace}/{rb_name}: {e}")


async def reconcile_folder_rbac(
    k8s_client: Any, folder: str, folder_meta: dict,
) -> None:
    """Reconcile Phase 2 RoleBindings for all env namespaces of a folder.

    Idempotent — safe to call after every access change.  Empty role
    lists → corresponding RB is deleted.  Existing Phase 1 RBs are not
    touched (distinguished by the `access-source` label).
    """
    rbac_api = await _get_rbac_api(k8s_client)
    ns_items = await _get_folder_namespaces(k8s_client, folder)
    if not ns_items:
        return

    for ns_obj in ns_items:
        ns_name = ns_obj.metadata.name
        env = (ns_obj.metadata.labels or {}).get(ENV_ENVIRONMENT_LABEL, ns_name)
        for role, rb_name, cluster_role in _RBAC_ROLE_NAMES:
            groups = _collect_subjects(folder_meta, env, role)
            await _apply_phase2_rb(
                rbac_api, rb_name, ns_name, folder, env, cluster_role, groups,
            )


async def reconcile_env_rbac(
    k8s_client: Any, folder: str, env: str, folder_meta: dict,
) -> None:
    """Reconcile Phase 2 RoleBindings for a single env namespace.

    Used when one env is added — avoids full-folder churn.
    """
    ns_name = _ns_name(folder, env)
    await reconcile_namespace_rbac(
        k8s_client, ns_name, folder, env, folder_meta,
    )


async def reconcile_namespace_rbac(
    k8s_client: Any,
    namespace: str,
    folder: str,
    env: str,
    folder_meta: dict,
) -> None:
    """Reconcile Phase 2 RoleBindings on an arbitrary namespace.

    Same logic as `reconcile_env_rbac`, but lets the caller pass any
    namespace name (e.g. `tenant-<name>` for T2 — tenant namespaces are
    not `<folder>-<env>` shaped so `_ns_name` doesn't apply).
    """
    rbac_api = await _get_rbac_api(k8s_client)
    for role, rb_name, cluster_role in _RBAC_ROLE_NAMES:
        groups = _collect_subjects(folder_meta, env, role)
        await _apply_phase2_rb(
            rbac_api, rb_name, namespace, folder, env, cluster_role, groups,
        )


# ---------------------------------------------------------------------------
# Helpers — RBAC propagation (Phase 1 — kept for backward compat)
# ---------------------------------------------------------------------------

async def _propagate_folder_access(
    k8s_client: Any, folders: dict[str, dict], folder_name: str, target_ns: str,
):
    """Copy all folder-scope access bindings from this folder and ancestors to target namespace.

    Uses two sources:
    1. ConfigMap-persisted access_entries (authoritative, survives empty folder state)
    2. Existing RoleBindings in sibling namespaces (fallback)
    """
    rbac_api = await _get_rbac_api(k8s_client)
    ancestor_chain = _get_ancestor_chain(folders, folder_name) + [folder_name]
    created_bindings: set[str] = set()

    # Source 1: ConfigMap-persisted access entries
    for ancestor in ancestor_chain:
        meta = folders.get(ancestor, {})
        for entry in meta.get("access_entries", []):
            binding_name = entry.get("binding_name")
            if not binding_name or binding_name in created_bindings:
                continue
            cluster_role = ROLE_TO_CLUSTERROLE.get(entry.get("role", ""))
            if not cluster_role:
                continue
            subject_kind = "Group" if entry.get("type") == "team" else "User"
            binding_body = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {
                    "name": binding_name,
                    "namespace": target_ns,
                    "labels": {
                        ACCESS_MANAGED_LABEL: "true",
                        ACCESS_TYPE_LABEL: entry.get("type", "user"),
                        ACCESS_SCOPE_LABEL: "folder",
                        ACCESS_FOLDER_LABEL: ancestor,
                    },
                },
                "subjects": [
                    {
                        "kind": subject_kind,
                        "name": entry["name"],
                        "apiGroup": "rbac.authorization.k8s.io",
                    },
                ],
                "roleRef": {
                    "kind": "ClusterRole",
                    "name": cluster_role,
                    "apiGroup": "rbac.authorization.k8s.io",
                },
            }
            try:
                await rbac_api.create_namespaced_role_binding(
                    namespace=target_ns, body=binding_body,
                )
                created_bindings.add(binding_name)
            except ApiException as e:
                if e.status == 409:
                    created_bindings.add(binding_name)
                else:
                    logger.warning(f"Failed to propagate binding {binding_name}: {e}")

    # Source 2: Existing RoleBindings in sibling namespaces (catches pre-persistence entries)
    for ancestor in ancestor_chain:
        ancestor_ns_items = await _get_folder_namespaces(k8s_client, ancestor)
        for ns_obj in ancestor_ns_items:
            if ns_obj.metadata.name == target_ns:
                continue
            try:
                bindings = await rbac_api.list_namespaced_role_binding(
                    namespace=ns_obj.metadata.name,
                    label_selector=(
                        f"{ACCESS_MANAGED_LABEL}=true,"
                        f"{ACCESS_SCOPE_LABEL}=folder,"
                        f"{ACCESS_FOLDER_LABEL}={ancestor}"
                    ),
                )
                for b in bindings.items:
                    if b.metadata.name in created_bindings:
                        continue
                    new_binding = {
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": "RoleBinding",
                        "metadata": {
                            "name": b.metadata.name,
                            "namespace": target_ns,
                            "labels": dict(b.metadata.labels or {}),
                        },
                        "subjects": [
                            {"kind": s.kind, "name": s.name, "apiGroup": s.api_group}
                            for s in (b.subjects or [])
                        ],
                        "roleRef": {
                            "kind": b.role_ref.kind,
                            "name": b.role_ref.name,
                            "apiGroup": b.role_ref.api_group,
                        },
                    }
                    try:
                        await rbac_api.create_namespaced_role_binding(
                            namespace=target_ns, body=new_binding,
                        )
                        created_bindings.add(b.metadata.name)
                    except ApiException as e:
                        if e.status != 409:
                            logger.warning(
                                f"Failed to propagate binding {b.metadata.name}: {e}"
                            )
                break  # Only need bindings from one namespace per ancestor
            except ApiException:
                continue


# ---------------------------------------------------------------------------
# Folder CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=FolderTreeResponse)
async def list_folders(request: Request, flat: bool = False, user: User = Depends(require_auth)):
    """List all folders. Returns tree structure by default, flat list if flat=true."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    # Get all managed namespaces at once
    try:
        all_ns = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_ENABLED_LABEL}=true",
        )
    except ApiException:
        all_ns = type("obj", (), {"items": []})()

    # Index namespaces by folder label, skipping tenant control-plane
    # namespaces so they don't surface as selectable environments.
    ns_by_folder: dict[str, list] = {}
    for ns in all_ns.items:
        if _is_tenant_ns(ns):
            continue
        folder = (ns.metadata.labels or {}).get(ENV_FOLDER_LABEL)
        if folder:
            ns_by_folder.setdefault(folder, []).append(ns)

    rbac_api = await _get_rbac_api(k8s_client)

    # Build responses for all folders
    folder_responses: dict[str, FolderResponse] = {}
    for name, meta in folders.items():
        env_ns_list = ns_by_folder.get(name, [])
        envs = []
        total_vms = 0
        total_bytes = 0

        for ns_obj in env_ns_list:
            env_resp = await _build_env_response(k8s_client, ns_obj, name)
            envs.append(env_resp)
            total_vms += env_resp.vm_count
            if env_resp.storage_used:
                total_bytes += _parse_storage(env_resp.storage_used)

        env_ns_names = [ns.metadata.name for ns in env_ns_list]
        teams, users = await _get_folder_access_summary(rbac_api, name, env_ns_names)

        quota_data = meta.get("quota")
        quota = FolderQuota(**quota_data) if quota_data else None
        path = _get_ancestor_chain(folders, name)

        folder_responses[name] = FolderResponse(
            name=name,
            display_name=meta.get("display_name", name),
            description=meta.get("description", ""),
            parent_id=meta.get("parent_id"),
            created_by=meta.get("created_by"),
            created_at=meta.get("created_at"),
            quota=quota,
            path=path,
            environments=envs,
            total_vms=total_vms,
            total_storage=_format_storage(total_bytes) if total_bytes > 0 else None,
            teams=teams,
            users=users,
            access=_build_access_spec(meta),
        )

    if flat:
        items = list(folder_responses.values())
        return FolderTreeResponse(items=items, total=len(items))

    # Build tree: nest children under parents
    children_index: dict[str | None, list[str]] = {}
    for name, meta in folders.items():
        pid = meta.get("parent_id")
        children_index.setdefault(pid, []).append(name)

    def _build_tree(parent_id: str | None) -> list[FolderResponse]:
        result = []
        for child_name in children_index.get(parent_id, []):
            resp = folder_responses[child_name]
            resp.children = _build_tree(child_name)
            # Aggregate descendant stats without mutating direct count
            descendant_vms = sum(child.total_vms for child in resp.children)
            resp.total_vms = resp.total_vms + descendant_vms
            result.append(resp)
        return result

    root_items = _build_tree(None)
    return FolderTreeResponse(items=root_items, total=len(folders))


@router.post("", response_model=FolderResponse, status_code=201)
async def create_folder(request: Request, folder: FolderCreateRequest, user: User = Depends(require_admin)):
    """Create a folder (ConfigMap entry) with optional initial environments."""
    k8s_client = request.app.state.k8s_client

    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if folder.name in folders:
        raise HTTPException(status_code=409, detail=f"Folder '{folder.name}' already exists")

    # Validate parent exists
    if folder.parent_id and folder.parent_id not in folders:
        raise HTTPException(status_code=404, detail=f"Parent folder '{folder.parent_id}' not found")

    # Validate quota against parent
    if folder.parent_id and folder.quota:
        parent_meta = folders[folder.parent_id]
        parent_quota = parent_meta.get("quota")
        if parent_quota:
            _validate_child_quota(folder.quota, FolderQuota(**parent_quota))
            # Siblings count too: two children of 16 each under a parent of 16
            # both pass the comparison above.
            await assert_child_folder_within_parent(
                k8s_client,
                {**folders, folder.name: {"parent_id": folder.parent_id}},
                folder.name,
                folder.quota.model_dump(exclude_none=True) if folder.quota else None,
            )

    now = datetime.now(timezone.utc).isoformat()
    meta: dict[str, Any] = {
        "display_name": folder.display_name,
        "description": folder.description,
        "parent_id": folder.parent_id,
        "created_by": user.email,
        "created_at": now,
    }
    if folder.quota:
        meta["quota"] = folder.quota.model_dump(exclude_none=True)
    await _save_folder_meta(k8s_client, folder.name, meta)
    logger.info(f"Created folder: {folder.name} (parent={folder.parent_id})")

    # Re-read for tree helpers
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    # Create initial environments
    envs = []
    for env_name in folder.environments:
        env_resp = await _create_environment_ns(k8s_client, folders, folder.name, env_name)
        envs.append(env_resp)

    path = _get_ancestor_chain(folders, folder.name)

    return FolderResponse(
        name=folder.name,
        display_name=folder.display_name,
        description=folder.description,
        parent_id=folder.parent_id,
        created_by=meta["created_by"],
        created_at=now,
        quota=folder.quota,
        path=path,
        environments=envs,
    )


@router.get("/{name}", response_model=FolderResponse)
async def get_folder(request: Request, name: str, user: User = Depends(require_auth)):
    """Get a single folder with environments."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    meta = folders[name]

    # List environments for this folder, excluding tenant control-plane
    # namespaces (they carry folder/env labels but must not appear as envs).
    ns_items = [ns for ns in await _get_folder_namespaces(k8s_client, name) if not _is_tenant_ns(ns)]

    envs = []
    total_vms = 0
    total_bytes = 0
    for ns_obj in ns_items:
        env_resp = await _build_env_response(k8s_client, ns_obj, name)
        envs.append(env_resp)
        total_vms += env_resp.vm_count
        if env_resp.storage_used:
            total_bytes += _parse_storage(env_resp.storage_used)

    rbac_api = await _get_rbac_api(k8s_client)
    env_ns_names = [ns.metadata.name for ns in ns_items]
    teams, users = await _get_folder_access_summary(rbac_api, name, env_ns_names)

    quota_data = meta.get("quota")
    quota = FolderQuota(**quota_data) if quota_data else None
    path = _get_ancestor_chain(folders, name)

    # Get direct children
    children = []
    for cname, cmeta in folders.items():
        if cmeta.get("parent_id") == name:
            children.append(FolderResponse(
                name=cname,
                display_name=cmeta.get("display_name", cname),
                description=cmeta.get("description", ""),
                parent_id=name,
                created_by=cmeta.get("created_by"),
                created_at=cmeta.get("created_at"),
                quota=FolderQuota(**cmeta["quota"]) if cmeta.get("quota") else None,
                path=path + [name],
            ))

    return FolderResponse(
        name=name,
        display_name=meta.get("display_name", name),
        description=meta.get("description", ""),
        parent_id=meta.get("parent_id"),
        created_by=meta.get("created_by"),
        created_at=meta.get("created_at"),
        quota=quota,
        path=path,
        children=children,
        environments=envs,
        total_vms=total_vms,
        total_storage=_format_storage(total_bytes) if total_bytes > 0 else None,
        teams=teams,
        users=users,
        access=_build_access_spec(meta),
    )


@router.patch("/{name}", response_model=FolderResponse)
async def update_folder(request: Request, name: str, update: FolderUpdateRequest, user: User = Depends(require_admin)):
    """Update folder metadata (display name, description, quota)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    meta = folders[name]

    if update.display_name is not None:
        meta["display_name"] = update.display_name
    if update.description is not None:
        meta["description"] = update.description
    if update.quota is not None:
        # Validate against parent quota
        parent_id = meta.get("parent_id")
        if parent_id and parent_id in folders:
            parent_quota_data = folders[parent_id].get("quota")
            if parent_quota_data:
                _validate_child_quota(update.quota, FolderQuota(**parent_quota_data))
        # ...and against what the parent has left, not only against its total.
        await assert_child_folder_within_parent(
            k8s_client, folders, name,
            update.quota.model_dump(exclude_none=True),
        )
        q = update.quota.model_dump(exclude_none=True)
        meta["quota"] = q if q else None

    # Remove internal keys before saving
    save_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
    await _save_folder_meta(k8s_client, name, save_meta)
    logger.info(f"Updated folder: {name}")

    return await get_folder(request, name)


@router.delete("/{name}", status_code=204)
async def delete_folder(request: Request, name: str, cascade: bool = False, user: User = Depends(require_admin)):
    """Delete a folder. Must be empty unless cascade=true."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    descendants = _get_descendant_names(folders, name)

    if not cascade:
        # Check for children
        if descendants:
            raise HTTPException(
                status_code=409,
                detail=f"Folder has {len(descendants)} child folder(s). Use cascade=true to delete all.",
            )
        # Check for environments
        ns_items = await _get_folder_namespaces(k8s_client, name)
        if ns_items:
            raise HTTPException(
                status_code=409,
                detail=f"Folder has {len(ns_items)} environment(s). Remove them first or use cascade=true.",
            )

    # Delete all descendant folders and their environments
    all_to_delete = descendants + [name]
    for fname in all_to_delete:
        # Delete environment namespaces
        try:
            ns_list = await k8s_client.core_api.list_namespace(
                label_selector=f"{ENV_FOLDER_LABEL}={fname},{ENV_MANAGED_LABEL}=true",
            )
            for ns in ns_list.items:
                try:
                    await k8s_client.core_api.delete_namespace(name=ns.metadata.name)
                    logger.info(f"Deleted environment namespace: {ns.metadata.name}")
                except ApiException as e:
                    logger.warning(f"Failed to delete namespace {ns.metadata.name}: {e}")
        except ApiException:
            pass

        # Remove folder from ConfigMap
        await _delete_folder_meta(k8s_client, fname)
        logger.info(f"Deleted folder: {fname}")


@router.post("/{name}/move", response_model=FolderResponse)
async def move_folder(request: Request, name: str, move: FolderMoveRequest, user: User = Depends(require_admin)):
    """Move a folder to a new parent (or to root if new_parent_id is null)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    if move.new_parent_id and move.new_parent_id not in folders:
        raise HTTPException(status_code=404, detail=f"Target parent '{move.new_parent_id}' not found")

    if _would_create_cycle(folders, name, move.new_parent_id):
        raise HTTPException(status_code=400, detail="Cannot move folder under its own descendant")

    meta = folders[name]
    meta["parent_id"] = move.new_parent_id

    save_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
    await _save_folder_meta(k8s_client, name, save_meta)
    logger.info(f"Moved folder {name} to parent={move.new_parent_id}")

    # TODO: re-propagate RBAC for moved subtree

    return await get_folder(request, name)


# ---------------------------------------------------------------------------
# Quota headroom
# ---------------------------------------------------------------------------

# Kubernetes quantities, parsed without pulling in a dependency: the folder
# quota and the env quotas have to be compared, and "16Gi" vs "8192Mi" must
# not become a string comparison.
_QUANTITY_SUFFIXES = {
    "": 1, "m": 0.001,
    "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
}


def parse_quantity(value: str | None) -> float | None:
    """A Kubernetes quantity as a number, or None when unset/unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for suffix in sorted(_QUANTITY_SUFFIXES, key=len, reverse=True):
        if suffix and text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * _QUANTITY_SUFFIXES[suffix]
            except ValueError:
                return None
    try:
        return float(text)
    except ValueError:
        return None


async def _own_env_quota(k8s_client: Any, folder_name: str) -> dict[str, float]:
    """Sum of the quotas handed to this folder's own environments.

    Read from the ResourceQuota objects rather than from bookkeeping of our
    own: they are what the API server enforces, and an admin editing one with
    kubectl must count.
    """
    totals = {"cpu": 0.0, "memory": 0.0, "storage": 0.0}
    try:
        namespaces = await k8s_client.list_namespaces(
            label_selector=f"{ENV_FOLDER_LABEL}={folder_name}",
        )
    except Exception as e:
        logger.warning(f"Could not list namespaces of folder {folder_name}: {e}")
        return totals

    for ns in namespaces:
        ns_name = ns["name"] if isinstance(ns, dict) else ns
        try:
            quotas = await k8s_client.core_api.list_namespaced_resource_quota(
                namespace=ns_name,
            )
        except ApiException:
            continue
        for quota in quotas.items:
            hard = (quota.spec.hard if quota.spec else None) or {}
            for key, field in (
                ("requests.cpu", "cpu"),
                ("requests.memory", "memory"),
                ("requests.storage", "storage"),
            ):
                value = parse_quantity(hard.get(key))
                if value:
                    totals[field] += value
    return totals


def _direct_children(folders: dict[str, dict], folder_name: str) -> list[str]:
    return [n for n, m in folders.items() if m.get("parent_id") == folder_name]


async def _allocated_env_quota(
    k8s_client: Any, folders: dict[str, dict], folder_name: str,
) -> dict[str, float]:
    """Everything a folder's ceiling already covers, sub-folders included.

    A sub-folder spends its parent's budget: without this, `lab` capped at 16
    CPU could hold two children of 16 each, because the only check compared
    one child against the parent and never the children against each other.

    A child that declares a quota reserves exactly that much — its own ceiling
    is the promise the parent has already made to it, whether or not its
    environments have claimed it yet. A child without one contributes whatever
    its subtree has actually allocated, which is the honest figure when
    nothing has been promised.
    """
    totals = await _own_env_quota(k8s_client, folder_name)

    for child in _direct_children(folders, folder_name):
        child_quota = (folders.get(child) or {}).get("quota") or {}
        if child_quota:
            for field in totals:
                value = parse_quantity(child_quota.get(field))
                if value:
                    totals[field] += value
        else:
            nested = await _allocated_env_quota(k8s_client, folders, child)
            for field in totals:
                totals[field] += nested[field]

    return totals



async def assert_child_folder_within_parent(
    k8s_client: Any, folders: dict[str, dict], child_name: str,
    quota: dict[str, str] | None,
) -> None:
    """Refuse a sub-folder quota the parent cannot cover.

    `_validate_child_quota` compares one child against the parent and stops
    there, so a parent capped at 16 CPU accepted two children of 16 — each
    was individually "not more than the parent". The siblings have to be
    counted, and so do the parent's own environments.
    """
    meta = folders.get(child_name) or {}
    parent = meta.get("parent_id")
    if not parent or parent not in folders:
        return
    if not quota:
        return

    ceiling = (folders[parent].get("quota") or {})
    if not ceiling:
        return

    allocated = await _allocated_env_quota(k8s_client, folders, parent)

    # This child's current reservation is not competing with itself.
    current = meta.get("quota") or {}
    for field in allocated:
        value = parse_quantity(current.get(field))
        if value:
            allocated[field] -= value

    for field, label in (("cpu", "CPU"), ("memory", "memory"), ("storage", "storage")):
        want = parse_quantity(quota.get(field))
        limit = parse_quantity(ceiling.get(field))
        if want is None or limit is None:
            continue
        free = limit - allocated[field]
        if want > free + 1e-9:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{label} quota {ceiling.get(field)} of parent folder "
                    f"'{parent}' is already {allocated[field]:g} allocated to "
                    f"its environments and other sub-folders; {free:g} is free "
                    f"and '{child_name}' asks for {want:g}."
                ),
            )



async def _read_env_quota(k8s_client: Any, ns_name: str) -> dict[str, str]:
    """The quota currently on an environment, as the fields we manage."""
    out: dict[str, str] = {}
    try:
        quotas = await k8s_client.core_api.list_namespaced_resource_quota(namespace=ns_name)
    except ApiException:
        return out
    for quota in quotas.items:
        hard = (quota.spec.hard if quota.spec else None) or {}
        for key, field in (
            ("requests.cpu", "cpu"),
            ("requests.memory", "memory"),
            ("requests.storage", "storage"),
        ):
            if hard.get(key):
                out[field] = hard[key]
    return out


def _quota_hard(cpu: str | None, memory: str | None, storage: str | None) -> dict[str, str]:
    """`spec.hard` for a quota, requests and limits together.

    Both, deliberately: a quota naming only requests lets a caller declare a
    small request and an unbounded limit and take the node anyway.
    """
    hard: dict[str, str] = {}
    if cpu:
        hard["requests.cpu"] = cpu
        hard["limits.cpu"] = cpu
    if memory:
        hard["requests.memory"] = memory
        hard["limits.memory"] = memory
    if storage:
        hard["requests.storage"] = storage
    return hard


async def _write_env_quota(
    k8s_client: Any, ns_name: str,
    cpu: str | None, memory: str | None, storage: str | None,
) -> None:
    """Replace an environment's ResourceQuota, or delete it when cleared."""
    name = f"{ns_name}-quota"
    hard = _quota_hard(cpu, memory, storage)
    if not hard:
        try:
            await k8s_client.core_api.delete_namespaced_resource_quota(
                name=name, namespace=ns_name,
            )
        except ApiException as e:
            if e.status != 404:
                raise
        return

    body = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {
            "name": name, "namespace": ns_name,
            "labels": {ENV_MANAGED_LABEL: "true"},
        },
        "spec": {"hard": hard},
    }
    try:
        await k8s_client.core_api.replace_namespaced_resource_quota(
            name=name, namespace=ns_name, body=body,
        )
    except ApiException as e:
        if e.status != 404:
            raise
        await k8s_client.core_api.create_namespaced_resource_quota(
            namespace=ns_name, body=body,
        )


async def _apply_reallocations(
    k8s_client: Any, folders: dict[str, dict], folder_name: str,
    reallocate: list[Any],
) -> list[tuple[Any, dict[str, str] | None]]:
    """Shrink the named siblings, returning what they held before.

    The caller restores from that list if the thing the room was freed for
    then fails to be created — a half-applied rebalance leaves someone
    smaller for nothing.

    A field the caller left out keeps its current value. The quota is
    replaced wholesale underneath, so taking memory from a sibling while
    saying nothing about its CPU would have wiped the CPU — silent damage to
    a namespace nobody was editing.
    """
    undo: list[tuple[Any, dict[str, str] | None]] = []
    for item in reallocate:
        asked = {"cpu": item.cpu, "memory": item.memory, "storage": item.storage}

        if item.kind == "folder":
            meta = folders.get(item.source)
            if meta is None:
                raise HTTPException(
                    status_code=404, detail=f"Sub-folder '{item.source}' not found",
                )
            current = dict(meta.get("quota") or {})
            undo.append((item, current or None))
            merged = {**current, **{k: v for k, v in asked.items() if v is not None}}
            meta["quota"] = {k: v for k, v in merged.items() if v} or None
            await _save_folder_meta(
                k8s_client, item.source,
                {k: v for k, v in meta.items() if not k.startswith("_")},
            )
        else:
            ns_name = _ns_name(folder_name, item.source)
            current = await _read_env_quota(k8s_client, ns_name)
            undo.append((item, current))
            merged = {**current, **{k: v for k, v in asked.items() if v is not None}}
            await _write_env_quota(
                k8s_client, ns_name,
                merged.get("cpu"), merged.get("memory"), merged.get("storage"),
            )
    return undo


async def _undo_reallocations(
    k8s_client: Any, folders: dict[str, dict], folder_name: str,
    undo: list[tuple[Any, dict[str, str] | None]],
) -> None:
    for item, previous in reversed(undo):
        try:
            if item.kind == "folder":
                meta = folders.get(item.source) or {}
                meta["quota"] = previous or None
                await _save_folder_meta(
                    k8s_client, item.source,
                    {k: v for k, v in meta.items() if not k.startswith("_")},
                )
            else:
                prev = previous or {}
                await _write_env_quota(
                    k8s_client, _ns_name(folder_name, item.source),
                    prev.get("cpu"), prev.get("memory"), prev.get("storage"),
                )
        except Exception as e:
            logger.warning(
                f"Could not restore quota of {item.source!r} after a failed "
                f"reallocation: {e}"
            )


def _plan_frees(
    allocated: dict[str, float], reallocate: list[Any],
    before: dict[str, dict[str, str]],
) -> dict[str, float]:
    """Allocation after the planned shrinks, before the new request."""
    out = dict(allocated)
    for item in reallocate:
        old = before.get(item.source, {})
        for field, new_value in (
            ("cpu", item.cpu), ("memory", item.memory), ("storage", item.storage),
        ):
            was = parse_quantity(old.get(field)) or 0.0
            now = parse_quantity(new_value) or 0.0
            out[field] += now - was
    return out


async def assert_within_folder_quota(
    k8s_client: Any, folders: dict[str, dict], folder_name: str,
    quota_cpu: str | None, quota_memory: str | None, quota_storage: str | None,
    exclude_namespace: str | None = None,
) -> None:
    """Refuse an environment quota that would overrun the folder's.

    The folder quota is a ceiling and nothing enforced it: it was a number on
    a page while the environments below it could be given anything. Checked
    here rather than in the wizard so `kubectl`-driven and API-driven callers
    obey it too — the whole reason the quota moves onto real ResourceQuota
    objects.

    A folder without a quota constrains nothing, which is the state every
    folder is in until someone sets one.
    """
    meta = folders.get(folder_name) or {}
    ceiling = meta.get("quota") or {}
    if not ceiling:
        return

    allocated = await _allocated_env_quota(k8s_client, folders, folder_name)
    if exclude_namespace:
        # Re-allocating an existing environment: its current quota is not
        # competing with itself.
        try:
            existing = await k8s_client.core_api.list_namespaced_resource_quota(
                namespace=exclude_namespace,
            )
            for quota in existing.items:
                hard = (quota.spec.hard if quota.spec else None) or {}
                for key, field in (
                    ("requests.cpu", "cpu"),
                    ("requests.memory", "memory"),
                    ("requests.storage", "storage"),
                ):
                    value = parse_quantity(hard.get(key))
                    if value:
                        allocated[field] -= value
        except ApiException:
            pass

    requested = {
        "cpu": parse_quantity(quota_cpu),
        "memory": parse_quantity(quota_memory),
        "storage": parse_quantity(quota_storage),
    }

    for field, label in (("cpu", "CPU"), ("memory", "memory"), ("storage", "storage")):
        want = requested[field]
        limit = parse_quantity(ceiling.get(field))
        if want is None or limit is None:
            continue
        free = limit - allocated[field]
        if want > free + 1e-9:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{label} quota {ceiling.get(field)} for folder "
                    f"'{folder_name}' is already {allocated[field]:g} allocated; "
                    f"{free:g} is free and this environment asks for "
                    f"{want:g}. Lower it, or take the difference from another "
                    f"environment first."
                ),
            )


# ---------------------------------------------------------------------------
# Environment CRUD
# ---------------------------------------------------------------------------

async def _create_environment_ns(
    k8s_client: Any,
    folders: dict[str, dict],
    folder_name: str,
    environment: str,
    quota_cpu: str | None = None,
    quota_memory: str | None = None,
    quota_storage: str | None = None,
) -> FolderEnvironmentResponse:
    """Create a namespace for an environment under a folder."""
    ns_name = _ns_name(folder_name, environment)

    # The folder quota is a ceiling; refuse before creating anything, so a
    # rejected request does not leave a namespace behind.
    await assert_within_folder_quota(
        k8s_client, folders, folder_name, quota_cpu, quota_memory, quota_storage,
    )

    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": ns_name,
            "labels": {
                ENV_ENABLED_LABEL: "true",
                ENV_MANAGED_LABEL: "true",
                ENV_FOLDER_LABEL: folder_name,
                ENV_ENVIRONMENT_LABEL: environment,
            },
        },
    }

    try:
        created = await k8s_client.core_api.create_namespace(body=namespace)
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409, detail=f"Environment '{ns_name}' already exists",
            )
        raise HTTPException(status_code=e.status, detail=str(e.reason))

    # Create quota if specified
    if quota_cpu or quota_memory or quota_storage:
        hard = {}
        if quota_cpu:
            hard["requests.cpu"] = quota_cpu
            hard["limits.cpu"] = quota_cpu
        if quota_memory:
            hard["requests.memory"] = quota_memory
            hard["limits.memory"] = quota_memory
        if quota_storage:
            hard["requests.storage"] = quota_storage
        try:
            await k8s_client.core_api.create_namespaced_resource_quota(
                namespace=ns_name,
                body={
                    "apiVersion": "v1",
                    "kind": "ResourceQuota",
                    "metadata": {
                        "name": f"{ns_name}-quota",
                        "namespace": ns_name,
                        "labels": {ENV_MANAGED_LABEL: "true"},
                    },
                    "spec": {"hard": hard},
                },
            )
        except ApiException as e:
            logger.warning(f"Failed to create quota for {ns_name}: {e}")

        # A quota that names limits.cpu/limits.memory makes the API server
        # reject every pod that does not declare them. VMs are fine —
        # virt-launcher sets both — but a plain pod would be refused outright
        # with "must specify limits.cpu". The LimitRange supplies defaults so
        # the quota constrains the namespace instead of blocking it.
        try:
            await k8s_client.core_api.create_namespaced_limit_range(
                namespace=ns_name,
                body={
                    "apiVersion": "v1",
                    "kind": "LimitRange",
                    "metadata": {
                        "name": f"{ns_name}-defaults",
                        "namespace": ns_name,
                        "labels": {ENV_MANAGED_LABEL: "true"},
                    },
                    "spec": {"limits": [{
                        "type": "Container",
                        "default": {"cpu": "500m", "memory": "512Mi"},
                        "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                    }]},
                },
            )
        except ApiException as e:
            if e.status != 409:
                logger.warning(f"Failed to create LimitRange for {ns_name}: {e}")

    # Propagate folder-level access (including ancestors) to new environment
    await _propagate_folder_access(k8s_client, folders, folder_name, ns_name)

    # Phase 2 reconcile — materialise the access block onto the new env.
    folder_meta = folders.get(folder_name) or {}
    await reconcile_env_rbac(k8s_client, folder_name, environment, folder_meta)

    logger.info(f"Created environment: {ns_name} (folder={folder_name})")
    return FolderEnvironmentResponse(
        name=ns_name,
        environment=environment,
        folder=folder_name,
        created=(
            created.metadata.creation_timestamp.isoformat()
            if created.metadata.creation_timestamp
            else None
        ),
        quota_cpu=quota_cpu,
        quota_memory=quota_memory,
        quota_storage=quota_storage,
    )


@router.post(
    "/{name}/environments",
    response_model=FolderEnvironmentResponse,
    status_code=201,
)
async def add_environment(
    request: Request,
    name: str,
    env: AddFolderEnvironmentRequest,
    user: User = Depends(require_folder_admin()),
):
    """Add an environment (namespace) to a folder."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    # Validate the whole plan before touching anything: shrinking a sibling
    # and then failing to create what the room was for leaves someone smaller
    # for nothing.
    before: dict[str, dict[str, str]] = {}
    for item in env.reallocate:
        if item.kind == "folder":
            before[item.source] = dict((folders.get(item.source) or {}).get("quota") or {})
        else:
            before[item.source] = await _read_env_quota(
                k8s_client, _ns_name(name, item.source),
            )

    if env.reallocate:
        allocated = await _allocated_env_quota(k8s_client, folders, name)
        planned = _plan_frees(allocated, env.reallocate, before)
        ceiling = (folders[name].get("quota") or {})
        for field, label in (("cpu", "CPU"), ("memory", "memory"), ("storage", "storage")):
            want = parse_quantity(
                {"cpu": env.quota_cpu, "memory": env.quota_memory,
                 "storage": env.quota_storage}[field]
            )
            limit = parse_quantity(ceiling.get(field))
            if want is None or limit is None:
                continue
            free = limit - planned[field]
            if want > free + 1e-9:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Even after the reallocation the {label} ceiling "
                        f"{ceiling.get(field)} of '{name}' leaves {free:g} free "
                        f"and this environment asks for {want:g}."
                    ),
                )

    undo = await _apply_reallocations(k8s_client, folders, name, env.reallocate)
    try:
        return await _create_environment_ns(
            k8s_client, folders, name, env.environment,
            env.quota_cpu, env.quota_memory, env.quota_storage,
        )
    except Exception:
        await _undo_reallocations(k8s_client, folders, name, undo)
        raise


@router.get("/{name}/quota-headroom")
async def get_folder_quota_headroom(
    request: Request, name: str, user: User = Depends(require_auth),
):
    """What the folder's ceiling still has free.

    The number the UI needs while someone types an environment quota, and the
    same arithmetic the create path refuses on — so the form and the server
    cannot disagree about how much is left.
    """
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)
    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    ceiling = (folders[name].get("quota") or {})
    allocated = await _allocated_env_quota(k8s_client, folders, name)

    out: dict[str, Any] = {"quota": ceiling or None, "allocated": {}, "free": {}}
    for field in ("cpu", "memory", "storage"):
        limit = parse_quantity(ceiling.get(field))
        out["allocated"][field] = allocated[field]
        out["free"][field] = None if limit is None else max(0.0, limit - allocated[field])
    return out


@router.put("/{name}/environments/{environment}/quota")
async def set_environment_quota(
    request: Request, name: str, environment: str,
    body: SetEnvironmentQuotaRequest,
    user: User = Depends(require_folder_admin()),
):
    """Replace one environment's quota.

    The half of rebalancing that had no route at all: environments could be
    created with a quota and deleted, never re-sized, so freeing room from a
    sibling meant deleting it.
    """
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)
    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    ns_name = _ns_name(name, environment)
    try:
        await k8s_client.core_api.read_namespace(name=ns_name)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404, detail=f"Environment '{environment}' not found",
            )
        raise

    await assert_within_folder_quota(
        k8s_client, folders, name, body.cpu, body.memory, body.storage,
        exclude_namespace=ns_name,
    )
    await _write_env_quota(k8s_client, ns_name, body.cpu, body.memory, body.storage)
    return {"environment": environment, "quota": {
        "cpu": body.cpu, "memory": body.memory, "storage": body.storage,
    }}


@router.delete("/{name}/environments/{environment}", status_code=204)
async def remove_environment(
    request: Request,
    name: str,
    environment: str,
    user: User = Depends(require_folder_admin()),
):
    """Remove an environment (delete its namespace)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    ns_name = _ns_name(name, environment)
    try:
        ns = await k8s_client.core_api.read_namespace(name=ns_name)
        labels = ns.metadata.labels or {}
        if labels.get(ENV_MANAGED_LABEL) != "true" or labels.get(ENV_FOLDER_LABEL) != name:
            raise HTTPException(
                status_code=403, detail="Namespace not managed by this folder",
            )
        await k8s_client.core_api.delete_namespace(name=ns_name)
        logger.info(f"Deleted environment: {ns_name}")
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail="Environment not found")
        raise HTTPException(status_code=e.status, detail=str(e.reason))


# ---------------------------------------------------------------------------
# Access CRUD (folder-level and environment-level)
# ---------------------------------------------------------------------------

@router.get("/{name}/access", response_model=FolderAccessListResponse)
async def list_folder_access(request: Request, name: str, user: User = Depends(require_auth)):
    """List all access entries for a folder (including inherited from ancestors)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    rbac_api = await _get_rbac_api(k8s_client)

    # Get namespaces for this folder
    ns_items = await _get_folder_namespaces(k8s_client, name)

    seen_ids: set[str] = set()
    entries: list[FolderAccessEntry] = []

    # Direct access on this folder
    for ns in ns_items:
        try:
            bindings = await rbac_api.list_namespaced_role_binding(
                namespace=ns.metadata.name,
                label_selector=f"{ACCESS_MANAGED_LABEL}=true,{ACCESS_FOLDER_LABEL}={name}",
            )
            for b in bindings.items:
                bid = b.metadata.name
                if bid in seen_ids:
                    continue
                seen_ids.add(bid)

                labels = b.metadata.labels or {}
                scope = labels.get(ACCESS_SCOPE_LABEL, "folder")
                atype = labels.get(ACCESS_TYPE_LABEL, "unknown")
                role = CLUSTERROLE_TO_ROLE.get(b.role_ref.name, "custom")
                env_label = (ns.metadata.labels or {}).get(ENV_ENVIRONMENT_LABEL)

                for s in b.subjects or []:
                    entries.append(FolderAccessEntry(
                        id=bid,
                        type=atype,
                        name=s.name,
                        role=role,
                        scope=scope,
                        environment=env_label if scope == "environment" else None,
                        folder=name,
                        inherited=False,
                        created=(
                            b.metadata.creation_timestamp.isoformat()
                            if b.metadata.creation_timestamp
                            else None
                        ),
                    ))
        except ApiException:
            pass

    # Inherited access from ancestors
    ancestor_chain = _get_ancestor_chain(folders, name)
    for ancestor in ancestor_chain:
        ancestor_ns = await _get_folder_namespaces(k8s_client, ancestor)
        for ns in ancestor_ns:
            try:
                bindings = await rbac_api.list_namespaced_role_binding(
                    namespace=ns.metadata.name,
                    label_selector=(
                        f"{ACCESS_MANAGED_LABEL}=true,"
                        f"{ACCESS_SCOPE_LABEL}=folder,"
                        f"{ACCESS_FOLDER_LABEL}={ancestor}"
                    ),
                )
                for b in bindings.items:
                    bid = f"inherited-{ancestor}-{b.metadata.name}"
                    if bid in seen_ids:
                        continue
                    seen_ids.add(bid)

                    labels = b.metadata.labels or {}
                    atype = labels.get(ACCESS_TYPE_LABEL, "unknown")
                    role = CLUSTERROLE_TO_ROLE.get(b.role_ref.name, "custom")

                    for s in b.subjects or []:
                        entries.append(FolderAccessEntry(
                            id=bid,
                            type=atype,
                            name=s.name,
                            role=role,
                            scope="folder",
                            folder=ancestor,
                            inherited=True,
                            created=(
                                b.metadata.creation_timestamp.isoformat()
                                if b.metadata.creation_timestamp
                                else None
                            ),
                        ))
            except ApiException:
                pass
            break  # Only check one namespace per ancestor

    return FolderAccessListResponse(items=entries, total=len(entries))


@router.post("/{name}/access", response_model=FolderAccessEntry, status_code=201)
async def add_folder_access(
    request: Request,
    name: str,
    access: AddFolderAccessRequest,
    user: User = Depends(require_folder_admin()),
):
    """Add access to a folder or specific environment."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    cluster_role = ROLE_TO_CLUSTERROLE.get(access.role)
    if not cluster_role:
        raise HTTPException(status_code=400, detail=f"Invalid role: {access.role}")

    safe_name = access.name.replace("@", "-at-").replace(".", "-")
    binding_name = f"{access.type}-{safe_name}-{access.role}"
    subject_kind = "Group" if access.type == "team" else "User"

    binding_labels = {
        ACCESS_MANAGED_LABEL: "true",
        ACCESS_TYPE_LABEL: access.type,
        ACCESS_SCOPE_LABEL: access.scope,
        ACCESS_FOLDER_LABEL: name,
    }

    binding_body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": binding_name, "labels": binding_labels},
        "subjects": [
            {
                "kind": subject_kind,
                "name": access.name,
                "apiGroup": "rbac.authorization.k8s.io",
            },
        ],
        "roleRef": {
            "kind": "ClusterRole",
            "name": cluster_role,
            "apiGroup": "rbac.authorization.k8s.io",
        },
    }

    rbac_api = await _get_rbac_api(k8s_client)

    if access.scope == "environment":
        # Single environment
        if not access.environment:
            raise HTTPException(
                status_code=400,
                detail="environment is required for environment-scope access",
            )
        target_ns = _ns_name(name, access.environment)
        binding_body["metadata"]["namespace"] = target_ns
        try:
            created = await rbac_api.create_namespaced_role_binding(
                namespace=target_ns, body=binding_body,
            )
        except ApiException as e:
            if e.status == 409:
                raise HTTPException(status_code=409, detail="Access already exists")
            raise HTTPException(status_code=e.status, detail=str(e.reason))
    else:
        # Folder scope — persist entry in ConfigMap so future namespaces get it
        access_entry_record = {
            "type": access.type,
            "name": access.name,
            "role": access.role,
            "binding_name": binding_name,
        }
        meta = folders[name]
        saved_entries = meta.get("access_entries", [])
        # Avoid duplicates by binding_name
        if not any(e.get("binding_name") == binding_name for e in saved_entries):
            saved_entries.append(access_entry_record)
            meta["access_entries"] = saved_entries
            save_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
            await _save_folder_meta(k8s_client, name, save_meta)

        # Create in ALL descendant environment namespaces
        all_ns = await _get_descendant_namespaces(k8s_client, folders, name)

        created = None
        for ns_obj in all_ns:
            b = dict(binding_body)
            b["metadata"] = dict(binding_body["metadata"])
            b["metadata"]["namespace"] = ns_obj.metadata.name
            try:
                created = await rbac_api.create_namespaced_role_binding(
                    namespace=ns_obj.metadata.name, body=b,
                )
            except ApiException as e:
                if e.status != 409:
                    logger.warning(
                        f"Failed to create binding in {ns_obj.metadata.name}: {e}"
                    )

        # No environments is OK now — access is persisted in ConfigMap

    logger.info(f"Added {access.scope} access: {binding_name} to folder {name}")
    return FolderAccessEntry(
        id=binding_name,
        type=access.type,
        name=access.name,
        role=access.role,
        scope=access.scope,
        environment=access.environment if access.scope == "environment" else None,
        folder=name,
        inherited=False,
        created=(
            created.metadata.creation_timestamp.isoformat()
            if created and created.metadata.creation_timestamp
            else None
        ),
    )


@router.patch("/{name}/access", response_model=FolderAccessSpec)
async def patch_folder_access(
    request: Request,
    name: str,
    patch: FolderAccessPatchRequest,
    user: User = Depends(require_folder_admin()),
) -> FolderAccessSpec:
    """Patch the Phase 2 access block on a folder (group ACL).

    Any field left `None` is left untouched.  To clear a list, send `[]`.
    `env_access` is full-replace when provided.

    After persistence, materialises K8s RoleBindings in each env namespace
    via the reconciler (Task #4 wires this in — for now we just persist
    the ConfigMap; reconciler.run() is the next hook point).
    """
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    meta = folders[name]
    current = meta.get("access") or {}

    # Patch — preserve fields that were not sent (omitted -> keep existing).
    new_access: dict = {
        "admins":  list(current.get("admins")  or []),
        "members": list(current.get("members") or []),
        "viewers": list(current.get("viewers") or []),
        "env_access": dict(current.get("env_access") or {}),
    }
    if patch.admins is not None:
        new_access["admins"] = list(patch.admins)
    if patch.members is not None:
        new_access["members"] = list(patch.members)
    if patch.viewers is not None:
        new_access["viewers"] = list(patch.viewers)
    if patch.env_access is not None:
        new_access["env_access"] = {
            env: spec.model_dump() for env, spec in patch.env_access.items()
        }

    meta["access"] = new_access
    save_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
    await _save_folder_meta(k8s_client, name, save_meta)
    logger.info(f"Patched access on folder {name} by {user.email}")

    # Materialise into K8s RoleBindings on every env namespace under the folder.
    # Failures are logged inside the reconciler — the ConfigMap is the
    # source of truth, so a stale RB will heal on the next reconcile.
    await reconcile_folder_rbac(k8s_client, name, save_meta)

    return FolderAccessSpec(**new_access)


@router.delete("/{name}/access/{binding_id}", status_code=204)
async def remove_folder_access(
    request: Request,
    name: str,
    binding_id: str,
    user: User = Depends(require_folder_admin()),
):
    """Remove access from a folder (deletes binding from all descendant namespaces if folder-scope)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    rbac_api = await _get_rbac_api(k8s_client)

    # Get all namespaces for this folder and descendants
    all_ns = await _get_descendant_namespaces(k8s_client, folders, name)

    deleted = False
    for ns_obj in all_ns:
        try:
            binding = await rbac_api.read_namespaced_role_binding(
                name=binding_id, namespace=ns_obj.metadata.name,
            )
            if (binding.metadata.labels or {}).get(ACCESS_MANAGED_LABEL) == "true":
                await rbac_api.delete_namespaced_role_binding(
                    name=binding_id, namespace=ns_obj.metadata.name,
                )
                deleted = True
        except ApiException:
            pass

    # Also remove from ConfigMap persisted access entries
    if name in folders:
        meta = folders[name]
        saved_entries = meta.get("access_entries", [])
        new_entries = [e for e in saved_entries if e.get("binding_name") != binding_id]
        if len(new_entries) != len(saved_entries):
            meta["access_entries"] = new_entries
            save_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
            await _save_folder_meta(k8s_client, name, save_meta)
            deleted = True

    if not deleted:
        raise HTTPException(status_code=404, detail="Access entry not found")
    logger.info(f"Removed access: {binding_id} from folder {name}")


# ---------------------------------------------------------------------------
# Quota validation
# ---------------------------------------------------------------------------

def _validate_child_quota(child: FolderQuota, parent: FolderQuota):
    """Validate that child quota does not exceed parent."""
    if parent.cpu and child.cpu:
        if _parse_cpu(child.cpu) > _parse_cpu(parent.cpu):
            raise HTTPException(
                status_code=400,
                detail=f"Child CPU quota ({child.cpu}) exceeds parent ({parent.cpu})",
            )
    if parent.memory and child.memory:
        if _parse_storage(child.memory) > _parse_storage(parent.memory):
            raise HTTPException(
                status_code=400,
                detail=f"Child memory quota ({child.memory}) exceeds parent ({parent.memory})",
            )
    if parent.storage and child.storage:
        if _parse_storage(child.storage) > _parse_storage(parent.storage):
            raise HTTPException(
                status_code=400,
                detail=f"Child storage quota ({child.storage}) exceeds parent ({parent.storage})",
            )


def _parse_cpu(value: str) -> float:
    """Parse CPU value (e.g. '4', '500m')."""
    if value.endswith("m"):
        return float(value[:-1]) / 1000
    return float(value)


# ---------------------------------------------------------------------------
# Migration: projects → folders
# ---------------------------------------------------------------------------

PROJECTS_CONFIGMAP = "kubevirt-ui-projects"
ENV_PROJECT_LABEL = "kubevirt-ui.io/project"


async def migrate_projects_to_folders(k8s_client: Any) -> list[str]:
    """Migrate all projects to root-level folders.

    - Creates a folder for each project in the folders ConfigMap.
    - Updates environment namespace labels to use folder label.
    - Preserves existing RBAC bindings (adds folder label).
    Returns list of migrated project names.
    """
    # Read projects ConfigMap
    try:
        projects_cm = await k8s_client.core_api.read_namespaced_config_map(
            name=PROJECTS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        )
        projects_data = projects_cm.data or {}
    except ApiException as e:
        if e.status == 404:
            return []
        raise

    if not projects_data:
        return []

    # Read/create folders ConfigMap
    folders_data = await _ensure_folders_configmap(k8s_client)
    now = datetime.now(timezone.utc).isoformat()
    migrated: list[str] = []
    rbac_api = await _get_rbac_api(k8s_client)

    for name, raw in projects_data.items():
        # Skip if folder already exists
        if name in folders_data:
            logger.info(f"Folder '{name}' already exists, skipping migration")
            continue

        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}

        # Create folder entry (root-level, parent_id=null)
        folder_meta = {
            "display_name": meta.get("display_name", name),
            "description": meta.get("description", ""),
            "parent_id": None,
            "created_by": meta.get("created_by"),
            "created_at": now,
        }
        if meta.get("quota"):
            folder_meta["quota"] = meta["quota"]

        await _save_folder_meta(k8s_client, name, folder_meta)

        # Update environment namespace labels: add folder label
        try:
            ns_list = await k8s_client.core_api.list_namespace(
                label_selector=f"{ENV_PROJECT_LABEL}={name}",
            )
            for ns in ns_list.items:
                patch = {
                    "metadata": {
                        "labels": {
                            ENV_FOLDER_LABEL: name,
                        },
                    },
                }
                try:
                    await k8s_client.core_api.patch_namespace(
                        name=ns.metadata.name, body=patch,
                    )
                except ApiException as e:
                    logger.warning(
                        f"Failed to update namespace {ns.metadata.name} labels: {e}"
                    )

                # Update RBAC bindings: add folder label
                try:
                    bindings = await rbac_api.list_namespaced_role_binding(
                        namespace=ns.metadata.name,
                        label_selector=f"kubevirt-ui.io/managed=true,kubevirt-ui.io/project={name}",
                    )
                    for b in bindings.items:
                        rb_patch = {
                            "metadata": {
                                "labels": {
                                    ACCESS_FOLDER_LABEL: name,
                                    ACCESS_SCOPE_LABEL: (
                                        (b.metadata.labels or {})
                                        .get("kubevirt-ui.io/access-scope", "folder")
                                        .replace("project", "folder")
                                    ),
                                },
                            },
                        }
                        try:
                            await rbac_api.patch_namespaced_role_binding(
                                name=b.metadata.name,
                                namespace=ns.metadata.name,
                                body=rb_patch,
                            )
                        except ApiException as e:
                            logger.warning(
                                f"Failed to update RoleBinding {b.metadata.name}: {e}"
                            )
                except ApiException:
                    pass
        except ApiException:
            pass

        migrated.append(name)
        logger.info(f"Migrated project '{name}' to folder")

    return migrated


@router.post("/migrate-from-projects", status_code=200)
async def migrate_from_projects(request: Request, user: User = Depends(require_admin)):
    """Migrate all projects to root-level folders. Idempotent."""
    k8s_client = request.app.state.k8s_client
    migrated = await migrate_projects_to_folders(k8s_client)
    return {"migrated": migrated, "count": len(migrated)}
