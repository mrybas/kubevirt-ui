"""Persistent Disks API endpoints."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from app.core.operator import (
    OPERATOR_GROUP,
    OPERATOR_VERSION,
    managed_owner,
    operation_name,
    patch_managed_disks,
)
from app.core.auth import User, require_auth, require_env_member, require_env_viewer
from app.core.kubevirt import get_hotplug_mode, kubevirt_subresource_call
from app.core.naming import (
    get_display_name,
    with_synthetic_metadata,
)
from app.core.storage_headroom import assert_storage_headroom
from app.models.template import (
    PersistentDisk,
    PersistentDiskCreate,
    PersistentDiskListResponse,
    AttachDiskRequest,
)

router = APIRouter()
snapshots_router = APIRouter()
logger = logging.getLogger(__name__)

# Labels
PERSISTENT_DISK_LABEL = "kubevirt-ui.io/persistent"
MANAGED_LABEL = "kubevirt-ui.io/managed"
ATTACHED_TO_LABEL = "kubevirt-ui.io/attached-to"


@router.get("", response_model=PersistentDiskListResponse)
async def list_persistent_disks(
    namespace: str,
    request: Request,
    user: User = Depends(require_env_viewer()),
) -> PersistentDiskListResponse:
    """List all persistent disks in a namespace."""
    k8s_client = request.app.state.k8s_client
    
    try:
        # List DataVolumes with persistent label
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        result = await custom_api.list_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            label_selector=f"{PERSISTENT_DISK_LABEL}=true",
        )
        
        # Also get VMs to check attachment status
        vms_result = await custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        
        # Build VM -> disks mapping
        vm_disks: dict[str, list[str]] = {}
        for vm in vms_result.get("items", []):
            vm_name = vm["metadata"]["name"]
            volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
            for vol in volumes:
                if "dataVolume" in vol:
                    dv_name = vol["dataVolume"]["name"]
                    if dv_name not in vm_disks:
                        vm_disks[dv_name] = []
                    vm_disks[dv_name].append(vm_name)
                elif "persistentVolumeClaim" in vol:
                    pvc_name = vol["persistentVolumeClaim"]["claimName"]
                    if pvc_name not in vm_disks:
                        vm_disks[pvc_name] = []
                    vm_disks[pvc_name].append(vm_name)
        
        disks = []
        for dv in result.get("items", []):
            metadata = dv.get("metadata", {})
            spec = dv.get("spec", {})
            status_obj = dv.get("status", {})
            labels = metadata.get("labels", {})
            
            dv_name = metadata.get("name")
            
            # Get size from PVC spec
            pvc_spec = spec.get("pvc", spec.get("storage", {}))
            size = pvc_spec.get("resources", {}).get("requests", {}).get("storage", "Unknown")
            storage_class = pvc_spec.get("storageClassName")
            
            # Check if attached
            attached_to = labels.get(ATTACHED_TO_LABEL)
            if not attached_to and dv_name in vm_disks:
                attached_to = vm_disks[dv_name][0] if vm_disks[dv_name] else None
            
            disks.append(PersistentDisk(
                name=dv_name,
                display_name=get_display_name(metadata) or dv_name,
                namespace=metadata.get("namespace"),
                size=size,
                storage_class=storage_class,
                status=status_obj.get("phase", "Unknown"),
                attached_to=attached_to,
                created=metadata.get("creationTimestamp"),
            ))
        
        return PersistentDiskListResponse(items=disks, total=len(disks))
    
    except ApiException as e:
        logger.error(f"Failed to list persistent disks: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list persistent disks: {e.reason}",
        )


@router.post("", response_model=PersistentDisk, status_code=status.HTTP_201_CREATED)
async def create_persistent_disk(
    namespace: str,
    disk: PersistentDiskCreate,
    request: Request,
    user: User = Depends(require_env_member()),
) -> PersistentDisk:
    """Create a new persistent disk."""
    k8s_client = request.app.state.k8s_client

    # Before the DataVolume, because the quota does not count DataVolumes.
    # CDI turns one into a PVC a moment later and the API server refuses
    # *that*, long after this endpoint has answered 201 and the dialog has
    # closed the way it closes on success.
    await assert_storage_headroom(
        k8s_client, namespace, disk.size,
        what=f"the disk {disk.display_name!r}",
    )

    try:
        # Determine source
        if disk.source_image:
            # Clone from image in the same namespace
            source = {
                "pvc": {
                    "name": disk.source_image,
                    "namespace": namespace,
                }
            }
        else:
            source = {"blank": {}}
        
        # Build DataVolume. The K8s name is assigned server-side via
        # `metadata.generateName` (collision-free); display_name lives in an
        # annotation. See app/core/naming.py.
        dv = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "labels": {
                    PERSISTENT_DISK_LABEL: "true",
                    MANAGED_LABEL: "true",
                },
                "annotations": {
                    "cdi.kubevirt.io/storage.deleteAfterCompletion": "false",
                },
            },
            "spec": {
                "source": source,
                "pvc": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {
                        "requests": {
                            "storage": disk.size,
                        }
                    },
                    "volumeMode": "Block",
                },
            },
        }

        if disk.storage_class:
            dv["spec"]["pvc"]["storageClassName"] = disk.storage_class

        # Inject generateName + display-name annotation + slug label.
        with_synthetic_metadata(dv, disk.display_name, namespace=namespace)

        # Create DataVolume
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            body=dv,
        )

        result_md = result.get("metadata", {})
        return PersistentDisk(
            name=result_md["name"],
            display_name=get_display_name(result_md) or result_md["name"],
            namespace=result_md["namespace"],
            size=disk.size,
            storage_class=disk.storage_class,
            status=result.get("status", {}).get("phase", "Pending"),
            attached_to=None,
            created=result_md.get("creationTimestamp"),
        )
    
    except ApiException as e:
        logger.error(f"Failed to create persistent disk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create persistent disk: {e.reason}",
        )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persistent_disk(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_member()),
) -> None:
    """Delete a persistent disk."""
    k8s_client = request.app.state.k8s_client
    
    try:
        # First check if disk is attached to any VM
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        vms_result = await custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        
        for vm in vms_result.get("items", []):
            volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
            for vol in volumes:
                if "dataVolume" in vol and vol["dataVolume"]["name"] == name:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Disk {name} is attached to VM {vm['metadata']['name']}. Detach it first.",
                    )
                if "persistentVolumeClaim" in vol and vol["persistentVolumeClaim"]["claimName"] == name:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Disk {name} is attached to VM {vm['metadata']['name']}. Detach it first.",
                    )
        
        # Delete DataVolume
        await custom_api.delete_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
    
    except HTTPException:
        raise
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Disk {name} not found",
            )
        logger.error(f"Failed to delete persistent disk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete persistent disk: {e.reason}",
        )


@router.post("/{name}/attach", status_code=status.HTTP_200_OK)
async def attach_disk_to_vm(
    namespace: str,
    name: str,
    attach_request: AttachDiskRequest,
    request: Request,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Attach a persistent disk to a VM.
    
    Enforces single-VM constraint: a persistent disk can only be attached to one VM.
    Supports hotplug for running VMs.
    """
    k8s_client = request.app.state.k8s_client
    
    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        # Get the disk to verify it exists and check attached-to label
        dv = await custom_api.get_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
        
        # Check attached-to label first (fast path)
        dv_labels = dv.get("metadata", {}).get("labels", {})
        attached_to = dv_labels.get(ATTACHED_TO_LABEL)
        if attached_to and attached_to != attach_request.vm_name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Disk '{name}' is already attached to VM '{attached_to}'. Detach it first.",
            )
        
        # Scan all VMs to enforce single-VM constraint (catches missing labels)
        vms_result = await custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        for other_vm in vms_result.get("items", []):
            other_vm_name = other_vm["metadata"]["name"]
            if other_vm_name == attach_request.vm_name:
                continue
            other_volumes = other_vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
            for vol in other_volumes:
                claim = None
                if "persistentVolumeClaim" in vol:
                    claim = vol["persistentVolumeClaim"].get("claimName")
                elif "dataVolume" in vol:
                    claim = vol["dataVolume"].get("name")
                if claim == name:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Disk '{name}' is already attached to VM '{other_vm_name}'. Detach it first.",
                    )
        
        # Get the target VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=attach_request.vm_name,
        )
        
        # Check if disk already attached to this VM
        volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
        for vol in volumes:
            claim = None
            if "persistentVolumeClaim" in vol:
                claim = vol["persistentVolumeClaim"].get("claimName")
            elif "dataVolume" in vol:
                claim = vol["dataVolume"].get("name")
            if claim == name:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Disk '{name}' is already attached to VM '{attach_request.vm_name}'",
                )
        
        # Build volume and disk specs
        disk_vol_name = f"disk-{name}"
        volume_spec = {
            "name": disk_vol_name,
            "persistentVolumeClaim": {"claimName": name},
        }
        disk_spec = {
            "name": disk_vol_name,
            "disk": {"bus": "virtio"},
        }
        
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
                    name=attach_request.vm_name,
                )
                is_running = True
            except ApiException as e:
                if e.status != 404:
                    raise
        
        hotplug_mode = await get_hotplug_mode(k8s_client)
        hotplug_ok = False
        hotplug_attempted = False
        restart_needed = False
        
        if is_running and attach_request.hotplug:
            hotplug_attempted = True
            
            if hotplug_mode == "declarative":
                # DeclarativeHotplugVolumes: patch VM spec with hotpluggable + virtio
                # KubeVirt auto-hotplugs new volumes to the running VMI
                hotplug_volume = {
                    "name": disk_vol_name,
                    "persistentVolumeClaim": {
                        "claimName": name,
                        "hotpluggable": True,
                    },
                }
                hotplug_disk = {
                    "name": disk_vol_name,
                    "disk": {"bus": "virtio"},
                }
                volumes.append(hotplug_volume)
                disks = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("devices", {}).get("disks", [])
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
                    plural="virtualmachines", name=attach_request.vm_name, body=patch,
                    _content_type="application/merge-patch+json",
                )
                hotplug_ok = True
                
            elif hotplug_mode == "legacy":
                # HotplugVolumes: addvolume subresource (scsi bus, reboot needed)
                hotplug_disk_spec = {
                    "name": disk_vol_name,
                    "disk": {"bus": "scsi"},
                }
                addvolume_body = {
                    "name": disk_vol_name,
                    "disk": hotplug_disk_spec,
                    "volumeSource": {
                        "persistentVolumeClaim": {
                            "claimName": name,
                            "hotpluggable": True,
                        },
                    },
                }
                hotplug_ok, _ = await kubevirt_subresource_call(
                    k8s_client, "put", namespace, attach_request.vm_name, "addvolume", addvolume_body,
                )
                if hotplug_ok:
                    restart_needed = True
        
        if hotplug_ok:
            method = "hotplug-declarative" if hotplug_mode == "declarative" else "hotplug-legacy"
        else:
            # Cold attach: patch VM spec (no hotpluggable flag)
            volumes.append(volume_spec)
            disks = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("devices", {}).get("disks", [])
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
                plural="virtualmachines", name=attach_request.vm_name, body=patch,
                _content_type="application/merge-patch+json",
            )
            method = "spec-patch"
        
        # Update attached-to label on the DataVolume
        patch_labels = {
            "metadata": {
                "labels": {
                    ATTACHED_TO_LABEL: attach_request.vm_name,
                }
            }
        }
        await custom_api.patch_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
            body=patch_labels,
            _content_type="application/merge-patch+json",
        )
        
        restart_required = restart_needed or (hotplug_attempted and not hotplug_ok)
        msg = f"Disk '{name}' attached to VM '{attach_request.vm_name}' ({method})"
        if restart_needed:
            msg += ". Restart VM to apply changes (legacy hotplug uses SCSI)."
        elif hotplug_attempted and not hotplug_ok:
            msg += ". Restart VM to apply changes."
        
        return {
            "status": "attached",
            "disk": name,
            "vm": attach_request.vm_name,
            "method": method,
            "restart_required": restart_required,
            "message": msg,
        }
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to attach disk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to attach disk: {e.reason}",
        )


