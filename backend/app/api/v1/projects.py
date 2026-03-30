"""Projects API endpoints (backward compatibility layer).

Architecture:
  - Project = logical grouping stored in ConfigMap (no own namespace)
  - Environment = K8s namespace belonging to a project
  - Access = RBAC at project level (all envs) or environment level

NOTE: Projects are being replaced by hierarchical Folders.
This API continues to work for backward compatibility.
After migration, projects are equivalent to root-level folders.
See folders.py for the new implementation.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client.rest import ApiException
from kubernetes_asyncio.client import RbacAuthorizationV1Api

from app.core.auth import User, require_auth
from app.core.groups import get_known_teams, get_known_teams_async
from app.models.project import (
    ProjectCreateRequest,
    UpdateProjectRequest,
    ProjectResponse,
    ProjectListResponse,
    ProjectQuota,
    AddEnvironmentRequest,
    EnvironmentResponse,
    AccessEntry,
    AccessListResponse,
    AddAccessRequest,
    TeamResponse,
    TeamListResponse,
    ROLE_TO_CLUSTERROLE,
    CLUSTERROLE_TO_ROLE,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ConfigMap storing project metadata
PROJECTS_CONFIGMAP = "kubevirt-ui-projects"
SYSTEM_NAMESPACE = "kubevirt-ui-system"

# Labels for managed namespaces (environments)
ENV_ENABLED_LABEL = "kubevirt-ui.io/enabled"
ENV_MANAGED_LABEL = "kubevirt-ui.io/managed"
ENV_PROJECT_LABEL = "kubevirt-ui.io/project"
ENV_ENVIRONMENT_LABEL = "kubevirt-ui.io/environment"

# Labels for managed RoleBindings
ACCESS_MANAGED_LABEL = "kubevirt-ui.io/managed"
ACCESS_TYPE_LABEL = "kubevirt-ui.io/access-type"
ACCESS_SCOPE_LABEL = "kubevirt-ui.io/access-scope"  # "project" or "environment"
ACCESS_PROJECT_LABEL = "kubevirt-ui.io/project"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns_name(project: str, environment: str) -> str:
    """Build namespace name from project + environment."""
    return f"{project}-{environment}"


async def _ensure_projects_configmap(k8s_client: Any) -> dict:
    """Read or create the projects ConfigMap. Returns data dict."""
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=PROJECTS_CONFIGMAP, namespace=SYSTEM_NAMESPACE
        )
        return cm.data or {}
    except ApiException as e:
        if e.status == 404:
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": PROJECTS_CONFIGMAP,
                    "namespace": SYSTEM_NAMESPACE,
                    "labels": {"kubevirt-ui.io/managed": "true"},
                },
                "data": {},
            }
            await k8s_client.core_api.create_namespaced_config_map(
                namespace=SYSTEM_NAMESPACE, body=body
            )
            logger.info("Created projects ConfigMap")
            return {}
        raise


async def _save_project_meta(k8s_client: Any, name: str, meta: dict):
    """Save project metadata to ConfigMap."""
    patch = {"data": {name: json.dumps(meta)}}
    await k8s_client.core_api.patch_namespaced_config_map(
        name=PROJECTS_CONFIGMAP, namespace=SYSTEM_NAMESPACE, body=patch
    )


async def _delete_project_meta(k8s_client: Any, name: str):
    """Remove project entry from ConfigMap with optimistic locking."""
    cm = await k8s_client.core_api.read_namespaced_config_map(
        name=PROJECTS_CONFIGMAP, namespace=SYSTEM_NAMESPACE
    )
    data = dict(cm.data or {})
    data.pop(name, None)
    # Use resourceVersion for optimistic concurrency control (409 on conflict)
    from kubernetes_asyncio.client import V1ConfigMap, V1ObjectMeta
    body = V1ConfigMap(
        metadata=V1ObjectMeta(
            name=PROJECTS_CONFIGMAP,
            namespace=SYSTEM_NAMESPACE,
            resource_version=cm.metadata.resource_version,
        ),
        data=data if data else {},
    )
    await k8s_client.core_api.replace_namespaced_config_map(
        name=PROJECTS_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        body=body,
    )


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
            namespace=namespace
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


def _parse_storage(value: str) -> int:
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for unit, mult in units.items():
        if value.endswith(unit):
            try:
                return int(float(value[:-len(unit)]) * mult)
            except (ValueError, TypeError):
                return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _format_storage(b: int) -> str:
    if b >= 1024**4: return f"{b / 1024**4:.1f}Ti"
    if b >= 1024**3: return f"{b / 1024**3:.1f}Gi"
    if b >= 1024**2: return f"{b / 1024**2:.1f}Mi"
    return f"{b / 1024:.1f}Ki"


async def _get_rbac_api(k8s_client: Any) -> RbacAuthorizationV1Api:
    return RbacAuthorizationV1Api(k8s_client._api_client)


async def _get_env_quotas(k8s_client: Any, ns: str) -> dict:
    q: dict[str, str | None] = {"quota_cpu": None, "quota_memory": None, "quota_storage": None}
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
    k8s_client: Any, ns_obj: Any, project_name: str
) -> EnvironmentResponse:
    """Build EnvironmentResponse from a namespace object."""
    labels = ns_obj.metadata.labels or {}
    env_name = labels.get(ENV_ENVIRONMENT_LABEL, ns_obj.metadata.name)
    stats = await _get_env_stats(k8s_client, ns_obj.metadata.name)
    quotas = await _get_env_quotas(k8s_client, ns_obj.metadata.name)
    return EnvironmentResponse(
        name=ns_obj.metadata.name,
        environment=env_name,
        project=project_name,
        created=ns_obj.metadata.creation_timestamp.isoformat()
            if ns_obj.metadata.creation_timestamp else None,
        vm_count=stats["vm_count"],
        storage_used=stats["storage_used"],
        **quotas,
    )


async def _get_project_access_summary(
    rbac_api: RbacAuthorizationV1Api,
    project_name: str,
    env_namespaces: list[str],
) -> tuple[list[str], list[str]]:
    """Aggregate unique teams and users across all environments of a project."""
    teams: list[str] = []
    users: list[str] = []
    for ns in env_namespaces:
        try:
            bindings = await rbac_api.list_namespaced_role_binding(
                namespace=ns,
                label_selector=f"{ACCESS_MANAGED_LABEL}=true,{ACCESS_PROJECT_LABEL}={project_name}"
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
# Project CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=ProjectListResponse)
async def list_projects(request: Request, user: User = Depends(require_auth)):
    """List all projects with their environments."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    # Get all managed namespaces at once
    try:
        all_ns = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_ENABLED_LABEL}=true"
        )
    except ApiException as e:
        logger.error(f"Failed to list namespaces: {e}")
        raise HTTPException(status_code=500, detail="Failed to list project environments")
    
    # Index namespaces by project label
    ns_by_project: dict[str, list] = {}
    for ns in all_ns.items:
        proj = (ns.metadata.labels or {}).get(ENV_PROJECT_LABEL)
        if proj:
            ns_by_project.setdefault(proj, []).append(ns)
    
    rbac_api = await _get_rbac_api(k8s_client)
    
    projects = []
    for name, raw in data.items():
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        
        env_ns_list = ns_by_project.get(name, [])
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
        teams, users = await _get_project_access_summary(rbac_api, name, env_ns_names)
        
        # Parse optional project quota
        quota_data = meta.get("quota")
        quota = ProjectQuota(**quota_data) if quota_data else None
        
        projects.append(ProjectResponse(
            name=name,
            display_name=meta.get("display_name", name),
            description=meta.get("description", ""),
            created_by=meta.get("created_by"),
            quota=quota,
            environments=envs,
            total_vms=total_vms,
            total_storage=_format_storage(total_bytes) if total_bytes > 0 else None,
            teams=teams,
            users=users,
        ))
    
    return ProjectListResponse(items=projects, total=len(projects))


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(request: Request, project: ProjectCreateRequest):
    """Create a project (ConfigMap entry) with optional initial environments."""
    k8s_client = request.app.state.k8s_client
    user = getattr(request.state, "user", None)
    
    data = await _ensure_projects_configmap(k8s_client)
    if project.name in data:
        raise HTTPException(status_code=409, detail=f"Project '{project.name}' already exists")
    
    # Save project metadata
    meta: dict[str, Any] = {
        "display_name": project.display_name,
        "description": project.description,
        "created_by": user.email if user else None,
    }
    if project.quota:
        meta["quota"] = project.quota.model_dump(exclude_none=True)
    await _save_project_meta(k8s_client, project.name, meta)
    logger.info(f"Created project: {project.name}")
    
    # Create initial environments
    envs = []
    for env_name in project.environments:
        env_resp = await _create_environment_ns(k8s_client, project.name, env_name)
        envs.append(env_resp)
    
    return ProjectResponse(
        name=project.name,
        display_name=project.display_name,
        description=project.description,
        created_by=meta["created_by"],
        quota=project.quota,
        environments=envs,
    )


