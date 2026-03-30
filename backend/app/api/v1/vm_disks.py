"""VM disk operations: attach, detach, list, resize, create-image."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth
from app.core.kubevirt import get_hotplug_mode, kubevirt_subresource_call

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request / response models ─────────────────────────────────────────────────

class AttachDiskRequest(BaseModel):
    """Request to attach an existing PVC/DataVolume as a disk to a VM."""
    disk_name: str = Field(..., description="Name for the disk in the VM spec")
    pvc_name: str = Field(..., description="Name of the PVC or DataVolume to attach")
    bus: str = Field("virtio", description="Disk bus type (virtio, scsi, sata)")
    is_cdrom: bool = Field(False, description="Mount as CD-ROM instead of disk")


class DiskDetailResponse(BaseModel):
    """Detailed disk information with size."""
    name: str
    type: str
    source_name: str | None = None
    size: str | None = None
    storage_class: str | None = None
    bus: str = "virtio"
    boot_order: int | None = None
    is_cloudinit: bool = False
    status: str | None = None
    can_resize: bool = False


class DiskResizeRequest(BaseModel):
    """Request to resize a disk."""
    new_size: str = Field(..., pattern=r"^\d+[KMGT]i$")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/{name}/attach-disk", status_code=status.HTTP_200_OK)
async def attach_disk_to_vm(
    request: Request,
    namespace: str,
    name: str,
    attach_request: AttachDiskRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Attach an existing PVC/DataVolume to a VM.
    
    - Running VM: uses KubeVirt hotplug (addvolume subresource API)
    - Stopped VM: patches VM spec directly
    
    For persistent disks (label kubevirt-ui.io/persistent=true):
    - Enforces single-VM constraint (disk can only be attached to one VM)
    - Updates kubevirt-ui.io/attached-to label on the DataVolume
    """
    k8s_client = request.app.state.k8s_client
    
    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        # --- Check persistent disk constraints ---
        is_persistent = False
        dv_obj = None
        
        try:
            dv_obj = await custom_api.get_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural="datavolumes",
                name=attach_request.pvc_name,
            )
            dv_labels = dv_obj.get("metadata", {}).get("labels", {})
            is_persistent = dv_labels.get("kubevirt-ui.io/persistent", "false").lower() == "true"
        except ApiException as e:
            if e.status != 404:
                raise
        
        if is_persistent:
            attached_to = dv_obj.get("metadata", {}).get("labels", {}).get("kubevirt-ui.io/attached-to")
            if attached_to and attached_to != name:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Persistent disk '{attach_request.pvc_name}' is already attached to VM '{attached_to}'. "
                           f"Detach it first before attaching to another VM.",
                )
            
            # Scan VMs to catch cases where label is missing
            vms_result = await custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
            )
            for other_vm in vms_result.get("items", []):
                other_vm_name = other_vm["metadata"]["name"]
                if other_vm_name == name:
                    continue
                other_volumes = other_vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
                for vol in other_volumes:
                    claim = None
                    if "persistentVolumeClaim" in vol:
                        claim = vol["persistentVolumeClaim"].get("claimName")
                    elif "dataVolume" in vol:
                        claim = vol["dataVolume"].get("name")
                    if claim == attach_request.pvc_name:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Persistent disk '{attach_request.pvc_name}' is already attached to VM '{other_vm_name}'. "
                                   f"Detach it first.",
                        )
        
        # Get the VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
        )
        
        # Check for duplicate disk name
        template_spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        existing_disks = template_spec.get("domain", {}).get("devices", {}).get("disks", [])
        if any(d.get("name") == attach_request.disk_name for d in existing_disks):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Disk name '{attach_request.disk_name}' already exists on this VM",
            )
        
        # Determine if VM is running
        run_strategy = vm.get("spec", {}).get("runStrategy")
        is_running = run_strategy in ("Always", "RerunOnFailure")
        
        if not is_running:
            try:
                await custom_api.get_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=name,
                )
                is_running = True
            except ApiException as e:
                if e.status != 404:
                    raise
        
        # Build disk spec
        disk_spec: dict[str, Any] = {"name": attach_request.disk_name}
        if attach_request.is_cdrom:
            disk_spec["cdrom"] = {"bus": attach_request.bus}
        else:
            disk_spec["disk"] = {"bus": attach_request.bus}
        
        hotplug_mode = await get_hotplug_mode(k8s_client)
        hotplug_attempted = False
        hotplug_ok = False
        restart_needed = False
        
        if is_running:
            hotplug_attempted = True
            
            if hotplug_mode == "declarative":
                hotplug_volume = {
                    "name": attach_request.disk_name,
                    "persistentVolumeClaim": {
                        "claimName": attach_request.pvc_name,
                        "hotpluggable": True,
                    },
                }
                hotplug_disk: dict[str, Any] = {"name": attach_request.disk_name}
                if attach_request.is_cdrom:
                    hotplug_disk["cdrom"] = {"bus": "virtio"}
                else:
                    hotplug_disk["disk"] = {"bus": "virtio"}
                
                volumes = list(template_spec.get("volumes", []))
                disks = list(existing_disks)
                volumes.append(hotplug_volume)
                disks.append(hotplug_disk)
                
                patch = {
                    "spec": {
                        "template": {
                            "spec": {
                                "volumes": volumes,
                                "domain": {"devices": {"disks": disks}},
                            }
                        }
                    }
                }
                await custom_api.patch_namespaced_custom_object(
                    group="kubevirt.io", version="v1", namespace=namespace,
                    plural="virtualmachines", name=name, body=patch,
                    _content_type="application/merge-patch+json",
                )
                hotplug_ok = True
                
            elif hotplug_mode == "legacy":
                hotplug_disk_spec: dict[str, Any] = {"name": attach_request.disk_name}
                if attach_request.is_cdrom:
                    hotplug_disk_spec["cdrom"] = {"bus": "scsi"}
                else:
                    hotplug_disk_spec["disk"] = {"bus": "scsi"}
                
                addvolume_body = {
                    "name": attach_request.disk_name,
                    "disk": hotplug_disk_spec,
                    "volumeSource": {
                        "persistentVolumeClaim": {
                            "claimName": attach_request.pvc_name,
                            "hotpluggable": True,
                        },
                    },
                }
                hotplug_ok, _ = await kubevirt_subresource_call(
                    k8s_client, "put", namespace, name, "addvolume", addvolume_body,
                )
                if hotplug_ok:
                    restart_needed = True
        
        if hotplug_ok:
            method = "hotplug-declarative" if hotplug_mode == "declarative" else "hotplug-legacy"
        else:
            # Cold attach: patch VM spec directly
            volume_spec = {
                "name": attach_request.disk_name,
                "persistentVolumeClaim": {"claimName": attach_request.pvc_name},
            }
            volumes = list(template_spec.get("volumes", []))
            disks = list(existing_disks)
            
            volumes.append(volume_spec)
            disks.append(disk_spec)
            
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": volumes,
                            "domain": {"devices": {"disks": disks}},
                        }
                    }
                }
            }
            
            await custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachines", name=name, body=patch,
                _content_type="application/merge-patch+json",
            )
            method = "spec-patch"
        
        # --- Update attached-to label for persistent disks ---
        if is_persistent and dv_obj:
            try:
                patch_labels = {
                    "metadata": {
                        "labels": {
                            "kubevirt-ui.io/attached-to": name,
                        }
                    }
                }
                await custom_api.patch_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                    name=attach_request.pvc_name,
                    body=patch_labels,
                    _content_type="application/merge-patch+json",
                )
            except Exception as label_err:
                logger.warning(f"Failed to update attached-to label on {attach_request.pvc_name}: {label_err}")
        
        restart_required = restart_needed or (hotplug_attempted and not hotplug_ok)
        msg = f"Disk '{attach_request.disk_name}' attached to VM '{name}' ({method})"
        if restart_needed:
            msg += ". Restart VM to apply changes (legacy hotplug uses SCSI)."
        elif hotplug_attempted and not hotplug_ok:
            msg += ". Restart VM to apply changes."
        
        return {
            "status": "ok",
            "message": msg,
            "method": method,
            "persistent": is_persistent,
            "restart_required": restart_required,
        }
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to attach disk to VM: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to attach disk: {e.reason}",
        )


