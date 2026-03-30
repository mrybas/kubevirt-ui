"""Velero Backup/Restore/Schedule API endpoints.

Manages Velero CRDs for cluster-level backup and restore operations:
- Backups: on-demand and scheduled cluster/namespace backups
- Restores: restore from a backup
- Schedules: recurring backup schedules
- Storage: BackupStorageLocation status
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth
from app.core.errors import k8s_error_to_http

router = APIRouter()
logger = logging.getLogger(__name__)

VELERO_GROUP = "velero.io"
VELERO_VERSION = "v1"


# ── Request models ────────────────────────────────────────────────────────────


class VeleroBackupCreateRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    included_namespaces: list[str] = []
    label_selector: str = ""
    snapshot_volumes: bool = True
    ttl: str = "720h"


class VeleroScheduleCreateRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    schedule: str = Field(..., description="Cron expression (e.g. '0 2 * * *')")
    included_namespaces: list[str] = []
    label_selector: str = ""
    snapshot_volumes: bool = True
    ttl: str = "720h"


class VeleroRestoreCreateRequest(BaseModel):
    name: str | None = Field(None, description="Name for the restore (auto-generated if empty)")
    included_namespaces: list[str] = []
    label_selector: str = ""
    restore_pvs: bool = True


class VeleroSchedulePatchRequest(BaseModel):
    paused: bool | None = Field(None, description="Pause or unpause the schedule")


class StorageLocationCreateRequest(BaseModel):
    name: str = Field("default", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    provider: str = Field("aws", description="Storage provider: aws (S3-compatible), gcp, azure")
    bucket: str = Field(..., description="Bucket name")
    prefix: str = Field("", description="Key prefix within bucket")
    region: str = Field("minio", description="Region (use 'minio' for MinIO)")
    s3_url: str = Field("", description="S3 endpoint URL (required for MinIO, e.g. http://minio.o0-minio.svc:9000)")
    s3_force_path_style: bool = Field(True, description="Use path-style URLs (required for MinIO)")
    credential_secret: str = Field("cloud-credentials", description="Secret name with storage credentials")
    credential_key: str = Field("cloud", description="Key in the credentials secret")
    access_mode: str = Field("ReadWrite", description="ReadWrite or ReadOnly")
    default: bool = Field(True, description="Set as default backup location")


class StorageLocationUpdateRequest(BaseModel):
    bucket: str | None = None
    prefix: str | None = None
    s3_url: str | None = None
    access_mode: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _find_velero_namespace(k8s_client: Any) -> str:
    """Find the namespace where Velero is installed by looking for BackupStorageLocations."""
    custom_api = client.CustomObjectsApi(k8s_client._api_client)

    try:
        result = await custom_api.list_cluster_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            plural="backupstoragelocations",
        )
        items = result.get("items", [])
        if items:
            return items[0]["metadata"]["namespace"]
    except ApiException:
        pass

    # Fallback: look for namespace with "velero" in name
    core_api = client.CoreV1Api(k8s_client._api_client)
    try:
        ns_list = await core_api.list_namespace(
            label_selector="app.kubernetes.io/name=velero",
        )
        if ns_list.items:
            return ns_list.items[0].metadata.name
    except ApiException:
        pass

    return "velero"


def _parse_backup(item: dict[str, Any]) -> dict[str, Any]:
    """Parse a Velero Backup CR into a response dict."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    backup_status = item.get("status", {})

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "phase": backup_status.get("phase", "Unknown"),
        "included_namespaces": spec.get("includedNamespaces", []),
        "label_selector": spec.get("labelSelector", {}).get("matchLabels", {}),
        "snapshot_volumes": spec.get("snapshotVolumes", True),
        "ttl": spec.get("ttl", ""),
        "started_at": backup_status.get("startTimestamp", ""),
        "completed_at": backup_status.get("completionTimestamp", ""),
        "expiration": backup_status.get("expiration", ""),
        "items_backed_up": backup_status.get("progress", {}).get("itemsBackedUp", 0),
        "total_items": backup_status.get("progress", {}).get("totalItems", 0),
        "errors": backup_status.get("errors", 0),
        "warnings": backup_status.get("warnings", 0),
        "creation_time": metadata.get("creationTimestamp", ""),
    }