@router.get("/{name}", response_model=ProjectResponse)
async def get_project(request: Request, name: str):
    """Get a single project with environments."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    try:
        meta = json.loads(data[name])
    except (json.JSONDecodeError, TypeError):
        meta = {}
    
    # List environments for this project
    try:
        ns_list = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_PROJECT_LABEL}={name}"
        )
    except ApiException:
        ns_list = type("obj", (), {"items": []})()
    
    envs = []
    total_vms = 0
    total_bytes = 0
    for ns_obj in ns_list.items:
        env_resp = await _build_env_response(k8s_client, ns_obj, name)
        envs.append(env_resp)
        total_vms += env_resp.vm_count
        if env_resp.storage_used:
            total_bytes += _parse_storage(env_resp.storage_used)
    
    rbac_api = await _get_rbac_api(k8s_client)
    env_ns_names = [ns.metadata.name for ns in ns_list.items]
    teams, users = await _get_project_access_summary(rbac_api, name, env_ns_names)
    
    quota_data = meta.get("quota")
    quota = ProjectQuota(**quota_data) if quota_data else None
    
    return ProjectResponse(
        name=name,
        display_name=meta.get("display_name", name),
        description=meta.get("description", ""),
        created_by=meta.get("created_by"),
        quota=quota,
        environments=envs,
        total_vms=total_vms,
        total_storage=_format_storage(total_bytes) if total_bytes > 0 else None,
        teams=teams,
        users=users,
    )


@router.patch("/{name}", response_model=ProjectResponse)
async def update_project(request: Request, name: str, update: UpdateProjectRequest):
    """Update project metadata (display name, description, quota)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    try:
        meta = json.loads(data[name])
    except (json.JSONDecodeError, TypeError):
        meta = {}
    
    if update.display_name is not None:
        meta["display_name"] = update.display_name
    if update.description is not None:
        meta["description"] = update.description
    if update.quota is not None:
        q = update.quota.model_dump(exclude_none=True)
        meta["quota"] = q if q else None
    
    await _save_project_meta(k8s_client, name, meta)
    logger.info(f"Updated project: {name}")
    
    # Return full project response
    return await get_project(request, name)