@router.post("/{name}/create-image", status_code=status.HTTP_201_CREATED)
async def create_image_from_vm(
    request: Request,
    namespace: str,
    name: str,
    image_name: str,
    display_name: str | None = None,
    description: str | None = None,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a golden image from VM's root disk (VM must be stopped)."""
    k8s_client = request.app.state.k8s_client
    
    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        # Get the VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
        )
        
        # Check if VM is running
        run_strategy = vm.get("spec", {}).get("runStrategy")
        if run_strategy == "Always":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VM must be stopped before creating an image. Stop the VM first.",
            )
        
        try:
            await custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=name,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VM is still running. Wait for it to stop completely.",
            )
        except ApiException as e:
            if e.status != 404:
                raise
        
        # Find the root disk
        volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
        root_disk_name = None
        
        for vol in volumes:
            if "dataVolume" in vol:
                root_disk_name = vol["dataVolume"]["name"]
                break
            if "persistentVolumeClaim" in vol:
                root_disk_name = vol["persistentVolumeClaim"]["claimName"]
                break
        
        if not root_disk_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VM has no root disk to clone",
            )
        
        # Get source disk size
        source_pvc = await k8s_client.core_api.read_namespaced_persistent_volume_claim(
            name=root_disk_name,
            namespace=namespace,
        )
        size = source_pvc.spec.resources.requests.get("storage", "50Gi")
        storage_class = source_pvc.spec.storage_class_name
        
        # Create golden image from disk
        golden_images_ns = "golden-images"
        
        # Ensure namespace exists
        try:
            await k8s_client.core_api.read_namespace(golden_images_ns)
        except ApiException as e:
            if e.status == 404:
                ns = client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=golden_images_ns,
                        labels={"kubevirt-ui.io/managed": "true"},
                    )
                )
                await k8s_client.core_api.create_namespace(ns)
        
        golden_storage: dict[str, Any] = {
            "volumeMode": "Block",
            "resources": {
                "requests": {
                    "storage": size,
                }
            },
        }
        if storage_class:
            golden_storage["storageClassName"] = storage_class
        
        dv = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": image_name,
                "namespace": golden_images_ns,
                "labels": {
                    "kubevirt-ui.io/golden-image": "true",
                    "kubevirt-ui.io/managed": "true",
                    "kubevirt-ui.io/cloned-from-vm": name,
                    "kubevirt-ui.io/cloned-from-namespace": namespace,
                },
                "annotations": {},
            },
            "spec": {
                "source": {
                    "pvc": {
                        "name": root_disk_name,
                        "namespace": namespace,
                    }
                },
                "storage": golden_storage,
            },
        }
        
        if display_name:
            dv["metadata"]["annotations"]["kubevirt-ui.io/display-name"] = display_name
        if description:
            dv["metadata"]["annotations"]["kubevirt-ui.io/description"] = description
        
        await custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=golden_images_ns,
            plural="datavolumes",
            body=dv,
        )
        
        return {
            "status": "created",
            "name": image_name,
            "namespace": golden_images_ns,
            "source_vm": name,
            "source_disk": root_disk_name,
        }
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to create image from VM: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create image from VM: {e.reason}",
        )


