"""VM lifecycle actions: start, stop, restart, migrate, recreate, clone, resize."""

import asyncio
import copy
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth
from app.core.kubevirt import kubevirt_subresource_call
from app.models.vm import VMStatusResponse

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request models ────────────────────────────────────────────────────────────

class StopVMRequest(BaseModel):
    """Request model for stopping a VM."""
    force: bool = Field(False, description="Force stop (immediate, no graceful shutdown)")
    grace_period: int = Field(120, ge=0, le=600, description="Grace period in seconds (default: 120)")


class MigrateVMRequest(BaseModel):
    """Request model for live migrating a VM."""
    target_node: str = Field(..., description="Target node to migrate the VM to")


class CloneVMRequest(BaseModel):
    new_name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63,
                          description="Name for the cloned VM")
    target_namespace: str | None = Field(None, description="Target namespace (defaults to same)")
    start: bool = Field(False, description="Start the cloned VM immediately")


class ResizeVMRequest(BaseModel):
    cpu_cores: int | None = Field(None, ge=1, le=256, description="Number of CPU cores")
    cpu_sockets: int | None = Field(None, ge=1, le=16, description="Number of CPU sockets")
    memory: str | None = Field(None, pattern=r"^\d+[MGT]i$", description="Memory (e.g. '4Gi')")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/{name}/start", response_model=VMStatusResponse)
async def start_vm(request: Request, namespace: str, name: str) -> VMStatusResponse:
    """Start a VirtualMachine."""
    k8s_client = request.app.state.k8s_client

    try:
        patch = {"spec": {"runStrategy": "Always"}}
        await k8s_client.patch_virtual_machine(
            name=name, namespace=namespace, body=patch
        )
        return VMStatusResponse(name=name, namespace=namespace, action="start", success=True)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start VM: {e.reason}",
        )


@router.post("/{name}/stop", response_model=VMStatusResponse)
async def stop_vm(
    request: Request, 
    namespace: str, 
    name: str,
    stop_request: StopVMRequest = StopVMRequest(),
) -> VMStatusResponse:
    """Stop a VirtualMachine using subresource API.
    
    - Graceful stop: sends ACPI shutdown signal, waits for grace_period
    - Force stop: immediately terminates the VM (like pulling the power cord)
    """
    k8s_client = request.app.state.k8s_client

    try:
        body: dict[str, Any] = {}
        if stop_request.force:
            body["gracePeriod"] = 0
        else:
            body["gracePeriod"] = stop_request.grace_period
        
        success, resp_text = await kubevirt_subresource_call(
            k8s_client, "put", namespace, name, "stop", body,
        )
        
        if not success:
            raise Exception(f"Stop subresource failed: {resp_text}")
        
        action = "force_stop" if stop_request.force else "stop"
        return VMStatusResponse(name=name, namespace=namespace, action=action, success=True)

    except HTTPException:
        raise
    except Exception as e:
        # If subresource API fails, fall back to patching runStrategy
        try:
            patch = {"spec": {"runStrategy": "Halted"}}
            await k8s_client.patch_virtual_machine(
                name=name, namespace=namespace, body=patch
            )
            return VMStatusResponse(
                name=name, namespace=namespace, action="stop", success=True,
                message="Stopped via runStrategy (subresource API unavailable)"
            )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to stop VM: {str(e)}",
            )


@router.post("/{name}/restart", response_model=VMStatusResponse)
async def restart_vm(request: Request, namespace: str, name: str) -> VMStatusResponse:
    """Restart a VirtualMachine by deleting its VMI."""
    k8s_client = request.app.state.k8s_client

    try:
        await k8s_client.delete_virtual_machine_instance(
            name=name, namespace=namespace
        )
        return VMStatusResponse(
            name=name, namespace=namespace, action="restart", success=True
        )

    except ApiException as e:
        if e.status == 404:
            # VMI doesn't exist, try to start the VM
            try:
                patch = {"spec": {"runStrategy": "Always"}}
                await k8s_client.patch_virtual_machine(
                    name=name, namespace=namespace, body=patch
                )
                return VMStatusResponse(
                    name=name, namespace=namespace, action="start", success=True
                )
            except ApiException as start_error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to restart VM: {start_error.reason}",
                )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to restart VM: {e.reason}",
        )


