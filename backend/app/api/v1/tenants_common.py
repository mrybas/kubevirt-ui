"""Shared constants and helpers for tenant management."""

import logging
import os
from typing import Any

import yaml
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from app.core.constants import CAPI_API_GROUP, CAPI_API_VERSION, KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.models.tenant import AddonCatalog, AddonComponent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_NS_PREFIX = "tenant-"
CATALOG_CONFIGMAP = "tenant-addon-catalog"
CATALOG_NAMESPACE = "flux-system"

CAPI_GROUP = CAPI_API_GROUP
CAPI_VERSION = CAPI_API_VERSION
KAMAJI_CP_GROUP = "controlplane.cluster.x-k8s.io"
KAMAJI_CP_VERSION = "v1alpha1"
KUBEVIRT_INFRA_GROUP = "infrastructure.cluster.x-k8s.io"
KUBEVIRT_INFRA_VERSION = "v1alpha1"
FLUX_HELM_GROUP = "helm.toolkit.fluxcd.io"
FLUX_HELM_VERSION = "v2"

# VpcDns configuration — VIP from Service CIDR (NOT from VPC subnet)
VPCDNS_VIP = "10.96.0.200"
VPCDNS_FORWARD_DNS = "10.96.0.10"  # kube-dns ClusterIP (Talos uses this)

METALB_IP = "192.168.196.199"

# OIDC defaults (can be overridden by env)
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "kubevirt-ui")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_ns(name: str) -> str:
    return f"{TENANT_NS_PREFIX}{name}"


def _endpoint_host(name: str) -> str:
    return f"{name}.{METALB_IP}.nip.io"


async def _get_addon_catalog(k8s) -> AddonCatalog:
    """Read addon catalog from ConfigMap."""
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=CATALOG_CONFIGMAP, namespace=CATALOG_NAMESPACE,
        )
        raw = yaml.safe_load(cm.data.get("catalog.yaml", "{}")) if cm.data else {}
        return AddonCatalog(
            git_repository_ref=raw.get("gitRepositoryRef", {}),
            base_path=raw.get("basePath", "base"),
            components=[AddonComponent(**c) for c in raw.get("components", [])],
        )
    except ApiException as e:
        if e.status == 404:
            logger.warning("Addon catalog ConfigMap not found")
            return AddonCatalog()
        raise


async def _namespace_exists(k8s, ns: str) -> bool:
    try:
        await k8s.core_api.read_namespace(name=ns)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


async def _create_namespace(k8s, ns: str, tenant_name: str, worker_type: str = "vm") -> None:
    body = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=ns,
            labels={
                "kubevirt-ui.io/tenant": tenant_name,
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/worker-type": worker_type,
                # Kamaji control-plane pods + KubeVirt VMs need elevated privileges
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/warn": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
            },
        )
    )
    await k8s.core_api.create_namespace(body=body)