@router.get("/{name}/disks", response_model=list[DiskDetailResponse])
async def get_vm_disks(
    request: Request,
    namespace: str,
    name: str,
) -> list[DiskDetailResponse]:
    """Get detailed disk information for a VM including sizes from PVCs."""
    k8s_client = request.app.state.k8s_client

    try:
        vm = await k8s_client.get_virtual_machine(name=name, namespace=namespace)
        template_spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        domain = template_spec.get("domain", {})
        
        volume_specs = template_spec.get("volumes", [])
        disk_specs = domain.get("devices", {}).get("disks", [])
        disk_spec_map = {d.get("name"): d for d in disk_specs}
        
        core_api = client.CoreV1Api(k8s_client._api_client)
        
        disks: list[DiskDetailResponse] = []
        
        for vol in volume_specs:
            vol_name = vol.get("name", "")
            disk_spec = disk_spec_map.get(vol_name, {})
            
            disk_type = "unknown"
            source_name = None
            is_cloudinit = False
            size = None
            storage_class = None
            pvc_status = None
            can_resize = False
            
            if "dataVolume" in vol:
                disk_type = "dataVolume"
                source_name = vol["dataVolume"].get("name")
                try:
                    pvc = await core_api.read_namespaced_persistent_volume_claim(
                        name=source_name,
                        namespace=namespace,
                    )
                    size = pvc.spec.resources.requests.get("storage")
                    storage_class = pvc.spec.storage_class_name
                    pvc_status = pvc.status.phase
                    can_resize = True
                except ApiException:
                    pass
                    
            elif "persistentVolumeClaim" in vol:
                disk_type = "persistentVolumeClaim"
                source_name = vol["persistentVolumeClaim"].get("claimName")
                try:
                    pvc = await core_api.read_namespaced_persistent_volume_claim(
                        name=source_name,
                        namespace=namespace,
                    )
                    size = pvc.spec.resources.requests.get("storage")
                    storage_class = pvc.spec.storage_class_name
                    pvc_status = pvc.status.phase
                    can_resize = True
                except ApiException:
                    pass
                    
            elif "cloudInitNoCloud" in vol or "cloudInitConfigDrive" in vol:
                disk_type = "cloudInit"
                is_cloudinit = True
                
            elif "containerDisk" in vol:
                disk_type = "containerDisk"
                source_name = vol["containerDisk"].get("image")
            
            bus = "virtio"
            if "disk" in disk_spec:
                bus = disk_spec["disk"].get("bus", "virtio")
            elif "cdrom" in disk_spec:
                bus = "sata"
            
            boot_order = disk_spec.get("bootOrder")
            
            disks.append(DiskDetailResponse(
                name=vol_name,
                type=disk_type,
                source_name=source_name,
                size=size,
                storage_class=storage_class,
                bus=bus,
                boot_order=boot_order,
                is_cloudinit=is_cloudinit,
                status=pvc_status,
                can_resize=can_resize,
            ))
        
        return disks

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get VM disks: {e.reason}",
        )