@router.delete("/{name}", status_code=204)
async def delete_project(request: Request, name: str):
    """Delete a project and all its environments."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Delete all environment namespaces
    try:
        ns_list = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_PROJECT_LABEL}={name},{ENV_MANAGED_LABEL}=true"
        )
        for ns in ns_list.items:
            try:
                await k8s_client.core_api.delete_namespace(name=ns.metadata.name)
                logger.info(f"Deleted environment namespace: {ns.metadata.name}")
            except ApiException as e:
                logger.warning(f"Failed to delete namespace {ns.metadata.name}: {e}")
    except ApiException:
        pass
    
    # Remove project from ConfigMap
    await _delete_project_meta(k8s_client, name)
    logger.info(f"Deleted project: {name}")


# ---------------------------------------------------------------------------
# Environment CRUD
# ---------------------------------------------------------------------------

async def _create_environment_ns(
    k8s_client: Any, project: str, environment: str,
    quota_cpu: str | None = None, quota_memory: str | None = None,
    quota_storage: str | None = None,
) -> EnvironmentResponse:
    """Create a namespace for an environment."""
    ns_name = _ns_name(project, environment)
    
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": ns_name,
            "labels": {
                ENV_ENABLED_LABEL: "true",
                ENV_MANAGED_LABEL: "true",
                ENV_PROJECT_LABEL: project,
                ENV_ENVIRONMENT_LABEL: environment,
            },
        },
    }
    
    try:
        created = await k8s_client.core_api.create_namespace(body=namespace)
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"Environment '{ns_name}' already exists")
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
                    "apiVersion": "v1", "kind": "ResourceQuota",
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
    
    # Propagate project-level access to new environment
    await _propagate_project_access(k8s_client, project, ns_name)
    
    logger.info(f"Created environment: {ns_name} (project={project})")
    return EnvironmentResponse(
        name=ns_name,
        environment=environment,
        project=project,
        created=created.metadata.creation_timestamp.isoformat()
            if created.metadata.creation_timestamp else None,
        quota_cpu=quota_cpu,
        quota_memory=quota_memory,
        quota_storage=quota_storage,
    )


@router.post("/{name}/environments", response_model=EnvironmentResponse, status_code=201)
async def add_environment(request: Request, name: str, env: AddEnvironmentRequest):
    """Add an environment (namespace) to a project."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    return await _create_environment_ns(
        k8s_client, name, env.environment,
        env.quota_cpu, env.quota_memory, env.quota_storage,
    )