@router.post("/{name}/migrate", response_model=VMStatusResponse)
async def migrate_vm(
    request: Request,
    namespace: str,
    name: str,
    migrate_request: MigrateVMRequest,
    user: User = Depends(require_auth),
) -> VMStatusResponse:
    """Live migrate a running VM to a target node.
    
    1. Validates the VM is running and not already on the target node.
    2. Patches the VM template with nodeSelector for the target node.
    3. Creates a VirtualMachineInstanceMigration CR to trigger migration.
    """
    k8s_client = request.app.state.k8s_client
    target_node = migrate_request.target_node

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # 1. Verify VM exists and is running
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
        )
        vm_status = vm.get("status", {}).get("printableStatus", "Unknown")
        if vm_status != "Running":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"VM must be running to migrate. Current status: {vm_status}",
            )

        # 2. Get VMI to check current node
        try:
            vmi = await custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=name,
            )
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="VM instance not found — VM may not be fully running yet",
                )
            raise

        current_node = vmi.get("status", {}).get("nodeName")
        if current_node == target_node:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"VM is already running on node {target_node}",
            )

        # 3. Patch VM template with nodeSelector targeting the desired node
        node_selector_patch = {
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {
                            "kubernetes.io/hostname": target_node,
                        }
                    }
                }
            }
        }
        await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
            body=node_selector_patch,
            _content_type="application/merge-patch+json",
        )

        # 4. Create VirtualMachineInstanceMigration CR
        migration = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachineInstanceMigration",
            "metadata": {
                "generateName": f"migrate-{name}-",
                "namespace": namespace,
            },
            "spec": {
                "vmiName": name,
            },
        }

        await custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachineinstancemigrations",
            body=migration,
        )

        logger.info(
            f"User {user.username} initiated live migration of "
            f"{namespace}/{name} from {current_node} to {target_node}"
        )
        return VMStatusResponse(
            name=name,
            namespace=namespace,
            action="migrate",
            success=True,
            message=f"Migration started: {current_node} → {target_node}",
        )

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to migrate VM {namespace}/{name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to migrate VM: {e.reason}",
        )


@router.post("/{name}/recreate", status_code=status.HTTP_200_OK)
async def recreate_vm(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Recreate a VM from its original golden image.

    Stops the VM, deletes the root DataVolume, re-clones from the golden image,
    then starts the VM. Preserves: VM name, network config (same IP), SSH keys.
    """
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        core_api = client.CoreV1Api(k8s_client._api_client)

        # 1. Get the VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
        )

        # 2. Find the root disk and its DataVolume template
        dv_templates = vm.get("spec", {}).get("dataVolumeTemplates", [])
        volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])

        root_dv_name = None
        root_dv_template = None

        for dv_tpl in dv_templates:
            dv_name = dv_tpl.get("metadata", {}).get("name")
            for vol in volumes:
                if vol.get("dataVolume", {}).get("name") == dv_name:
                    root_dv_name = dv_name
                    root_dv_template = dv_tpl
                    break
            if root_dv_name:
                break

        if not root_dv_name or not root_dv_template:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VM has no dataVolumeTemplate — cannot determine golden image for recreate. "
                       "Only VMs created from templates support recreate.",
            )

        # Verify golden image source exists
        source = root_dv_template.get("spec", {}).get("source", {})
        pvc_source = source.get("pvc", {})
        golden_name = pvc_source.get("name")
        golden_ns = pvc_source.get("namespace", "golden-images")

        if not golden_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Root DataVolume has no PVC source — cannot determine golden image.",
            )

        try:
            await core_api.read_namespaced_persistent_volume_claim(
                name=golden_name, namespace=golden_ns,
            )
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Golden image '{golden_ns}/{golden_name}' no longer exists.",
                )
            raise

        # 3. Stop VM if running
        was_running = False
        original_run_strategy = vm.get("spec", {}).get("runStrategy", "Always")

        try:
            await custom_api.get_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachineinstances", name=name,
            )
            was_running = True
        except ApiException as e:
            if e.status != 404:
                raise

        if was_running or original_run_strategy in ("Always", "RerunOnFailure"):
            await custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachines", name=name,
                body={"spec": {"runStrategy": "Halted"}},
                _content_type="application/merge-patch+json",
            )
            for _ in range(90):
                try:
                    await custom_api.get_namespaced_custom_object(
                        group="kubevirt.io", version="v1", namespace=namespace,
                        plural="virtualmachineinstances", name=name,
                    )
                    await asyncio.sleep(2)
                except ApiException as e:
                    if e.status == 404:
                        break
                    raise

        # 4. Delete the root DV and PVC
        try:
            await custom_api.delete_namespaced_custom_object(
                group="cdi.kubevirt.io", version="v1beta1", namespace=namespace,
                plural="datavolumes", name=root_dv_name,
            )
            for _ in range(30):
                try:
                    await custom_api.get_namespaced_custom_object(
                        group="cdi.kubevirt.io", version="v1beta1", namespace=namespace,
                        plural="datavolumes", name=root_dv_name,
                    )
                    await asyncio.sleep(1)
                except ApiException:
                    break
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete DV {root_dv_name}: {e.reason}")

        try:
            await core_api.delete_namespaced_persistent_volume_claim(
                name=root_dv_name, namespace=namespace,
            )
            for _ in range(30):
                try:
                    await core_api.read_namespaced_persistent_volume_claim(
                        name=root_dv_name, namespace=namespace,
                    )
                    await asyncio.sleep(1)
                except ApiException:
                    break
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete PVC {root_dv_name}: {e.reason}")

        # 5. Start VM — KubeVirt will re-create the DV from dataVolumeTemplates
        await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
            body={"spec": {"runStrategy": original_run_strategy}},
            _content_type="application/merge-patch+json",
        )

        return {
            "status": "recreating",
            "vm": name,
            "root_disk": root_dv_name,
            "golden_image": f"{golden_ns}/{golden_name}",
            "was_running": was_running,
            "message": f"VM '{name}' is being recreated from golden image '{golden_name}'. "
                       "Network config and SSH keys are preserved.",
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to recreate VM: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to recreate VM: {e.reason}",
        )


@router.post("/{name}/clone", status_code=status.HTTP_201_CREATED)
async def clone_vm(
    request: Request,
    namespace: str,
    name: str,
    clone_request: CloneVMRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Clone a VM by duplicating its spec and DataVolumes."""
    k8s_client = request.app.state.k8s_client
    target_ns = clone_request.target_namespace or namespace

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # 1. Get source VM
        source_vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
        )

        source_spec = source_vm.get("spec", {})

        # 2. Deep copy the VM spec
        new_spec = copy.deepcopy(source_spec)

        # 3. Update DataVolume names and references
        dv_templates = new_spec.get("dataVolumeTemplates", [])
        volume_rename_map = {}

        for dv in dv_templates:
            old_name = dv.get("metadata", {}).get("name", "")
            new_dv_name = old_name.replace(name, clone_request.new_name, 1)
            if new_dv_name == old_name:
                new_dv_name = f"{clone_request.new_name}-{old_name}"
            dv["metadata"]["name"] = new_dv_name
            dv["metadata"].pop("uid", None)
            dv["metadata"].pop("resourceVersion", None)
            dv["metadata"].pop("creationTimestamp", None)
            volume_rename_map[old_name] = new_dv_name

        # Update volume references in template spec
        volumes = new_spec.get("template", {}).get("spec", {}).get("volumes", [])
        for vol in volumes:
            if "dataVolume" in vol:
                old_dv = vol["dataVolume"].get("name", "")
                if old_dv in volume_rename_map:
                    vol["dataVolume"]["name"] = volume_rename_map[old_dv]
            elif "persistentVolumeClaim" in vol:
                old_pvc = vol["persistentVolumeClaim"].get("claimName", "")
                if old_pvc in volume_rename_map:
                    vol["persistentVolumeClaim"]["claimName"] = volume_rename_map[old_pvc]

        # 4. Set run strategy
        new_spec["runStrategy"] = "Always" if clone_request.start else "Halted"
        new_spec.pop("running", None)

        # 5. Build clone manifest
        source_labels = source_vm.get("metadata", {}).get("labels", {})
        clone_labels = {k: v for k, v in source_labels.items()
                        if not k.startswith("kubevirt.io/")}
        clone_labels["kubevirt-ui.io/cloned-from"] = name

        clone_manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": clone_request.new_name,
                "namespace": target_ns,
                "labels": clone_labels,
                "annotations": {
                    "kubevirt-ui.io/cloned-from": f"{namespace}/{name}",
                },
            },
            "spec": new_spec,
        }

        # 6. Create the clone
        await custom_api.create_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=target_ns,
            plural="virtualmachines", body=clone_manifest,
        )

        return {
            "status": "cloned",
            "source": f"{namespace}/{name}",
            "clone": f"{target_ns}/{clone_request.new_name}",
            "start": clone_request.start,
            "volumes_cloned": list(volume_rename_map.values()),
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to clone VM: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clone VM: {e.reason}",
        )