@router.put("/{name}/disks/{disk_name}/resize")
async def resize_vm_disk(
    request: Request,
    namespace: str,
    name: str,
    disk_name: str,
    resize_request: DiskResizeRequest,
    user: User = Depends(require_auth),
) -> DiskDetailResponse:
    """Resize a VM disk (PVC). Supports online resize if the storage class allows it."""
    k8s_client = request.app.state.k8s_client

    try:
        vm = await k8s_client.get_virtual_machine(name=name, namespace=namespace)
        template_spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        volume_specs = template_spec.get("volumes", [])
        
        pvc_name = None
        for vol in volume_specs:
            if vol.get("name") == disk_name:
                if "dataVolume" in vol:
                    pvc_name = vol["dataVolume"].get("name")
                elif "persistentVolumeClaim" in vol:
                    pvc_name = vol["persistentVolumeClaim"].get("claimName")
                break
        
        if not pvc_name:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Disk {disk_name} not found or cannot be resized",
            )
        
        core_api = client.CoreV1Api(k8s_client._api_client)
        
        pvc = await core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace,
        )
        
        current_size = pvc.spec.resources.requests.get("storage", "0")
        
        patch = {
            "spec": {
                "resources": {
                    "requests": {
                        "storage": resize_request.new_size
                    }
                }
            }
        }
        
        await core_api.patch_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace,
            body=patch,
        )
        
        pvc = await core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace,
        )
        
        logger.info(f"User {user.username} resized disk {disk_name} ({pvc_name}) from {current_size} to {resize_request.new_size}")
        
        return DiskDetailResponse(
            name=disk_name,
            type="dataVolume" if "dataVolume" in str(volume_specs) else "persistentVolumeClaim",
            source_name=pvc_name,
            size=pvc.spec.resources.requests.get("storage"),
            storage_class=pvc.spec.storage_class_name,
            status=pvc.status.phase,
            can_resize=True,
        )

    except HTTPException:
        raise
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM or disk not found",
            )
        logger.error(f"Failed to resize disk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resize disk: {e.reason}",
        )