def _parse_restore(item: dict[str, Any]) -> dict[str, Any]:
    """Parse a Velero Restore CR into a response dict."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    restore_status = item.get("status", {})

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "phase": restore_status.get("phase", "Unknown"),
        "backup_name": spec.get("backupName", ""),
        "included_namespaces": spec.get("includedNamespaces", []),
        "restore_pvs": spec.get("restorePVs", True),
        "started_at": restore_status.get("startTimestamp", ""),
        "completed_at": restore_status.get("completionTimestamp", ""),
        "errors": restore_status.get("errors", 0),
        "warnings": restore_status.get("warnings", 0),
        "creation_time": metadata.get("creationTimestamp", ""),
    }


def _parse_schedule(item: dict[str, Any]) -> dict[str, Any]:
    """Parse a Velero Schedule CR into a response dict."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    sched_status = item.get("status", {})
    template = spec.get("template", {})

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "schedule": spec.get("schedule", ""),
        "paused": spec.get("paused", False),
        "included_namespaces": template.get("includedNamespaces", []),
        "label_selector": template.get("labelSelector", {}).get("matchLabels", {}),
        "snapshot_volumes": template.get("snapshotVolumes", True),
        "ttl": template.get("ttl", ""),
        "phase": sched_status.get("phase", "Unknown"),
        "last_backup": sched_status.get("lastBackup", ""),
        "creation_time": metadata.get("creationTimestamp", ""),
    }


def _parse_storage_location(item: dict[str, Any]) -> dict[str, Any]:
    """Parse a Velero BackupStorageLocation CR into a response dict."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    loc_status = item.get("status", {})

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "provider": spec.get("provider", ""),
        "bucket": spec.get("objectStorage", {}).get("bucket", ""),
        "prefix": spec.get("objectStorage", {}).get("prefix", ""),
        "phase": loc_status.get("phase", "Unknown"),
        "last_synced": loc_status.get("lastSyncedTime", ""),
        "last_validation": loc_status.get("lastValidationTime", ""),
        "access_mode": spec.get("accessMode", ""),
        "default": spec.get("default", False),
        "creation_time": metadata.get("creationTimestamp", ""),
    }


# ── Backup endpoints ─────────────────────────────────────────────────────────


@router.get("/backups", status_code=status.HTTP_200_OK)
async def list_velero_backups(
    request: Request,
    user: User = Depends(require_auth),
) -> list[dict[str, Any]]:
    """List all Velero backups."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.list_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="backups",
        )
        backups = [_parse_backup(item) for item in result.get("items", [])]
        backups.sort(key=lambda x: x["creation_time"], reverse=True)
        return backups

    except ApiException as e:
        if e.status == 404:
            return []
        raise k8s_error_to_http(e, "listing Velero backups")


@router.post("/backups", status_code=status.HTTP_201_CREATED)
async def create_velero_backup(
    request: Request,
    backup_request: VeleroBackupCreateRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a manual Velero backup."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    spec: dict[str, Any] = {
        "snapshotVolumes": backup_request.snapshot_volumes,
        "ttl": backup_request.ttl,
    }
    if backup_request.included_namespaces:
        spec["includedNamespaces"] = backup_request.included_namespaces
    if backup_request.label_selector:
        spec["labelSelector"] = {"matchLabels": _parse_label_selector(backup_request.label_selector)}

    body = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "Backup",
        "metadata": {
            "name": backup_request.name,
            "namespace": velero_ns,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": spec,
    }

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="backups",
            body=body,
        )
        return _parse_backup(result)

    except ApiException as e:
        raise k8s_error_to_http(e, "creating Velero backup")