@router.post("/{name}/detach", status_code=status.HTTP_200_OK)
async def detach_disk_from_vm(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Detach a persistent disk from a VM."""
    k8s_client = request.app.state.k8s_client
    
    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        # Get the disk to find which VM it's attached to
        dv = await custom_api.get_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
        
        attached_to = dv.get("metadata", {}).get("labels", {}).get(ATTACHED_TO_LABEL)
        
        if not attached_to:
            # Search in VMs
            vms_result = await custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
            )
            
            for vm in vms_result.get("items", []):
                volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
                for vol in volumes:
                    if "persistentVolumeClaim" in vol and vol["persistentVolumeClaim"]["claimName"] == name:
                        attached_to = vm["metadata"]["name"]
                        break
                    if "dataVolume" in vol and vol["dataVolume"]["name"] == name:
                        attached_to = vm["metadata"]["name"]
                        break
                if attached_to:
                    break
        
        if attached_to:
            # A machine the operator owns keeps its attachments in its own spec.
            # Editing the VirtualMachine here would be reverted on the next pass,
            # so the button would appear to work and then undo itself.
            try:
                holder = await custom_api.get_namespaced_custom_object(
                    group="kubevirt.io", version="v1",
                    namespace=namespace, plural="virtualmachines", name=attached_to,
                )
                owner = managed_owner(holder, "ManagedVM")
            except ApiException as e:
                if e.status != 404:
                    raise
                owner = None

            if owner:
                await patch_managed_disks(
                    custom_api, namespace, owner, name, attach=False,
                )
                return {
                    "disk": name,
                    "vm": attached_to,
                    "detached": True,
                    "message": f"Disk {name} detached from {attached_to}",
                }

        if not attached_to:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Disk {name} is not attached to any VM",
            )
        
        # Get the VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=attached_to,
        )
        
        # Remove volume and disk from VM spec
        volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
        disks = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("devices", {}).get("disks", [])
        
        # Find the volume name used for this disk
        disk_vol_name = None
        for vol in volumes:
            if "persistentVolumeClaim" in vol and vol["persistentVolumeClaim"]["claimName"] == name:
                disk_vol_name = vol["name"]
                break
            elif "dataVolume" in vol and vol["dataVolume"]["name"] == name:
                disk_vol_name = vol["name"]
                break
        
        if not disk_vol_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Disk {name} not found in VM {attached_to}",
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
                    name=attached_to,
                )
                is_running = True
            except ApiException as e:
                if e.status != 404:
                    raise
        
        hotplug_mode = await get_hotplug_mode(k8s_client)
        hotplug_ok = False
        restart_needed = False
        
        if is_running:
            if hotplug_mode == "declarative":
                # DeclarativeHotplugVolumes: remove from VM spec
                # KubeVirt auto-unplugs from the running VMI
                new_volumes = [v for v in volumes if v.get("name") != disk_vol_name]
                new_disks = [d for d in disks if d["name"] != disk_vol_name]
                
                patch = {
                    "spec": {
                        "template": {
                            "spec": {
                                "volumes": new_volumes,
                                "domain": {"devices": {"disks": new_disks}},
                            }
                        }
                    }
                }
                await custom_api.patch_namespaced_custom_object(
                    group="kubevirt.io", version="v1", namespace=namespace,
                    plural="virtualmachines", name=attached_to, body=patch,
                    _content_type="application/merge-patch+json",
                )
                hotplug_ok = True
                
            elif hotplug_mode == "legacy":
                # HotplugVolumes: removevolume subresource
                removevolume_body = {"name": disk_vol_name}
                hotplug_ok, _ = await kubevirt_subresource_call(
                    k8s_client, "put", namespace, attached_to, "removevolume", removevolume_body,
                )
                if hotplug_ok:
                    restart_needed = True
        
        if hotplug_ok:
            method = "hotplug-declarative" if hotplug_mode == "declarative" else "hotplug-legacy"
        else:
            # Cold detach: update VM spec
            new_volumes = [v for v in volumes if v.get("name") != disk_vol_name]
            new_disks = [d for d in disks if d["name"] != disk_vol_name]
            
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": new_volumes,
                            "domain": {"devices": {"disks": new_disks}},
                        }
                    }
                }
            }
            await custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachines", name=attached_to, body=patch,
                _content_type="application/merge-patch+json",
            )
            method = "spec-patch" if not is_running else "spec-patch-restart-required"
        
        # Remove attached-to label from disk (best-effort, DV may not exist)
        try:
            patch_labels = {
                "metadata": {
                    "labels": {
                        ATTACHED_TO_LABEL: None,
                    }
                }
            }
            await custom_api.patch_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural="datavolumes",
                name=name,
                body=patch_labels,
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to remove label from DV {name}: {e.reason}")
        
        return {"status": "detached", "disk": name, "vm": attached_to, "method": method}
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to detach disk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to detach disk: {e.reason}",
        )


# ==================== Save disk as image ====================

@router.post("/{name}/save-as-image", status_code=status.HTTP_201_CREATED)
async def save_disk_as_image(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Clone a disk's PVC as a new image DataVolume for reuse as VM template.

    Image's K8s name is assigned server-side via ``generateName``; the
    human-friendly ``display_name`` is stored in an annotation.
    """
    k8s_client = request.app.state.k8s_client
    body = await request.json()
    display_name = body.get("display_name")
    # Back-compat: older clients sent `image_name` (used as both K8s name and
    # display name); accept it as the display name when display_name is absent.
    if not display_name:
        display_name = body.get("image_name")

    if not display_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="display_name is required",
        )

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        core_api = client.CoreV1Api(k8s_client._api_client)

        # Get source PVC info
        source_pvc = await core_api.read_namespaced_persistent_volume_claim(
            name=name, namespace=namespace,
        )
        storage_class = source_pvc.spec.storage_class_name or ""
        volume_mode = source_pvc.spec.volume_mode or "Block"
        access_modes = source_pvc.spec.access_modes or ["ReadWriteMany"]
        capacity = source_pvc.status.capacity.get("storage", "10Gi") if source_pvc.status.capacity else "10Gi"

        # Create DataVolume that clones the source PVC. Use generateName for
        # collision-free K8s naming.
        dv_body = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "labels": {
                    MANAGED_LABEL: "true",
                    PERSISTENT_DISK_LABEL: "false",
                    "kubevirt-ui.io/disk-type": "image",
                    "kubevirt-ui.io/os-type": "linux",
                },
                "annotations": {
                    "kubevirt-ui.io/cloned-from": name,
                    "cdi.kubevirt.io/storage.usePopulator": "true",
                },
            },
            "spec": {
                "source": {
                    "pvc": {
                        "namespace": namespace,
                        "name": name,
                    },
                },
                "storage": {
                    "accessModes": access_modes,
                    "storageClassName": storage_class,
                    "volumeMode": volume_mode,
                    "resources": {
                        "requests": {
                            "storage": capacity,
                        },
                    },
                },
            },
        }

        # Inject generateName + display-name annotation + slug label.
        with_synthetic_metadata(dv_body, display_name, namespace=namespace)

        result = await custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            body=dv_body,
        )

        result_md = result.get("metadata", {})
        return {
            "status": "cloning",
            "name": result_md.get("name"),
            "display_name": get_display_name(result_md) or display_name,
            # Back-compat key for older clients.
            "image_name": result_md.get("name"),
            "source_pvc": name,
            "size": capacity,
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to save disk as image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save disk as image: {e.reason}",
        )


