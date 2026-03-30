"""Shared CIDR allocator using ConfigMap-based counter with optimistic locking."""

import asyncio
import logging

from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException, V1ConfigMap, V1ObjectMeta

from app.core.constants import SYSTEM_NAMESPACE

logger = logging.getLogger(__name__)

VPC_CIDR_CONFIGMAP = "vpc-cidr-allocator"
VPC_CIDR_BASE = 200  # 10.{200+N}.0.0/24 — above K8s service CIDR (10.96.0.0/12)

MAX_RETRIES = 5
BASE_DELAY = 0.1  # seconds


async def allocate_vpc_cidr(k8s) -> tuple[str, str]:
    """Allocate the next VPC CIDR using ConfigMap-based counter with optimistic locking.

    Uses replace with resourceVersion for optimistic concurrency control.
    Retries on 409 Conflict with exponential backoff.
    Returns (cidr, gateway_ip) tuple.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await _allocate_vpc_cidr_once(k8s)
        except ApiException as e:
            if e.status == 409 and attempt < MAX_RETRIES - 1:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(f"VPC CIDR allocation conflict (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)
                continue
            raise

    raise HTTPException(status_code=409, detail="VPC CIDR allocation failed after retries")


async def _allocate_vpc_cidr_once(k8s) -> tuple[str, str]:
    """Single attempt to allocate the next VPC CIDR."""
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=VPC_CIDR_CONFIGMAP, namespace=SYSTEM_NAMESPACE,
        )
        data = cm.data or {}
        next_index = int(data.get("next_index", "0"))
        resource_version = cm.metadata.resource_version
    except ApiException as e:
        if e.status == 404:
            cm = await k8s.core_api.create_namespaced_config_map(
                namespace=SYSTEM_NAMESPACE,
                body=V1ConfigMap(
                    metadata=V1ObjectMeta(
                        name=VPC_CIDR_CONFIGMAP,
                        labels={"kubevirt-ui.io/managed": "true"},
                    ),
                    data={"next_index": "0"},
                ),
            )
            next_index = 0
            resource_version = cm.metadata.resource_version
        else:
            raise

    second_octet = VPC_CIDR_BASE + next_index
    if second_octet > 254:
        raise HTTPException(status_code=409, detail="VPC CIDR pool exhausted (max 55 VPCs)")

    cidr = f"10.{second_octet}.0.0/24"
    gateway = f"10.{second_octet}.0.1"

    # Increment counter with optimistic lock (resourceVersion).
    # If another request raced us, this will return 409 Conflict.
    await k8s.core_api.replace_namespaced_config_map(
        name=VPC_CIDR_CONFIGMAP,
        namespace=SYSTEM_NAMESPACE,
        body=V1ConfigMap(
            metadata=V1ObjectMeta(
                name=VPC_CIDR_CONFIGMAP,
                namespace=SYSTEM_NAMESPACE,
                resource_version=resource_version,
                labels={"kubevirt-ui.io/managed": "true"},
            ),
            data={"next_index": str(next_index + 1)},
        ),
    )

    return cidr, gateway