@router.delete("/backups/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_velero_backup(
    request: Request,
    name: str,
    user: User = Depends(require_auth),
) -> None:
    """Delete a Velero backup by creating a DeleteBackupRequest."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    # Velero uses DeleteBackupRequest CRD to trigger backup deletion
    dbr_body = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "DeleteBackupRequest",
        "metadata": {
            "name": f"delete-{name}-{int(time.time())}",
            "namespace": velero_ns,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "backupName": name,
        },
    }

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        await custom_api.create_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="deletebackuprequests",
            body=dbr_body,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "deleting Velero backup")


# ── Restore endpoints ────────────────────────────────────────────────────────


@router.post("/backups/{backup_name}/restore", status_code=status.HTTP_201_CREATED)
async def create_velero_restore(
    request: Request,
    backup_name: str,
    restore_request: VeleroRestoreCreateRequest | None = None,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a Velero Restore from a backup."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    restore_name = (
        (restore_request.name if restore_request and restore_request.name else None)
        or f"restore-{backup_name}-{int(time.time())}"
    )

    spec: dict[str, Any] = {
        "backupName": backup_name,
        "restorePVs": restore_request.restore_pvs if restore_request else True,
    }
    if restore_request and restore_request.included_namespaces:
        spec["includedNamespaces"] = restore_request.included_namespaces
    if restore_request and restore_request.label_selector:
        spec["labelSelector"] = {"matchLabels": _parse_label_selector(restore_request.label_selector)}

    body = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "Restore",
        "metadata": {
            "name": restore_name,
            "namespace": velero_ns,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": spec,
    }

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="restores",
            body=body,
        )
        return _parse_restore(result)

    except ApiException as e:
        raise k8s_error_to_http(e, "creating Velero restore")


# ── Schedule endpoints ────────────────────────────────────────────────────────


@router.get("/schedules", status_code=status.HTTP_200_OK)
async def list_velero_schedules(
    request: Request,
    user: User = Depends(require_auth),
) -> list[dict[str, Any]]:
    """List all Velero backup schedules."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.list_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="schedules",
        )
        schedules = [_parse_schedule(item) for item in result.get("items", [])]
        schedules.sort(key=lambda x: x["creation_time"], reverse=True)
        return schedules

    except ApiException as e:
        if e.status == 404:
            return []
        raise k8s_error_to_http(e, "listing Velero schedules")


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
async def create_velero_schedule(
    request: Request,
    schedule_request: VeleroScheduleCreateRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a Velero backup schedule."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    template: dict[str, Any] = {
        "snapshotVolumes": schedule_request.snapshot_volumes,
        "ttl": schedule_request.ttl,
    }
    if schedule_request.included_namespaces:
        template["includedNamespaces"] = schedule_request.included_namespaces
    if schedule_request.label_selector:
        template["labelSelector"] = {"matchLabels": _parse_label_selector(schedule_request.label_selector)}

    body = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "Schedule",
        "metadata": {
            "name": schedule_request.name,
            "namespace": velero_ns,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "schedule": schedule_request.schedule,
            "template": template,
        },
    }

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="schedules",
            body=body,
        )
        return _parse_schedule(result)

    except ApiException as e:
        raise k8s_error_to_http(e, "creating Velero schedule")


@router.delete("/schedules/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_velero_schedule(
    request: Request,
    name: str,
    user: User = Depends(require_auth),
) -> None:
    """Delete a Velero backup schedule."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        await custom_api.delete_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="schedules",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise k8s_error_to_http(e, "deleting Velero schedule")


@router.patch("/schedules/{name}", status_code=status.HTTP_200_OK)
async def patch_velero_schedule(
    request: Request,
    name: str,
    patch_request: VeleroSchedulePatchRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Pause or unpause a Velero backup schedule."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    patch_body: dict[str, Any] = {"spec": {}}
    if patch_request.paused is not None:
        patch_body["spec"]["paused"] = patch_request.paused

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.patch_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="schedules",
            name=name, body=patch_body,
            _content_type="application/merge-patch+json",
        )
        return _parse_schedule(result)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Velero schedule not found")
        raise k8s_error_to_http(e, "patching Velero schedule")


# ── Storage endpoints ────────────────────────────────────────────────────────


@router.get("/storage", status_code=status.HTTP_200_OK)
async def list_velero_storage_locations(
    request: Request,
    user: User = Depends(require_auth),
) -> list[dict[str, Any]]:
    """List Velero BackupStorageLocations with status."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.list_cluster_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            plural="backupstoragelocations",
        )
        return [_parse_storage_location(item) for item in result.get("items", [])]

    except ApiException as e:
        if e.status == 404:
            return []
        raise k8s_error_to_http(e, "listing Velero storage locations")