@router.delete("/{name}/environments/{environment}", status_code=204)
async def remove_environment(request: Request, name: str, environment: str):
    """Remove an environment (delete its namespace)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    ns_name = _ns_name(name, environment)
    try:
        ns = await k8s_client.core_api.read_namespace(name=ns_name)
        labels = ns.metadata.labels or {}
        if labels.get(ENV_MANAGED_LABEL) != "true" or labels.get(ENV_PROJECT_LABEL) != name:
            raise HTTPException(status_code=403, detail="Namespace not managed by this project")
        await k8s_client.core_api.delete_namespace(name=ns_name)
        logger.info(f"Deleted environment: {ns_name}")
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail="Environment not found")
        raise HTTPException(status_code=e.status, detail=str(e.reason))


# ---------------------------------------------------------------------------
# Access CRUD (project-level and environment-level)
# ---------------------------------------------------------------------------

async def _propagate_project_access(k8s_client: Any, project: str, target_ns: str):
    """Copy all project-scope access bindings to a (new) namespace."""
    rbac_api = await _get_rbac_api(k8s_client)
    
    # Find existing project-scope bindings in any sibling env
    try:
        all_ns = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_PROJECT_LABEL}={project},{ENV_MANAGED_LABEL}=true"
        )
    except ApiException:
        return
    
    for ns in all_ns.items:
        if ns.metadata.name == target_ns:
            continue
        try:
            bindings = await rbac_api.list_namespaced_role_binding(
                namespace=ns.metadata.name,
                label_selector=f"{ACCESS_MANAGED_LABEL}=true,{ACCESS_SCOPE_LABEL}=project,{ACCESS_PROJECT_LABEL}={project}"
            )
            for b in bindings.items:
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
                        namespace=target_ns, body=new_binding
                    )
                except ApiException as e:
                    if e.status != 409:
                        logger.warning(f"Failed to propagate binding {b.metadata.name}: {e}")
            break  # Only need bindings from one sibling
        except ApiException:
            continue


@router.get("/{name}/access", response_model=AccessListResponse)
async def list_project_access(request: Request, name: str):
    """List all access entries for a project (across all environments)."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    rbac_api = await _get_rbac_api(k8s_client)
    
    try:
        all_ns = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_PROJECT_LABEL}={name}"
        )
    except ApiException:
        return AccessListResponse(items=[], total=0)
    
    seen_ids: set[str] = set()
    entries: list[AccessEntry] = []
    
    for ns in all_ns.items:
        try:
            bindings = await rbac_api.list_namespaced_role_binding(
                namespace=ns.metadata.name,
                label_selector=f"{ACCESS_MANAGED_LABEL}=true,{ACCESS_PROJECT_LABEL}={name}"
            )
            for b in bindings.items:
                bid = b.metadata.name
                if bid in seen_ids:
                    continue
                seen_ids.add(bid)
                
                labels = b.metadata.labels or {}
                scope = labels.get(ACCESS_SCOPE_LABEL, "project")
                atype = labels.get(ACCESS_TYPE_LABEL, "unknown")
                role = CLUSTERROLE_TO_ROLE.get(b.role_ref.name, "custom")
                env_label = (ns.metadata.labels or {}).get(ENV_ENVIRONMENT_LABEL)
                
                for s in b.subjects or []:
                    entries.append(AccessEntry(
                        id=bid,
                        type=atype,
                        name=s.name,
                        role=role,
                        scope=scope,
                        environment=env_label if scope == "environment" else None,
                        created=b.metadata.creation_timestamp.isoformat()
                            if b.metadata.creation_timestamp else None,
                    ))
        except ApiException:
            pass
    
    return AccessListResponse(items=entries, total=len(entries))


