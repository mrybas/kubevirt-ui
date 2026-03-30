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

from app.core.auth import User, require_auth, check_folder_access

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
    FolderAccessEntry,
    FolderAccessListResponse,
    AddFolderAccessRequest,
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
# Helpers — RBAC propagation
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

    # Index namespaces by folder label
    ns_by_folder: dict[str, list] = {}
    for ns in all_ns.items:
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
async def create_folder(request: Request, folder: FolderCreateRequest, user: User = Depends(require_auth)):
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

    # List environments for this folder
    ns_items = await _get_folder_namespaces(k8s_client, name)

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
    )


@router.patch("/{name}", response_model=FolderResponse)
async def update_folder(request: Request, name: str, update: FolderUpdateRequest, user: User = Depends(require_auth)):
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
        q = update.quota.model_dump(exclude_none=True)
        meta["quota"] = q if q else None

    # Remove internal keys before saving
    save_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
    await _save_folder_meta(k8s_client, name, save_meta)
    logger.info(f"Updated folder: {name}")

    return await get_folder(request, name)


@router.delete("/{name}", status_code=204)
async def delete_folder(request: Request, name: str, cascade: bool = False, user: User = Depends(require_auth)):
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
async def move_folder(request: Request, name: str, move: FolderMoveRequest, user: User = Depends(require_auth)):
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

    # Propagate folder-level access (including ancestors) to new environment
    await _propagate_folder_access(k8s_client, folders, folder_name, ns_name)

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
    request: Request, name: str, env: AddFolderEnvironmentRequest, user: User = Depends(require_auth),
):
    """Add an environment (namespace) to a folder."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_folders_configmap(k8s_client)
    folders = _parse_all_folders(data)

    if name not in folders:
        raise HTTPException(status_code=404, detail="Folder not found")

    return await _create_environment_ns(
        k8s_client, folders, name, env.environment,
        env.quota_cpu, env.quota_memory, env.quota_storage,
    )


@router.delete("/{name}/environments/{environment}", status_code=204)
async def remove_environment(request: Request, name: str, environment: str, user: User = Depends(require_auth)):
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
    request: Request, name: str, access: AddFolderAccessRequest, user: User = Depends(require_auth),
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


@router.delete("/{name}/access/{binding_id}", status_code=204)
async def remove_folder_access(request: Request, name: str, binding_id: str, user: User = Depends(require_auth)):
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
async def migrate_from_projects(request: Request, user: User = Depends(require_auth)):
    """Migrate all projects to root-level folders. Idempotent."""
    k8s_client = request.app.state.k8s_client
    migrated = await migrate_projects_to_folders(k8s_client)
    return {"migrated": migrated, "count": len(migrated)}
