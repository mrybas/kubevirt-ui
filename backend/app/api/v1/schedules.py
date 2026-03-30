"""Scheduled Actions API endpoints.

Implements VM scheduled actions (auto-stop, auto-start, periodic reboot, auto-delete,
auto-snapshot) using Kubernetes CronJobs that call the KubeVirt subresource API.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth

router = APIRouter()
logger = logging.getLogger(__name__)

SCHEDULE_LABEL = "kubevirt-ui.io/scheduled-action"
VM_LABEL = "kubevirt-ui.io/vm"
NS_LABEL = "kubevirt-ui.io/vm-namespace"
ACTION_LABEL = "kubevirt-ui.io/action"


class CreateScheduleRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    action: str = Field(..., description="Action to perform: stop, start, restart, delete, snapshot")
    schedule: str = Field(..., description="Cron expression (e.g. '0 18 * * *' for daily at 18:00)")
    vm_name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63, description="Target VM name")
    vm_namespace: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63, description="Target VM namespace")
    suspend: bool = Field(False, description="Create in suspended state")


def _build_cronjob(req: CreateScheduleRequest, namespace: str) -> dict[str, Any]:
    """Build a CronJob manifest for a scheduled VM action."""
    # Map action to kubectl command (exec form — no shell, prevents injection)
    action_commands: dict[str, list[str]] = {
        "stop": [
            "kubectl", "patch", "vm", req.vm_name, "-n", req.vm_namespace,
            "--type", "merge", "-p", '{"spec":{"runStrategy":"Halted"}}',
        ],
        "start": [
            "kubectl", "patch", "vm", req.vm_name, "-n", req.vm_namespace,
            "--type", "merge", "-p", '{"spec":{"runStrategy":"Always"}}',
        ],
        "restart": [
            "kubectl", "delete", "vmi", req.vm_name, "-n", req.vm_namespace,
            "--ignore-not-found",
        ],
        "delete": [
            "kubectl", "delete", "vm", req.vm_name, "-n", req.vm_namespace,
        ],
        "snapshot": [
            "sh", "-c",
            "kubectl apply -f - <<'SNAPSHOT_EOF'\n"
            "apiVersion: snapshot.kubevirt.io/v1beta1\n"
            "kind: VirtualMachineSnapshot\n"
            "metadata:\n"
            f"  generateName: {req.vm_name}-auto-\n"
            f"  namespace: {req.vm_namespace}\n"
            "  labels:\n"
            '    kubevirt-ui.io/managed: "true"\n'
            f"    kubevirt-ui.io/vm: {req.vm_name}\n"
            '    kubevirt-ui.io/auto-snapshot: "true"\n'
            "spec:\n"
            "  source:\n"
            "    apiGroup: kubevirt.io\n"
            "    kind: VirtualMachine\n"
            f"    name: {req.vm_name}\n"
            "SNAPSHOT_EOF",
        ],
    }

    if req.action not in action_commands:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action '{req.action}'. Must be one of: stop, start, restart, delete, snapshot",
        )

    command = action_commands[req.action]

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": req.name,
            "namespace": namespace,
            "labels": {
                SCHEDULE_LABEL: "true",
                VM_LABEL: req.vm_name,
                NS_LABEL: req.vm_namespace,
                ACTION_LABEL: req.action,
                "kubevirt-ui.io/managed": "true",
            },
        },
        "spec": {
            "schedule": req.schedule,
            "suspend": req.suspend,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 1,
                    "ttlSecondsAfterFinished": 3600,
                    "template": {
                        "metadata": {
                            "labels": {
                                SCHEDULE_LABEL: "true",
                                VM_LABEL: req.vm_name,
                                ACTION_LABEL: req.action,
                            },
                        },
                        "spec": {
                            "serviceAccountName": "kubevirt-ui-scheduler",
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "action",
                                    "image": "bitnami/kubectl:latest",
                                    "command": command,
                                },
                            ],
                        },
                    },
                },
            },
        },
    }


@router.get("", status_code=status.HTTP_200_OK)
async def list_scheduled_actions(
    request: Request,
    namespace: str,
    vm_name: str | None = None,
    user: User = Depends(require_auth),
) -> list[dict[str, Any]]:
    """List scheduled actions (CronJobs) for VMs in a namespace."""
    k8s_client = request.app.state.k8s_client

    try:
        batch_api = client.BatchV1Api(k8s_client._api_client)

        label_selector = f"{SCHEDULE_LABEL}=true"
        if vm_name:
            label_selector += f",{VM_LABEL}={vm_name}"

        result = await batch_api.list_namespaced_cron_job(
            namespace=namespace,
            label_selector=label_selector,
        )

        schedules = []
        for cj in result.items:
            labels = cj.metadata.labels or {}
            last_schedule = None
            if cj.status and cj.status.last_schedule_time:
                last_schedule = cj.status.last_schedule_time.isoformat()

            # Count active jobs
            active_count = len(cj.status.active) if cj.status and cj.status.active else 0

            schedules.append({
                "name": cj.metadata.name,
                "namespace": cj.metadata.namespace,
                "vm_name": labels.get(VM_LABEL, ""),
                "vm_namespace": labels.get(NS_LABEL, ""),
                "action": labels.get(ACTION_LABEL, ""),
                "schedule": cj.spec.schedule,
                "suspended": cj.spec.suspend or False,
                "last_schedule_time": last_schedule,
                "active_jobs": active_count,
                "creation_time": cj.metadata.creation_timestamp.isoformat() if cj.metadata.creation_timestamp else "",
            })

        return schedules

    except ApiException as e:
        logger.error(f"Failed to list scheduled actions: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list scheduled actions: {e.reason}",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_scheduled_action(
    request: Request,
    namespace: str,
    schedule_request: CreateScheduleRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a scheduled action (CronJob) for a VM."""
    k8s_client = request.app.state.k8s_client

    try:
        batch_api = client.BatchV1Api(k8s_client._api_client)

        cronjob_body = _build_cronjob(schedule_request, namespace)

        result = await batch_api.create_namespaced_cron_job(
            namespace=namespace,
            body=cronjob_body,
        )

        return {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "vm_name": schedule_request.vm_name,
            "action": schedule_request.action,
            "schedule": schedule_request.schedule,
            "suspended": schedule_request.suspend,
        }

    except ApiException as e:
        logger.error(f"Failed to create scheduled action: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create scheduled action: {e.reason}",
        )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_action(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_auth),
) -> None:
    """Delete a scheduled action (CronJob)."""
    k8s_client = request.app.state.k8s_client

    try:
        batch_api = client.BatchV1Api(k8s_client._api_client)

        await batch_api.delete_namespaced_cron_job(
            name=name,
            namespace=namespace,
            propagation_policy="Background",
        )

    except ApiException as e:
        if e.status == 404:
            return
        logger.error(f"Failed to delete scheduled action: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete scheduled action: {e.reason}",
        )