@router.post("/storage", status_code=status.HTTP_201_CREATED)
async def create_storage_location(
    request: Request,
    data: StorageLocationCreateRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a Velero BackupStorageLocation."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    bsl_config: dict[str, Any] = {}
    if data.region:
        bsl_config["region"] = data.region
    if data.s3_url:
        bsl_config["s3Url"] = data.s3_url
    if data.s3_force_path_style:
        bsl_config["s3ForcePathStyle"] = "true"

    bsl_body: dict[str, Any] = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "BackupStorageLocation",
        "metadata": {
            "name": data.name,
            "namespace": velero_ns,
        },
        "spec": {
            "provider": data.provider,
            "objectStorage": {
                "bucket": data.bucket,
                **({"prefix": data.prefix} if data.prefix else {}),
            },
            "credential": {
                "name": data.credential_secret,
                "key": data.credential_key,
            },
            "accessMode": data.access_mode,
            "default": data.default,
            **({"config": bsl_config} if bsl_config else {}),
        },
    }

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="backupstoragelocations",
            body=bsl_body,
        )
        logger.info(f"Created BSL '{data.name}' in {velero_ns} (bucket={data.bucket})")
        return _parse_storage_location(result)

    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"Storage location '{data.name}' already exists")
        raise k8s_error_to_http(e, "creating backup storage location")


@router.put("/storage/{name}", status_code=status.HTTP_200_OK)
async def update_storage_location(
    request: Request,
    name: str,
    data: StorageLocationUpdateRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Update a Velero BackupStorageLocation."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    patch: dict[str, Any] = {"spec": {}}
    if data.bucket is not None or data.prefix is not None:
        obj_storage: dict[str, Any] = {}
        if data.bucket is not None:
            obj_storage["bucket"] = data.bucket
        if data.prefix is not None:
            obj_storage["prefix"] = data.prefix
        patch["spec"]["objectStorage"] = obj_storage
    if data.s3_url is not None:
        patch["spec"].setdefault("config", {})["s3Url"] = data.s3_url
    if data.access_mode is not None:
        patch["spec"]["accessMode"] = data.access_mode

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.patch_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="backupstoragelocations",
            name=name, body=patch,
            _content_type="application/merge-patch+json",
        )
        logger.info(f"Updated BSL '{name}'")
        return _parse_storage_location(result)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Storage location '{name}' not found")
        raise k8s_error_to_http(e, "updating backup storage location")


@router.delete("/storage/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_storage_location(
    request: Request,
    name: str,
    user: User = Depends(require_auth),
) -> None:
    """Delete a Velero BackupStorageLocation."""
    k8s_client = request.app.state.k8s_client
    velero_ns = await _find_velero_namespace(k8s_client)

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        await custom_api.delete_namespaced_custom_object(
            group=VELERO_GROUP, version=VELERO_VERSION,
            namespace=velero_ns, plural="backupstoragelocations",
            name=name,
        )
        logger.info(f"Deleted BSL '{name}'")

    except ApiException as e:
        if e.status == 404:
            return
        raise k8s_error_to_http(e, "deleting backup storage location")


# ── Utilities ─────────────────────────────────────────────────────────────────


def _parse_label_selector(selector: str) -> dict[str, str]:
    """Parse 'key1=val1,key2=val2' into a dict."""
    labels = {}
    for part in selector.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k.strip()] = v.strip()
    return labels