# ==================== VolumeSnapshot endpoints ====================

@router.get("/{name}/snapshots", status_code=status.HTTP_200_OK)
async def list_disk_snapshots(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_viewer()),
) -> list[dict[str, Any]]:
    """List VolumeSnapshots for a specific PVC."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        snapshots = await custom_api.list_namespaced_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            namespace=namespace,
            plural="volumesnapshots",
        )

        result = []
        for snap in snapshots.get("items", []):
            source = snap.get("spec", {}).get("source", {})
            source_pvc = source.get("persistentVolumeClaimName", "")
            if source_pvc != name:
                continue

            snap_metadata = snap.get("metadata", {})
            snap_status = snap.get("status", {})
            restore_size = snap_status.get("restoreSize", "")
            ready = snap_status.get("readyToUse", False)
            creation = snap_metadata.get("creationTimestamp", "")
            snap_class = snap.get("spec", {}).get("volumeSnapshotClassName", "")
            snap_name = snap_metadata.get("name", "")

            result.append({
                "name": snap_name,
                "display_name": get_display_name(snap_metadata) or snap_name,
                "namespace": namespace,
                "pvc_name": source_pvc,
                "storage_class": snap_class,
                "size": restore_size,
                "ready": ready,
                "creation_time": creation,
                "snapshot_class": snap_class,
            })

        # Sort by creation time descending
        result.sort(key=lambda x: x["creation_time"], reverse=True)
        return result

    except ApiException as e:
        if e.status == 404:
            return []
        logger.error(f"Failed to list snapshots: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list snapshots: {e.reason}",
        )


@router.post("/{name}/snapshots", status_code=status.HTTP_201_CREATED)
async def create_disk_snapshot(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Create a VolumeSnapshot from a PVC.

    K8s name is assigned server-side via ``generateName``; display_name lives
    in the ``kubevirt-ui.io/display-name`` annotation.
    """
    k8s_client = request.app.state.k8s_client
    body = await request.json()
    display_name = body.get("display_name")
    # Back-compat: older clients sent `snapshot_name` (used as both K8s name
    # and display name); accept it as display_name when missing.
    if not display_name:
        display_name = body.get("snapshot_name")
    snapshot_class = body.get("snapshot_class", "")

    if not display_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="display_name is required",
        )

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # If no snapshot class provided, try to detect one
        if not snapshot_class:
            try:
                sc_list = await custom_api.list_cluster_custom_object(
                    group="snapshot.storage.k8s.io",
                    version="v1",
                    plural="volumesnapshotclasses",
                )
                classes = sc_list.get("items", [])
                if classes:
                    snapshot_class = classes[0]["metadata"]["name"]
            except ApiException:
                pass

        snapshot_body = {
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {
                "labels": {
                    "kubevirt-ui.io/managed": "true",
                    "kubevirt-ui.io/source-pvc": name,
                },
            },
            "spec": {
                "source": {
                    "persistentVolumeClaimName": name,
                },
            },
        }

        if snapshot_class:
            snapshot_body["spec"]["volumeSnapshotClassName"] = snapshot_class

        # Inject generateName + display-name annotation + slug label.
        with_synthetic_metadata(snapshot_body, display_name, namespace=namespace)

        result = await custom_api.create_namespaced_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            namespace=namespace,
            plural="volumesnapshots",
            body=snapshot_body,
        )

        result_md = result.get("metadata", {})
        return {
            "name": result_md["name"],
            "display_name": get_display_name(result_md) or display_name,
            "namespace": namespace,
            "pvc_name": name,
            "storage_class": snapshot_class,
            "size": "",
            "ready": False,
            "creation_time": result_md.get("creationTimestamp", ""),
            "snapshot_class": snapshot_class,
        }

    except ApiException as e:
        logger.error(f"Failed to create snapshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create snapshot: {e.reason}",
        )


