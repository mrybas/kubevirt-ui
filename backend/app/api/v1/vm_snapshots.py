"""VM snapshot operations: list, create, delete, restore."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth, require_env_member, require_env_viewer
from app.core.naming import get_display_name, with_synthetic_metadata

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request models ────────────────────────────────────────────────────────────


class CreateVMSnapshotRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)


class RestoreVMSnapshotRequest(BaseModel):
    display_name: str | None = Field(
        None,
        max_length=100,
        description="Human-friendly name for the restore (auto-generated if empty)",
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/{name}/snapshots", status_code=status.HTTP_200_OK)
async def list_vm_snapshots(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_env_viewer()),
) -> list[dict[str, Any]]:
    """List VirtualMachineSnapshots for a specific VM."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        result = await custom_api.list_namespaced_custom_object(
            group="snapshot.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="virtualmachinesnapshots",
        )

        snapshots = []
        for snap in result.get("items", []):
            source = snap.get("spec", {}).get("source", {})
            if source.get("kind") != "VirtualMachine" or source.get("name") != name:
                continue

            snap_status = snap.get("status", {})
            snap_metadata = snap.get("metadata", {})
            snapshots.append(
                {
                    "name": snap_metadata["name"],
                    "display_name": get_display_name(snap_metadata) or snap_metadata["name"],
                    "namespace": namespace,
                    "vm_name": name,
                    "phase": snap_status.get("phase", "Unknown"),
                    "ready": snap_status.get("readyToUse", False),
                    "creation_time": snap_metadata.get("creationTimestamp", ""),
                    "indications": snap_status.get("indications", []),
                    "error": snap_status.get("error", {}).get("message"),
                }
            )

        snapshots.sort(key=lambda x: x["creation_time"], reverse=True)
        return snapshots

    except ApiException as e:
        if e.status == 404:
            return []
        logger.error(f"Failed to list VM snapshots: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list VM snapshots: {e.reason}",
        )


@router.post("/{name}/snapshots", status_code=status.HTTP_201_CREATED)
async def create_vm_snapshot(
    request: Request,
    namespace: str,
    name: str,
    snap_request: CreateVMSnapshotRequest,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Create a VirtualMachineSnapshot for a VM."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        snapshot_body: dict[str, Any] = {
            "apiVersion": "snapshot.kubevirt.io/v1beta1",
            "kind": "VirtualMachineSnapshot",
            "metadata": {
                "namespace": namespace,
                "labels": {
                    "kubevirt-ui.io/managed": "true",
                    "kubevirt-ui.io/vm": name,
                },
            },
            "spec": {
                "source": {
                    "apiGroup": "kubevirt.io",
                    "kind": "VirtualMachine",
                    "name": name,
                },
            },
        }
        with_synthetic_metadata(snapshot_body, snap_request.display_name, namespace=namespace)

        result = await custom_api.create_namespaced_custom_object(
            group="snapshot.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="virtualmachinesnapshots",
            body=snapshot_body,
        )

        return {
            "name": result["metadata"]["name"],
            "display_name": snap_request.display_name,
            "namespace": namespace,
            "vm_name": name,
            "phase": "InProgress",
            "ready": False,
            "creation_time": result["metadata"].get("creationTimestamp", ""),
            "indications": [],
            "error": None,
        }

    except ApiException as e:
        logger.error(f"Failed to create VM snapshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create VM snapshot: {e.reason}",
        )


@router.delete("/{name}/snapshots/{snapshot_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vm_snapshot(
    request: Request,
    namespace: str,
    name: str,
    snapshot_name: str,
    user: User = Depends(require_env_member()),
) -> None:
    """Delete a VirtualMachineSnapshot."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        await custom_api.delete_namespaced_custom_object(
            group="snapshot.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="virtualmachinesnapshots",
            name=snapshot_name,
        )

    except ApiException as e:
        if e.status == 404:
            return
        logger.error(f"Failed to delete VM snapshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete VM snapshot: {e.reason}",
        )


@router.post("/{name}/snapshots/{snapshot_name}/restore", status_code=status.HTTP_200_OK)
async def restore_vm_snapshot(
    request: Request,
    namespace: str,
    name: str,
    snapshot_name: str,
    restore_request: RestoreVMSnapshotRequest | None = None,
    user: User = Depends(require_env_member()),
) -> dict[str, Any]:
    """Restore a VM from a VirtualMachineSnapshot.

    Stops the VM if running, creates a VirtualMachineRestore, waits for completion,
    then restarts if it was running.
    """
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # 1. Stop VM if running
        was_running = False
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
        )
        original_run_strategy = vm.get("spec", {}).get("runStrategy", "Always")

        try:
            await custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=name,
            )
            was_running = True
        except ApiException as e:
            if e.status != 404:
                raise

        if was_running:
            await custom_api.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=name,
                body={"spec": {"runStrategy": "Halted"}},
                _content_type="application/merge-patch+json",
            )
            for _ in range(60):
                try:
                    await custom_api.get_namespaced_custom_object(
                        group="kubevirt.io",
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachineinstances",
                        name=name,
                    )
                    await asyncio.sleep(2)
                except ApiException as e:
                    if e.status == 404:
                        break
                    raise

        # 2. Create VirtualMachineRestore. Use display_name if supplied,
        # otherwise default to "Restore of {snapshot_name}". K8s assigns the
        # actual resource name via generateName.
        display_name = (
            restore_request.display_name
            if restore_request and restore_request.display_name
            else f"Restore of {snapshot_name}"
        )

        restore_body: dict[str, Any] = {
            "apiVersion": "snapshot.kubevirt.io/v1beta1",
            "kind": "VirtualMachineRestore",
            "metadata": {
                "namespace": namespace,
                "labels": {
                    "kubevirt-ui.io/managed": "true",
                    "kubevirt-ui.io/vm": name,
                },
            },
            "spec": {
                "target": {
                    "apiGroup": "kubevirt.io",
                    "kind": "VirtualMachine",
                    "name": name,
                },
                "virtualMachineSnapshotName": snapshot_name,
            },
        }
        with_synthetic_metadata(restore_body, display_name, namespace=namespace)

        created_restore = await custom_api.create_namespaced_custom_object(
            group="snapshot.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="virtualmachinerestores",
            body=restore_body,
        )
        restore_name = created_restore.get("metadata", {}).get("name", "")

        # 3. Wait for restore to complete
        for _ in range(120):
            try:
                restore_obj = await custom_api.get_namespaced_custom_object(
                    group="snapshot.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="virtualmachinerestores",
                    name=restore_name,
                )
                conditions = restore_obj.get("status", {}).get("conditions", [])
                for cond in conditions:
                    if cond.get("type") == "Ready" and cond.get("status") == "True":
                        break
                else:
                    await asyncio.sleep(2)
                    continue
                break
            except ApiException:
                await asyncio.sleep(2)

        # 4. Restart VM if it was running
        if was_running:
            await custom_api.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=name,
                body={"spec": {"runStrategy": original_run_strategy}},
                _content_type="application/merge-patch+json",
            )

        return {
            "status": "restored",
            "vm": name,
            "snapshot": snapshot_name,
            "restore": restore_name,
            "restore_display_name": display_name,
            "was_running": was_running,
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to restore VM snapshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to restore VM snapshot: {e.reason}",
        )