class PatchScheduleRequest(BaseModel):
    suspend: bool | None = Field(None, description="Suspend or resume the schedule")
    schedule: str | None = Field(None, description="New cron expression")


@router.patch("/{name}", status_code=status.HTTP_200_OK)
async def update_scheduled_action(
    request: Request,
    namespace: str,
    name: str,
    patch_request: PatchScheduleRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Update a scheduled action (suspend/resume or change schedule)."""
    k8s_client = request.app.state.k8s_client

    try:
        batch_api = client.BatchV1Api(k8s_client._api_client)

        patch_body: dict[str, Any] = {"spec": {}}
        if patch_request.suspend is not None:
            patch_body["spec"]["suspend"] = patch_request.suspend
        if patch_request.schedule is not None:
            patch_body["spec"]["schedule"] = patch_request.schedule

        result = await batch_api.patch_namespaced_cron_job(
            name=name,
            namespace=namespace,
            body=patch_body,
        )

        labels = result.metadata.labels or {}
        return {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "vm_name": labels.get(VM_LABEL, ""),
            "action": labels.get(ACTION_LABEL, ""),
            "schedule": result.spec.schedule,
            "suspended": result.spec.suspend or False,
        }

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
        logger.error(f"Failed to update scheduled action: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update scheduled action: {e.reason}",
        )


@router.post("/{name}/trigger", status_code=status.HTTP_200_OK)
async def trigger_scheduled_action(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Trigger a scheduled action immediately by creating a Job from the CronJob."""
    k8s_client = request.app.state.k8s_client

    try:
        batch_api = client.BatchV1Api(k8s_client._api_client)

        # Get the CronJob to extract job template
        cj = await batch_api.read_namespaced_cron_job(name=name, namespace=namespace)

        # Create a Job from the CronJob template
        job_name = f"{name}-manual-{int(time.time())}"
        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": dict(cj.metadata.labels or {}),
                "annotations": {
                    "cronjob.kubernetes.io/instantiate": "manual",
                },
            },
            "spec": cj.spec.job_template.spec.to_dict(),
        }

        await batch_api.create_namespaced_job(namespace=namespace, body=job_body)

        return {
            "status": "triggered",
            "job": job_name,
            "schedule": name,
        }

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
        logger.error(f"Failed to trigger scheduled action: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to trigger scheduled action: {e.reason}",
        )