# ==================== Snapshots router (mounted at /namespaces/{ns}/snapshots) ====================

@snapshots_router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snapshot(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_member()),
) -> None:
    """Delete a VolumeSnapshot."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        await custom_api.delete_namespaced_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            namespace=namespace,
            plural="volumesnapshots",
            name=name,
        )

    except ApiException as e:
        if e.status == 404:
            return
        logger.error(f"Failed to delete snapshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete snapshot: {e.reason}",
        )


async def _managed_owner_holding_snapshot_source(
    custom_api: Any, namespace: str, snapshot_name: str,
) -> str | None:
    """The ManagedVM that has the snapshot's source disk attached, if any."""
    try:
        snap = await custom_api.get_namespaced_custom_object(
            group="snapshot.storage.k8s.io", version="v1",
            namespace=namespace, plural="volumesnapshots", name=snapshot_name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    source = (
        ((snap.get("spec", {}) or {}).get("source", {}) or {})
        .get("persistentVolumeClaimName", "")
    )
    if not source:
        return None

    try:
        vms = await custom_api.list_namespaced_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            namespace=namespace, plural="managedvms",
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    for vm in vms.get("items", []):
        for disk in ((vm.get("spec", {}) or {}).get("disks") or []):
            if disk.get("claim") == source:
                return vm["metadata"]["name"]
    return None


@snapshots_router.post("/{name}/rollback", status_code=status.HTTP_200_OK)
async def rollback_snapshot(
    namespace: str,
    name: str,
    request: Request,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Roll a disk back to a snapshot.

    For a disk attached to a machine the operator owns this asks for an
    operation, which builds the replacement first, points the machine at it, and
    only then removes what it replaced.

    The path below is what remains for everything else, and it is why this
    endpoint is being replaced: it deletes the claim and creates its successor
    afterwards, so a process that dies in between leaves a machine with no disk
    and nothing anywhere that knows what it had.
    """
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        core_api = client.CoreV1Api(k8s_client._api_client)

        owner = await _managed_owner_holding_snapshot_source(custom_api, namespace, name)
        if owner:
            op_name = operation_name("RollbackDisk", owner)
            await custom_api.create_namespaced_custom_object(
                group=OPERATOR_GROUP, version=OPERATOR_VERSION,
                namespace=namespace, plural="managedvmoperations",
                body={
                    "apiVersion": f"{OPERATOR_GROUP}/{OPERATOR_VERSION}",
                    "kind": "ManagedVMOperation",
                    "metadata": {"name": op_name, "namespace": namespace},
                    "spec": {
                        "vmName": owner,
                        "action": "RollbackDisk",
                        "rollbackDisk": {"snapshotName": name},
                    },
                },
            )
            return {
                "status": "rolling_back",
                "snapshot": name,
                "vm": owner,
                "operation": op_name,
                "message": f"Rollback requested ({op_name})",
            }

        # 1. Get snapshot info
        snap = await custom_api.get_namespaced_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            namespace=namespace,
            plural="volumesnapshots",
            name=name,
        )
        source_pvc_name = snap.get("spec", {}).get("source", {}).get("persistentVolumeClaimName", "")
        restore_size = snap.get("status", {}).get("restoreSize", "10Gi")
        ready = snap.get("status", {}).get("readyToUse", False)

        if not ready:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Snapshot is not ready yet")
        if not source_pvc_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot determine source PVC from snapshot")

        # 2. Get original PVC info (access modes, storage class, volume mode, labels)
        original_pvc = await core_api.read_namespaced_persistent_volume_claim(
            name=source_pvc_name, namespace=namespace
        )
        access_modes = original_pvc.spec.access_modes or ["ReadWriteMany"]
        storage_class = original_pvc.spec.storage_class_name or ""
        volume_mode = original_pvc.spec.volume_mode or "Block"
        original_labels = dict(original_pvc.metadata.labels or {})
        original_annotations = dict(original_pvc.metadata.annotations or {})
        # Remove system annotations
        for k in list(original_annotations.keys()):
            if k.startswith("pv.kubernetes.io/") or k.startswith("volume.") or k.startswith("cdi.kubevirt.io/"):
                del original_annotations[k]

        # 3. Find VM that uses this PVC and stop it
        vm_name = None
        original_run_strategy = "Always"
        vms_result = await custom_api.list_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace, plural="virtualmachines",
        )
        for vm in vms_result.get("items", []):
            volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
            for vol in volumes:
                if "persistentVolumeClaim" in vol and vol["persistentVolumeClaim"]["claimName"] == source_pvc_name:
                    vm_name = vm["metadata"]["name"]
                    original_run_strategy = vm.get("spec", {}).get("runStrategy", "Always")
                    break
                if "dataVolume" in vol and vol["dataVolume"]["name"] == source_pvc_name:
                    vm_name = vm["metadata"]["name"]
                    original_run_strategy = vm.get("spec", {}).get("runStrategy", "Always")
                    break
            if vm_name:
                break

        was_running = False
        if vm_name:
            # Check if VM is running
            try:
                await custom_api.get_namespaced_custom_object(
                    group="kubevirt.io", version="v1", namespace=namespace,
                    plural="virtualmachineinstances", name=vm_name,
                )
                was_running = True
            except ApiException as e:
                if e.status != 404:
                    raise

            if was_running:
                # Stop VM
                await custom_api.patch_namespaced_custom_object(
                    group="kubevirt.io", version="v1", namespace=namespace,
                    plural="virtualmachines", name=vm_name,
                    body={"spec": {"runStrategy": "Halted"}},
                    _content_type="application/merge-patch+json",
                )
                # Wait for VMI to disappear
                for _ in range(60):
                    try:
                        await custom_api.get_namespaced_custom_object(
                            group="kubevirt.io", version="v1", namespace=namespace,
                            plural="virtualmachineinstances", name=vm_name,
                        )
                        await asyncio.sleep(2)
                    except ApiException as e:
                        if e.status == 404:
                            break
                        raise

        # 4. Delete original PVC (and DataVolume if exists)
        try:
            await custom_api.delete_namespaced_custom_object(
                group="cdi.kubevirt.io", version="v1beta1", namespace=namespace,
                plural="datavolumes", name=source_pvc_name,
            )
            # Wait for DV deletion
            for _ in range(30):
                try:
                    await custom_api.get_namespaced_custom_object(
                        group="cdi.kubevirt.io", version="v1beta1", namespace=namespace,
                        plural="datavolumes", name=source_pvc_name,
                    )
                    await asyncio.sleep(1)
                except ApiException:
                    break
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete DV {source_pvc_name}: {e.reason}")

        try:
            await core_api.delete_namespaced_persistent_volume_claim(
                name=source_pvc_name, namespace=namespace,
            )
            # Wait for PVC deletion
            for _ in range(30):
                try:
                    await core_api.read_namespaced_persistent_volume_claim(
                        name=source_pvc_name, namespace=namespace,
                    )
                    await asyncio.sleep(1)
                except ApiException:
                    break
        except ApiException as e:
            if e.status != 404:
                raise

        # 5. Create new PVC from snapshot with the same name
        pvc_body = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": source_pvc_name,
                "namespace": namespace,
                "labels": original_labels,
                "annotations": original_annotations,
            },
            "spec": {
                "accessModes": access_modes,
                "storageClassName": storage_class,
                "volumeMode": volume_mode,
                "resources": {
                    "requests": {
                        "storage": restore_size,
                    },
                },
                "dataSource": {
                    "name": name,
                    "kind": "VolumeSnapshot",
                    "apiGroup": "snapshot.storage.k8s.io",
                },
            },
        }

        await core_api.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=pvc_body,
        )

        # 6. Start VM back if it was running
        if vm_name and was_running:
            await custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachines", name=vm_name,
                body={"spec": {"runStrategy": original_run_strategy}},
                _content_type="application/merge-patch+json",
            )
            # Wait a moment for VM to start scheduling
            await asyncio.sleep(2)

        return {
            "status": "rolled_back",
            "snapshot": name,
            "pvc": source_pvc_name,
            "vm": vm_name,
            "was_running": was_running,
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to rollback snapshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rollback snapshot: {e.reason}",
        )