@router.patch("/{name}/resize", status_code=status.HTTP_200_OK)
async def resize_vm(
    request: Request,
    namespace: str,
    name: str,
    resize_request: ResizeVMRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Resize VM CPU and/or memory."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # Build patch
        domain_patch: dict[str, Any] = {}

        if resize_request.cpu_cores is not None or resize_request.cpu_sockets is not None:
            cpu_patch: dict[str, Any] = {}
            if resize_request.cpu_cores is not None:
                cpu_patch["cores"] = resize_request.cpu_cores
            if resize_request.cpu_sockets is not None:
                cpu_patch["sockets"] = resize_request.cpu_sockets
            domain_patch["cpu"] = cpu_patch

        if resize_request.memory is not None:
            domain_patch["resources"] = {
                "requests": {"memory": resize_request.memory},
            }

        if not domain_patch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one of cpu_cores, cpu_sockets, or memory must be specified",
            )

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": domain_patch,
                    },
                },
            },
        }

        await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
            body=patch_body,
            _content_type="application/merge-patch+json",
        )

        # Check if VM is running (may need restart)
        needs_restart = False
        try:
            await custom_api.get_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachineinstances", name=name,
            )
            needs_restart = True
        except ApiException as e:
            if e.status != 404:
                raise

        changes = {}
        if resize_request.cpu_cores is not None:
            changes["cpu_cores"] = resize_request.cpu_cores
        if resize_request.cpu_sockets is not None:
            changes["cpu_sockets"] = resize_request.cpu_sockets
        if resize_request.memory is not None:
            changes["memory"] = resize_request.memory

        return {
            "status": "resized",
            "vm": name,
            "changes": changes,
            "needs_restart": needs_restart,
            "message": "VM resized. " + ("Restart required for changes to take effect." if needs_restart else ""),
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to resize VM: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resize VM: {e.reason}",
        )