@router.post("/{name}/access", response_model=AccessEntry, status_code=201)
async def add_project_access(request: Request, name: str, access: AddAccessRequest):
    """Add access to a project or specific environment."""
    k8s_client = request.app.state.k8s_client
    data = await _ensure_projects_configmap(k8s_client)
    
    if name not in data:
        raise HTTPException(status_code=404, detail="Project not found")
    
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
        ACCESS_PROJECT_LABEL: name,
    }
    
    binding_body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": binding_name, "labels": binding_labels},
        "subjects": [{"kind": subject_kind, "name": access.name, "apiGroup": "rbac.authorization.k8s.io"}],
        "roleRef": {"kind": "ClusterRole", "name": cluster_role, "apiGroup": "rbac.authorization.k8s.io"},
    }
    
    rbac_api = await _get_rbac_api(k8s_client)
    
    if access.scope == "environment":
        # Single environment
        if not access.environment:
            raise HTTPException(status_code=400, detail="environment is required for environment-scope access")
        target_ns = _ns_name(name, access.environment)
        binding_body["metadata"]["namespace"] = target_ns
        try:
            created = await rbac_api.create_namespaced_role_binding(
                namespace=target_ns, body=binding_body
            )
        except ApiException as e:
            if e.status == 409:
                raise HTTPException(status_code=409, detail="Access already exists")
            raise HTTPException(status_code=e.status, detail=str(e.reason))
    else:
        # Project scope — create in ALL environment namespaces
        try:
            ns_list = await k8s_client.core_api.list_namespace(
                label_selector=f"{ENV_PROJECT_LABEL}={name}"
            )
        except ApiException:
            raise HTTPException(status_code=500, detail="Failed to list project environments")
        
        created = None
        for ns in ns_list.items:
            b = dict(binding_body)
            b["metadata"] = dict(binding_body["metadata"])
            b["metadata"]["namespace"] = ns.metadata.name
            try:
                created = await rbac_api.create_namespaced_role_binding(
                    namespace=ns.metadata.name, body=b
                )
            except ApiException as e:
                if e.status != 409:
                    logger.warning(f"Failed to create binding in {ns.metadata.name}: {e}")
        
        if created is None:
            raise HTTPException(status_code=400, detail="No environments in project to assign access to")
    
    logger.info(f"Added {access.scope} access: {binding_name} to project {name}")
    return AccessEntry(
        id=binding_name,
        type=access.type,
        name=access.name,
        role=access.role,
        scope=access.scope,
        environment=access.environment if access.scope == "environment" else None,
        created=created.metadata.creation_timestamp.isoformat()
            if created and created.metadata.creation_timestamp else None,
    )


@router.delete("/{name}/access/{binding_id}", status_code=204)
async def remove_project_access(request: Request, name: str, binding_id: str):
    """Remove access from a project (deletes binding from all environments if project-scope)."""
    k8s_client = request.app.state.k8s_client
    rbac_api = await _get_rbac_api(k8s_client)
    
    # Find all namespaces for this project
    try:
        ns_list = await k8s_client.core_api.list_namespace(
            label_selector=f"{ENV_PROJECT_LABEL}={name}"
        )
    except ApiException:
        raise HTTPException(status_code=404, detail="Project not found")
    
    deleted = False
    for ns in ns_list.items:
        try:
            binding = await rbac_api.read_namespaced_role_binding(
                name=binding_id, namespace=ns.metadata.name
            )
            if (binding.metadata.labels or {}).get(ACCESS_MANAGED_LABEL) == "true":
                await rbac_api.delete_namespaced_role_binding(
                    name=binding_id, namespace=ns.metadata.name
                )
                deleted = True
        except ApiException:
            pass
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Access entry not found")
    logger.info(f"Removed access: {binding_id} from project {name}")


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

teams_router = APIRouter()


@teams_router.get("", response_model=TeamListResponse)
async def list_teams(request: Request, user: User = Depends(require_auth)):
    """List known teams from LLDAP or fallback."""
    teams = await get_known_teams_async()
    return TeamListResponse(
        items=[TeamResponse(**t) for t in teams],
        total=len(teams),
    )
