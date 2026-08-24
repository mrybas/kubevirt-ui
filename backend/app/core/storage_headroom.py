"""Is there room for this disk — asked before it is created.

Creating a disk creates a DataVolume, and a DataVolume is not what the quota
counts: CDI makes a PersistentVolumeClaim from it a moment later, and *that*
is what the API server refuses. So the create returned 201, the dialog closed
the way it closes on success, and the disk never existed.

Measured in UAT run 4: a 100Gi disk against a 12Gi environment quota with
10Gi already used. `POST` succeeded, no error anywhere in the UI, and the
cluster kept a Pending DataVolume with `ErrExceededQuota` that nobody could
see and that went on counting against the quota. The plan asked for an honest
error in human language; what it got was worse than a 500, because a 500
would at least have said no.

This asks the ResourceQuota first. It is a courtesy, not the enforcement —
the API server is still the thing that says no, and `status.used` is updated
asynchronously, so a request that squeaks through here can still be refused
later. That is fine. What is not fine is refusing it silently.
"""

from typing import Any

from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.folders import _format_quantity, parse_quantity

STORAGE = "requests.storage"


async def assert_storage_headroom(
    k8s: Any, namespace: str, asked: str, what: str = "this disk",
) -> None:
    """Refuse a disk the namespace's quota has no room for.

    Every quota in the namespace is checked, not just the first: Kubernetes
    satisfies all of them, so the binding one is whichever has least room.
    A namespace with no quota constrains nothing and returns at once.
    """
    wanted = parse_quantity(asked)
    if wanted is None:
        return

    try:
        quotas = await k8s.core_api.list_namespaced_resource_quota(namespace=namespace)
    except ApiException:
        # A quota that cannot be read has not said no. The API server will
        # still enforce whatever is there; this check simply does not run.
        return

    for quota in quotas.items:
        hard = parse_quantity(((quota.spec.hard if quota.spec else None) or {}).get(STORAGE))
        if hard is None:
            continue
        used = parse_quantity(
            ((quota.status.used if quota.status else None) or {}).get(STORAGE)
        ) or 0.0
        free = max(0.0, hard - used)
        if wanted > free + 1e-9:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{namespace} has {_format_quantity('storage', hard)} of storage "
                    f"and {_format_quantity('storage', used)} of it is already used, "
                    f"so {_format_quantity('storage', free)} is free — "
                    f"{what} asks for {_format_quantity('storage', wanted)}. "
                    f"Delete something, or raise the environment's quota."
                ),
            )
