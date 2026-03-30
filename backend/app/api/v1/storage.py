"""Storage API endpoints - DataVolumes, PVCs."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel

from app.core.auth import User, require_auth
from app.core.constants import parse_k8s_capacity as _parse_capacity
from app.models.storage import (
    DataVolumeCreateRequest,
    DataVolumeListResponse,
    DataVolumeResponse,
    PVCListResponse,
    PVCResponse,
    StorageClassDetailResponse,
    StorageClassListResponse,
    dv_from_k8s,
    pvc_from_k8s,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# CDI API constants
CDI_API_GROUP = "cdi.kubevirt.io"
CDI_API_VERSION = "v1beta1"


class ImageUsageResponse(BaseModel):
    """Image usage response model."""
    name: str
    namespace: str
    display_name: str | None = None
    description: str | None = None
    size: str | None = None
    status: str  # Ready, InUse, Pending
    source_type: str | None = None
    source_url: str | None = None
    used_by_vms: list[str]
    created: str | None = None


@router.get("/datavolumes", response_model=DataVolumeListResponse)
async def list_datavolumes(request: Request, namespace: str) -> DataVolumeListResponse:
    """List all DataVolumes in a namespace."""
    k8s_client = request.app.state.k8s_client

    try:
        result = await k8s_client.custom_api.list_namespaced_custom_object(
            group=CDI_API_GROUP,
            version=CDI_API_VERSION,
            namespace=namespace,
            plural="datavolumes",
        )
        items = [dv_from_k8s(dv) for dv in result.get("items", [])]
        return DataVolumeListResponse(items=items, total=len(items))

    except ApiException as e:
        if e.status == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to namespace {namespace}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list DataVolumes: {e.reason}",
        )


@router.get("/datavolumes/{name}", response_model=DataVolumeResponse)
async def get_datavolume(
    request: Request, namespace: str, name: str
) -> DataVolumeResponse:
    """Get a specific DataVolume."""
    k8s_client = request.app.state.k8s_client

    try:
        dv = await k8s_client.custom_api.get_namespaced_custom_object(
            group=CDI_API_GROUP,
            version=CDI_API_VERSION,
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
        return dv_from_k8s(dv)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"DataVolume {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get DataVolume: {e.reason}",
        )


@router.post(
    "/datavolumes", response_model=DataVolumeResponse, status_code=status.HTTP_201_CREATED
)
async def create_datavolume(
    request: Request, namespace: str, dv_request: DataVolumeCreateRequest
) -> DataVolumeResponse:
    """Create a new DataVolume."""
    k8s_client = request.app.state.k8s_client

    # Build DataVolume manifest
    dv_manifest = dv_request.to_k8s_manifest(namespace)

    try:
        created_dv = await k8s_client.custom_api.create_namespaced_custom_object(
            group=CDI_API_GROUP,
            version=CDI_API_VERSION,
            namespace=namespace,
            plural="datavolumes",
            body=dv_manifest,
        )
        return dv_from_k8s(created_dv)

    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"DataVolume {dv_request.name} already exists",
            )
        if e.status == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to create DataVolume",
            )
        logger.error(f"Failed to create DataVolume: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create DataVolume: {e.reason}",
        )


@router.delete("/datavolumes/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_datavolume(request: Request, namespace: str, name: str) -> None:
    """Delete a DataVolume."""
    k8s_client = request.app.state.k8s_client

    try:
        await k8s_client.custom_api.delete_namespaced_custom_object(
            group=CDI_API_GROUP,
            version=CDI_API_VERSION,
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"DataVolume {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete DataVolume: {e.reason}",
        )


@router.get("/pvcs", response_model=PVCListResponse)
async def list_pvcs(request: Request, namespace: str) -> PVCListResponse:
    """List all PVCs in a namespace."""
    k8s_client = request.app.state.k8s_client

    try:
        result = await k8s_client.core_api.list_namespaced_persistent_volume_claim(
            namespace=namespace
        )
        items = [pvc_from_k8s(pvc) for pvc in result.items]
        return PVCListResponse(items=items, total=len(items))

    except ApiException as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list PVCs: {e.reason}",
        )


@router.get("/storageclasses", response_model=StorageClassListResponse)
async def list_storage_classes(request: Request) -> StorageClassListResponse:
    """List all StorageClasses."""
    k8s_client = request.app.state.k8s_client

    try:
        # Use storage API
        from kubernetes_asyncio.client import StorageV1Api

        storage_api = StorageV1Api(k8s_client._api_client)
        result = await storage_api.list_storage_class()

        items = [
            {
                "name": sc.metadata.name,
                "provisioner": sc.provisioner,
                "reclaim_policy": sc.reclaim_policy,
                "volume_binding_mode": sc.volume_binding_mode,
                "allow_volume_expansion": sc.allow_volume_expansion or False,
                "is_default": sc.metadata.annotations.get(
                    "storageclass.kubernetes.io/is-default-class"
                )
                == "true"
                if sc.metadata.annotations
                else False,
            }
            for sc in result.items
        ]

        return StorageClassListResponse(items=items, total=len(items))

    except ApiException as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list StorageClasses: {e.reason}",
        )


async def _query_linstor_pools(k8s_client: Any) -> dict[str, tuple[int, int]]:
    """Query LINSTOR REST API for storage pool capacity via K8s service proxy.

    Returns dict: pool_name -> (total_bytes, free_bytes) summed across
    non-DISKLESS nodes.
    """
    import ast as _ast
    import json as _json

    try:
        svcs = await k8s_client.core_api.list_service_for_all_namespaces(
            field_selector="metadata.name=linstor-controller"
        )
        if not svcs.items:
            return {}

        ns = svcs.items[0].metadata.namespace
        resp = await k8s_client.core_api.connect_get_namespaced_service_proxy_with_path(
            name="linstor-controller:3370",
            namespace=ns,
            path="v1/view/storage-pools",
        )
        # kubernetes_asyncio proxy may return Python repr (single quotes)
        # instead of strict JSON — normalize single quotes to double quotes and parse as JSON
        try:
            pools_data = _json.loads(resp)
        except (_json.JSONDecodeError, TypeError):
            pools_data = _json.loads(resp.replace("'", '"'))

        pool_totals: dict[str, int] = {}
        pool_free: dict[str, int] = {}
        for p in pools_data:
            pn = p.get("storage_pool_name", "")
            if p.get("provider_kind", "") == "DISKLESS":
                continue
            # LINSTOR reports capacity in KiB
            pool_totals[pn] = pool_totals.get(pn, 0) + p.get("total_capacity", 0) * 1024
            pool_free[pn] = pool_free.get(pn, 0) + p.get("free_capacity", 0) * 1024

        return {
            pn: (pool_totals[pn], pool_free.get(pn, 0))
            for pn in pool_totals
        }
    except Exception as exc:
        logger.debug("LINSTOR pool query failed: %s", exc)
        return {}


@router.get("/storageclasses/details", response_model=list[StorageClassDetailResponse])
async def list_storage_classes_details(request: Request) -> list[StorageClassDetailResponse]:
    """List StorageClasses with PV/PVC capacity stats.

    Capacity resolution order per StorageClass:
    1. LINSTOR REST API (via K8s service proxy) — for ``linstor.csi.linbit.com``
    2. CSIStorageCapacity objects (K8s 1.24+)
    3. Fallback: PV/PVC sums
    """
    k8s_client = request.app.state.k8s_client

    try:
        from kubernetes_asyncio.client import StorageV1Api

        storage_api = StorageV1Api(k8s_client._api_client)
        sc_result = await storage_api.list_storage_class()

        # Check if any SC uses LINSTOR provisioner
        has_linstor = any(
            sc.provisioner == "linstor.csi.linbit.com" for sc in sc_result.items
        )

        # --- LINSTOR direct pool query ---
        linstor_pools: dict[str, tuple[int, int]] = {}
        if has_linstor:
            linstor_pools = await _query_linstor_pools(k8s_client)

        # --- CSIStorageCapacity (generic CSI) ---
        sc_pool_available: dict[str, int] = {}
        if not linstor_pools:
            try:
                csi_caps = await storage_api.list_csi_storage_capacity_for_all_namespaces()
                for cap_obj in csi_caps.items:
                    sc_name = cap_obj.storage_class_name or ""
                    cap_str = cap_obj.capacity or "0"
                    sc_pool_available[sc_name] = (
                        sc_pool_available.get(sc_name, 0) + _parse_capacity(cap_str)
                    )
            except Exception as csi_err:
                logger.debug("CSIStorageCapacity unavailable: %s", csi_err)

        # Get all PVs and PVCs
        pvs = await k8s_client.core_api.list_persistent_volume()
        pvcs = await k8s_client.core_api.list_persistent_volume_claim_for_all_namespaces()

        # Build per-SC stats from PVs / PVCs
        sc_pv_count: dict[str, int] = {}
        sc_pv_capacity: dict[str, int] = {}
        sc_pvc_count: dict[str, int] = {}
        sc_pvc_capacity: dict[str, int] = {}

        for pv in pvs.items:
            sc_name = pv.spec.storage_class_name or ""
            sc_pv_count[sc_name] = sc_pv_count.get(sc_name, 0) + 1
            cap = pv.spec.capacity.get("storage", "0") if pv.spec.capacity else "0"
            sc_pv_capacity[sc_name] = sc_pv_capacity.get(sc_name, 0) + _parse_capacity(cap)

        for pvc in pvcs.items:
            sc_name = pvc.spec.storage_class_name or ""
            sc_pvc_count[sc_name] = sc_pvc_count.get(sc_name, 0) + 1
            cap = pvc.spec.resources.requests.get("storage", "0") if pvc.spec.resources and pvc.spec.resources.requests else "0"
            sc_pvc_capacity[sc_name] = sc_pvc_capacity.get(sc_name, 0) + _parse_capacity(cap)

        result = []
        for sc in sc_result.items:
            name = sc.metadata.name
            is_default = False
            if sc.metadata.annotations:
                is_default = sc.metadata.annotations.get(
                    "storageclass.kubernetes.io/is-default-class"
                ) == "true"

            params = sc.parameters or {}
            provisioned = sc_pv_capacity.get(name, 0)
            pool_name = params.get("storagePool", "")
            repl_count = int(params.get("autoPlace", "1") or "1")

            if pool_name and pool_name in linstor_pools:
                # LINSTOR: real pool capacity (raw), adjusted for replication
                raw_total, raw_free = linstor_pools[pool_name]
                total = raw_total // max(repl_count, 1)
                used = (raw_total - raw_free) // max(repl_count, 1)
            elif name in sc_pool_available:
                # CSIStorageCapacity: total = available + provisioned
                pool_avail = sc_pool_available[name]
                total = pool_avail + provisioned
                used = provisioned
            else:
                # Fallback: best-effort from PV/PVC sums
                total = provisioned
                used = sc_pvc_capacity.get(name, 0)

            result.append(StorageClassDetailResponse(
                name=name,
                provisioner=sc.provisioner,
                reclaim_policy=sc.reclaim_policy,
                volume_binding_mode=sc.volume_binding_mode,
                allow_volume_expansion=sc.allow_volume_expansion or False,
                is_default=is_default,
                parameters=params,
                pv_count=sc_pv_count.get(name, 0),
                pvc_count=sc_pvc_count.get(name, 0),
                total_capacity_bytes=total,
                used_capacity_bytes=used,
                created=sc.metadata.creation_timestamp.isoformat() if sc.metadata.creation_timestamp else None,
            ))

        return result

    except ApiException as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list StorageClass details: {e.reason}",
        )


@router.get("/datavolumes/{name}/usage", response_model=ImageUsageResponse)
async def get_image_usage(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_auth),
) -> ImageUsageResponse:
    """Get image usage - status and which VMs are using this image."""
    k8s_client = request.app.state.k8s_client
    
    try:
        # Get the DataVolume
        dv = await k8s_client.custom_api.get_namespaced_custom_object(
            group=CDI_API_GROUP,
            version=CDI_API_VERSION,
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
        
        metadata = dv.get("metadata", {})
        spec = dv.get("spec", {})
        status_obj = dv.get("status", {})
        annotations = metadata.get("annotations", {})
        
        # Get phase
        phase = status_obj.get("phase", "Unknown")
        
        # Get source info
        source = spec.get("source", {})
        source_type = None
        source_url = None
        if "http" in source:
            source_type = "http"
            source_url = source["http"].get("url")
        elif "registry" in source:
            source_type = "registry"
            source_url = source["registry"].get("url")
        elif "pvc" in source:
            source_type = "clone"
            source_url = f"{source['pvc'].get('namespace', namespace)}/{source['pvc'].get('name')}"
        elif "blank" in source:
            source_type = "blank"
        
        # Get size
        pvc_spec = spec.get("pvc", spec.get("storage", {}))
        size = pvc_spec.get("resources", {}).get("requests", {}).get("storage")
        
        # Get all VMs in namespace to check usage
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        vms = await custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        
        # Find VMs using this image
        using_vms = []
        for vm in vms.get("items", []):
            vm_name = vm["metadata"]["name"]
            
            # Check dataVolumeTemplates
            dv_templates = vm.get("spec", {}).get("dataVolumeTemplates", [])
            for dv_tmpl in dv_templates:
                tmpl_source = dv_tmpl.get("spec", {}).get("source", {})
                if "pvc" in tmpl_source:
                    if tmpl_source["pvc"].get("name") == name:
                        using_vms.append(vm_name)
                        break
            
            # Check volumes
            volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
            for vol in volumes:
                if "dataVolume" in vol and vol["dataVolume"].get("name") == name:
                    if vm_name not in using_vms:
                        using_vms.append(vm_name)
                if "persistentVolumeClaim" in vol and vol["persistentVolumeClaim"].get("claimName") == name:
                    if vm_name not in using_vms:
                        using_vms.append(vm_name)
        
        # Check conditions for errors (CDI keeps phase=Pending during retries
        # but sets Running condition reason=Error)
        has_error_condition = False
        for cond in status_obj.get("conditions", []):
            if cond.get("type") == "Running" and cond.get("status") == "False" and cond.get("reason") in ("Error", "TransferFailed"):
                has_error_condition = True
                break
        
        # Determine status
        if phase in ("Failed", "Error") or has_error_condition:
            image_status = "Error"
        elif phase in ("Pending", "ImportScheduled", "ImportInProgress", "CloneScheduled", "CloneInProgress", "WaitForFirstConsumer"):
            image_status = "Pending"
        elif phase == "Succeeded" and using_vms:
            image_status = "InUse"
        elif phase == "Succeeded":
            image_status = "Ready"
        else:
            image_status = phase
        
        return ImageUsageResponse(
            name=name,
            namespace=namespace,
            display_name=annotations.get("kubevirt-ui.io/display-name"),
            description=annotations.get("kubevirt-ui.io/description"),
            size=size,
            status=image_status,
            source_type=source_type,
            source_url=source_url,
            used_by_vms=using_vms,
            created=metadata.get("creationTimestamp"),
        )
    
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"DataVolume {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get image usage: {e.reason}",
        )
