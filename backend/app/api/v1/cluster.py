"""Cluster API endpoints."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel

from app.core.auth import User, require_auth
from app.core.groups import get_user_namespaces
from app.models.cluster import ClusterStatusResponse, NodeListResponse, NodeResponse, NodeResourceUsage

router = APIRouter()
logger = logging.getLogger(__name__)

SETTINGS_CONFIGMAP = "kubevirt-ui-settings"
SETTINGS_NAMESPACE = "kubevirt-ui-system"

# Default cluster settings
DEFAULT_SETTINGS = {
    "cpu_overcommit": 1,  # 1 = no overcommit, 2 = 2:1, etc.
}


class ClusterSettings(BaseModel):
    """Global cluster settings."""
    cpu_overcommit: int = 1


async def get_cluster_settings(core_api) -> ClusterSettings:
    """Read cluster settings from ConfigMap. Returns defaults if not found."""
    try:
        cm = await core_api.read_namespaced_config_map(
            name=SETTINGS_CONFIGMAP, namespace=SETTINGS_NAMESPACE,
        )
        raw = json.loads(cm.data.get("settings", "{}")) if cm.data else {}
        return ClusterSettings(**{**DEFAULT_SETTINGS, **raw})
    except ApiException as e:
        if e.status == 404:
            return ClusterSettings(**DEFAULT_SETTINGS)
        raise


class ResourceUsage(BaseModel):
    """Resource usage model."""
    used: str
    total: str
    percentage: float


class SchedulableSlot(BaseModel):
    """Largest block of free resources available on a single node."""
    cpu_cores: float
    memory_gi: float
    node: str


class UserResourcesResponse(BaseModel):
    """User resources response model."""
    vms_total: int
    vms_running: int
    cpu: ResourceUsage
    memory: ResourceUsage
    storage: ResourceUsage
    max_schedulable: SchedulableSlot | None = None


def _parse_cpu(cpu_str: str) -> float:
    """Parse K8s CPU string to cores (float). E.g. '8' -> 8.0, '500m' -> 0.5."""
    if not cpu_str:
        return 0
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1]) / 1000
    return float(cpu_str)


def _parse_memory_gi(mem_str: str) -> float:
    """Parse K8s memory string to GiB (float). E.g. '16Gi' -> 16, '8192Mi' -> 8."""
    if not mem_str:
        return 0
    if mem_str.endswith("Ki"):
        return int(mem_str[:-2]) / 1024 / 1024
    if mem_str.endswith("Mi"):
        return int(mem_str[:-2]) / 1024
    if mem_str.endswith("Gi"):
        return float(mem_str[:-2])
    if mem_str.endswith("Ti"):
        return float(mem_str[:-2]) * 1024
    # Plain bytes
    try:
        return int(mem_str) / 1024 / 1024 / 1024
    except ValueError:
        return 0


def _parse_storage_gi(s: str) -> float:
    """Parse K8s storage string to GiB."""
    if not s:
        return 0
    if s.endswith("Ti"):
        return float(s[:-2]) * 1024
    if s.endswith("Gi"):
        return float(s[:-2])
    if s.endswith("Mi"):
        return float(s[:-2]) / 1024
    try:
        return int(s) / 1024 / 1024 / 1024
    except ValueError:
        return 0


@router.get("/resources", response_model=UserResourcesResponse)
async def get_user_resources(
    request: Request,
    user: User = Depends(require_auth),
) -> UserResourcesResponse:
    """Get cluster resource usage based on all pod requests vs node allocatable."""
    k8s_client = request.app.state.k8s_client

    try:
        # --- VM counts ---
        # Use the same RBAC-filtered namespace discovery as the VM list page
        accessible_ns = await get_user_namespaces(k8s_client, user)

        total_vms = 0
        running_vms = 0
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        for ns in accessible_ns:
            try:
                vms = await custom_api.list_namespaced_custom_object(
                    group="kubevirt.io", version="v1",
                    namespace=ns, plural="virtualmachines",
                )
                total_vms += len(vms.get("items", []))

                vmis = await custom_api.list_namespaced_custom_object(
                    group="kubevirt.io", version="v1",
                    namespace=ns, plural="virtualmachineinstances",
                )
                running_vms += len(vmis.get("items", []))
            except ApiException:
                continue

        # --- CPU / Memory: allocatable vs requests ---
        # Only count schedulable nodes (no NoSchedule taints) for capacity,
        # so the percentage reflects resources actually available for VMs.
        core_api = k8s_client.core_api
        nodes_result = await core_api.list_node()

        total_cpu = 0.0
        total_memory_gi = 0.0
        schedulable_node_names: set[str] = set()
        # Track per-node allocatable for max-slot calculation
        node_alloc: dict[str, dict[str, float]] = {}

        for node in nodes_result.items:
            # Skip nodes with NoSchedule taints (e.g. control-plane)
            taints = node.spec.taints or []
            has_no_schedule = any(
                t.effect in ("NoSchedule", "NoExecute") for t in taints
            )
            if has_no_schedule:
                continue

            name = node.metadata.name
            schedulable_node_names.add(name)
            alloc = node.status.allocatable or {}
            cpu_val = _parse_cpu(alloc.get("cpu", "0"))
            mem_val = _parse_memory_gi(alloc.get("memory", "0"))
            total_cpu += cpu_val
            total_memory_gi += mem_val
            node_alloc[name] = {"cpu": cpu_val, "mem": mem_val}

        # Sum resource requests from running pods on schedulable nodes only
        all_pods = await core_api.list_pod_for_all_namespaces(
            field_selector="status.phase=Running",
        )

        requested_cpu = 0.0
        requested_memory_gi = 0.0
        # Track per-node requests for max-slot calculation
        node_req: dict[str, dict[str, float]] = {n: {"cpu": 0.0, "mem": 0.0} for n in schedulable_node_names}

        for pod in all_pods.items:
            node_name = pod.spec.node_name
            if node_name not in schedulable_node_names:
                continue
            for container in pod.spec.containers or []:
                req = (container.resources or client.V1ResourceRequirements()).requests or {}
                cpu_r = _parse_cpu(req.get("cpu", "0"))
                mem_r = _parse_memory_gi(req.get("memory", "0"))
                requested_cpu += cpu_r
                requested_memory_gi += mem_r
                node_req[node_name]["cpu"] += cpu_r
                node_req[node_name]["mem"] += mem_r

        # Find the node with the most free CPU (largest schedulable slot)
        max_slot: SchedulableSlot | None = None
        for name in schedulable_node_names:
            free_cpu = node_alloc[name]["cpu"] - node_req.get(name, {}).get("cpu", 0)
            free_mem = node_alloc[name]["mem"] - node_req.get(name, {}).get("mem", 0)
            if max_slot is None or free_cpu > max_slot.cpu_cores:
                max_slot = SchedulableSlot(
                    cpu_cores=round(free_cpu, 2),
                    memory_gi=round(free_mem, 1),
                    node=name,
                )

        # --- Storage: PV capacity vs PVC usage ---
        pvs = await core_api.list_persistent_volume()
        total_storage_gi = 0.0
        for pv in pvs.items:
            cap = (pv.spec.capacity or {}).get("storage", "0")
            total_storage_gi += _parse_storage_gi(cap)

        used_storage_gi = 0.0
        for ns in accessible_ns:
            try:
                pvcs = await core_api.list_namespaced_persistent_volume_claim(namespace=ns)
                for pvc in pvcs.items:
                    cap = (pvc.status.capacity or {}).get("storage", "0")
                    used_storage_gi += _parse_storage_gi(cap)
            except ApiException:
                continue

        return UserResourcesResponse(
            vms_total=total_vms,
            vms_running=running_vms,
            cpu=ResourceUsage(
                used=f"{requested_cpu:.1f}",
                total=f"{total_cpu:.0f}",
                percentage=round((requested_cpu / total_cpu * 100) if total_cpu > 0 else 0, 1),
            ),
            memory=ResourceUsage(
                used=f"{requested_memory_gi:.1f}Gi",
                total=f"{total_memory_gi:.0f}Gi",
                percentage=round((requested_memory_gi / total_memory_gi * 100) if total_memory_gi > 0 else 0, 1),
            ),
            storage=ResourceUsage(
                used=f"{used_storage_gi:.0f}Gi",
                total=f"{total_storage_gi:.0f}Gi",
                percentage=round((used_storage_gi / total_storage_gi * 100) if total_storage_gi > 0 else 0, 1),
            ),
            max_schedulable=max_slot,
        )

    except Exception as e:
        logger.error(f"Failed to get user resources: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user resources",
        )


class ActivityItem(BaseModel):
    """Activity item model."""
    id: str
    type: str  # vm_started, vm_stopped, image_imported, etc.
    message: str
    resource_name: str
    resource_namespace: str
    timestamp: str
    icon: str


class RecentActivityResponse(BaseModel):
    """Recent activity response model."""
    items: list[ActivityItem]


@router.get("/activity", response_model=RecentActivityResponse)
async def get_recent_activity(
    request: Request,
    user: User = Depends(require_auth),
    limit: int = 10,
) -> RecentActivityResponse:
    """Get recent activity from Kubernetes events."""
    k8s_client = request.app.state.k8s_client
    
    try:
        # Use the same RBAC-filtered namespace discovery as the VM list page
        accessible_ns = await get_user_namespaces(k8s_client, user)
        
        activities = []
        
        for ns in accessible_ns[:5]:  # Limit namespaces to check
            try:
                events = await k8s_client.core_api.list_namespaced_event(
                    namespace=ns,
                    limit=20,
                )
                
                for event in events.items:
                    involved = event.involved_object
                    if not involved:
                        continue
                    
                    # Filter relevant events
                    kind = involved.kind or ""
                    if kind not in ("VirtualMachine", "VirtualMachineInstance", "DataVolume", "Pod",
                                    "VirtualMachineSnapshot", "VirtualMachineRestore", "VolumeSnapshot"):
                        continue
                    
                    # Determine activity type and icon
                    reason = event.reason or ""
                    message = event.message or ""
                    
                    if kind == "VirtualMachine" or kind == "VirtualMachineInstance":
                        if "Started" in reason or "Running" in message:
                            activity_type = "vm_started"
                            icon = "play"
                        elif "Stopped" in reason or "Succeeded" in reason:
                            activity_type = "vm_stopped"
                            icon = "square"
                        elif "Created" in reason:
                            activity_type = "vm_created"
                            icon = "plus"
                        elif "Deleted" in reason:
                            activity_type = "vm_deleted"
                            icon = "trash"
                        else:
                            activity_type = "vm_event"
                            icon = "server"
                    elif kind == "DataVolume":
                        if "ImportSucceeded" in reason or "Succeeded" in reason:
                            activity_type = "image_imported"
                            icon = "check"
                        elif "ImportScheduled" in reason or "ImportInProgress" in reason:
                            activity_type = "image_importing"
                            icon = "download"
                        else:
                            activity_type = "storage_event"
                            icon = "hard-drive"
                    elif kind in ("VirtualMachineSnapshot", "VirtualMachineRestore"):
                        activity_type = "snapshot"
                        icon = "camera"
                    elif kind == "VolumeSnapshot":
                        activity_type = "volume_snapshot"
                        icon = "save"
                    else:
                        continue
                    
                    timestamp = event.last_timestamp or event.event_time
                    if timestamp:
                        timestamp = timestamp.isoformat()
                    else:
                        timestamp = ""
                    
                    activities.append(ActivityItem(
                        id=f"{ns}-{involved.name}-{event.metadata.uid or ''}",
                        type=activity_type,
                        message=f"{reason}: {message[:100]}",
                        resource_name=involved.name or "",
                        resource_namespace=ns,
                        timestamp=timestamp,
                        icon=icon,
                    ))
            except ApiException:
                continue
        
        # Sort by timestamp and limit
        activities.sort(key=lambda x: x.timestamp, reverse=True)
        return RecentActivityResponse(items=activities[:limit])
    
    except Exception as e:
        logger.error(f"Failed to get recent activity: {e}")
        return RecentActivityResponse(items=[])


@router.get("/status", response_model=ClusterStatusResponse)
async def get_cluster_status(request: Request, user: User = Depends(require_auth)) -> ClusterStatusResponse:
    """Get cluster status including KubeVirt and CDI."""
    k8s_client = request.app.state.k8s_client

    try:
        kubevirt_status = await k8s_client.get_kubevirt_status()
        cdi_status = await k8s_client.get_cdi_status()
        nodes = await k8s_client.list_nodes()

        return ClusterStatusResponse(
            kubevirt=kubevirt_status,
            cdi=cdi_status,
            nodes_count=len(nodes),
            nodes_ready=sum(1 for n in nodes if n["status"] == "Ready"),
        )

    except Exception as e:
        logger.error(f"Failed to get cluster status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get cluster status",
        )


@router.get("/nodes", response_model=NodeListResponse)
async def list_nodes(request: Request, user: User = Depends(require_auth)) -> NodeListResponse:
    """List all cluster nodes with per-node resource usage."""
    k8s_client = request.app.state.k8s_client

    try:
        nodes = await k8s_client.list_nodes()
        core_api = k8s_client.core_api

        # Build allocatable map per node
        node_alloc: dict[str, dict[str, float]] = {}
        nodes_raw = await core_api.list_node()
        for n in nodes_raw.items:
            alloc = n.status.allocatable or {}
            node_alloc[n.metadata.name] = {
                "cpu": _parse_cpu(alloc.get("cpu", "0")),
                "mem": _parse_memory_gi(alloc.get("memory", "0")),
            }

        # Sum pod requests per node
        node_req: dict[str, dict[str, float]] = {
            name: {"cpu": 0.0, "mem": 0.0} for name in node_alloc
        }
        all_pods = await core_api.list_pod_for_all_namespaces(
            field_selector="status.phase=Running",
        )
        for pod in all_pods.items:
            node_name = pod.spec.node_name
            if node_name not in node_req:
                continue
            for container in pod.spec.containers or []:
                req = (container.resources or client.V1ResourceRequirements()).requests or {}
                node_req[node_name]["cpu"] += _parse_cpu(req.get("cpu", "0"))
                node_req[node_name]["mem"] += _parse_memory_gi(req.get("memory", "0"))

        node_responses = []
        for node in nodes:
            name = node["name"]
            alloc = node_alloc.get(name, {"cpu": 0, "mem": 0})
            req = node_req.get(name, {"cpu": 0, "mem": 0})

            cpu_total = alloc["cpu"]
            cpu_used = req["cpu"]
            mem_total = alloc["mem"]
            mem_used = req["mem"]

            node_responses.append(
                NodeResponse(
                    name=name,
                    status=node["status"],
                    roles=node["roles"],
                    version=node["version"],
                    os=node["os"],
                    cpu=node["cpu"],
                    memory=node["memory"],
                    internal_ip=node["internal_ip"],
                    cpu_usage=NodeResourceUsage(
                        total=round(cpu_total, 2),
                        used=round(cpu_used, 2),
                        free=round(max(cpu_total - cpu_used, 0), 2),
                        percentage=round(cpu_used / cpu_total * 100, 1) if cpu_total > 0 else 0,
                    ),
                    memory_usage=NodeResourceUsage(
                        total=round(mem_total, 1),
                        used=round(mem_used, 1),
                        free=round(max(mem_total - mem_used, 0), 1),
                        percentage=round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0,
                    ),
                )
            )

        return NodeListResponse(items=node_responses, total=len(node_responses))

    except ApiException as e:
        logger.error(f"Failed to list nodes: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list nodes: {e.reason}",
        )


@router.get("/settings", response_model=ClusterSettings)
async def read_settings(
    request: Request,
    user: User = Depends(require_auth),
) -> ClusterSettings:
    """Get global cluster settings."""
    k8s_client = request.app.state.k8s_client
    return await get_cluster_settings(k8s_client.core_api)


@router.put("/settings", response_model=ClusterSettings)
async def update_settings(
    request: Request,
    settings: ClusterSettings,
    user: User = Depends(require_auth),
) -> ClusterSettings:
    """Update global cluster settings."""
    k8s_client = request.app.state.k8s_client
    core_api = k8s_client.core_api

    data = {"settings": json.dumps(settings.model_dump())}

    try:
        await core_api.read_namespaced_config_map(
            name=SETTINGS_CONFIGMAP, namespace=SETTINGS_NAMESPACE,
        )
        # Update existing
        await core_api.patch_namespaced_config_map(
            name=SETTINGS_CONFIGMAP,
            namespace=SETTINGS_NAMESPACE,
            body={"data": data},
        )
    except ApiException as e:
        if e.status == 404:
            # Create new
            await core_api.create_namespaced_config_map(
                namespace=SETTINGS_NAMESPACE,
                body=client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(
                        name=SETTINGS_CONFIGMAP,
                        namespace=SETTINGS_NAMESPACE,
                    ),
                    data=data,
                ),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update settings: {e.reason}",
            )

    logger.info(f"Cluster settings updated by {user.username}: {settings}")
    return settings
