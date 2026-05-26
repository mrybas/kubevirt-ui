"""Shared constants and helpers for tenant management."""

import logging
import os
import time
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

# OIDC defaults (can be overridden by env)
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "kubevirt-ui")

# ---------------------------------------------------------------------------
# Cluster-discovered config (ingress IP/class + mgmt CIDR)
# Populated lazily via _ensure_cluster_config(k8s); env overrides win.
# ---------------------------------------------------------------------------

_CLUSTER_CONFIG_TTL_SEC = 300

# {"ingress_ip": str, "ingress_class": str, "ingress_controller": str,
#  "mgmt_cidr": str | None, "fetched_at": float}
_cluster_config: dict[str, Any] | None = None


async def _ensure_cluster_config(k8s) -> dict[str, Any]:
    """Populate cluster-discovered config (ingress IP/class, mgmt CIDR).

    Idempotent + TTL-cached. Env overrides bypass discovery per-field.
    Must be awaited at the entry of any tenant handler before any sync
    getter (_endpoint_host, _ingress_class, _mgmt_cidr_drop) is called.
    """
    global _cluster_config
    now = time.monotonic()
    if _cluster_config and (now - _cluster_config["fetched_at"]) < _CLUSTER_CONFIG_TTL_SEC:
        return _cluster_config

    ip_override = os.getenv("TENANTS_INGRESS_IP")
    class_override = os.getenv("TENANTS_INGRESS_CLASS")
    cidr_override = os.getenv("TENANTS_MGMT_CIDR")

    ingress_class = class_override
    ingress_controller = "unknown"
    if not ingress_class:
        networking_api = client.NetworkingV1Api(k8s._api_client)
        classes = await networking_api.list_ingress_class()
        default = next(
            (c for c in classes.items
             if (c.metadata.annotations or {}).get(
                 "ingressclass.kubernetes.io/is-default-class") == "true"),
            None,
        )
        if default is None:
            raise RuntimeError(
                "No default IngressClass on cluster; "
                "set TENANTS_INGRESS_CLASS env var"
            )
        ingress_class = default.metadata.name
        ingress_controller = default.spec.controller or "unknown"
    else:
        # Try to also resolve the controller string for the override class
        try:
            networking_api = client.NetworkingV1Api(k8s._api_client)
            ic = await networking_api.read_ingress_class(name=ingress_class)
            ingress_controller = ic.spec.controller or "unknown"
        except ApiException:
            pass

    ingress_ip = ip_override
    if not ingress_ip:
        svcs = await k8s.core_api.list_service_for_all_namespaces()
        candidates = [
            s for s in svcs.items
            if s.spec and s.spec.type == "LoadBalancer"
            and s.status and s.status.load_balancer
            and s.status.load_balancer.ingress
        ]
        # Heuristic: pick the Service whose name OR namespace mentions the
        # ingress class. Common conventions: ingress-nginx/ingress-nginx-controller,
        # traefik/traefik, projectcontour/envoy.
        match = next(
            (s for s in candidates
             if ingress_class in s.metadata.name
             or ingress_class in (s.metadata.namespace or "")
             or "ingress" in s.metadata.name),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"No LoadBalancer Service found for ingress class {ingress_class}; "
                "set TENANTS_INGRESS_IP env var"
            )
        lb = match.status.load_balancer.ingress[0]
        ingress_ip = lb.ip or lb.hostname
        if not ingress_ip:
            raise RuntimeError(
                f"Service {match.metadata.namespace}/{match.metadata.name} has no LB IP/hostname"
            )

    mgmt_cidr = cidr_override
    if not mgmt_cidr:
        try:
            nodes = await k8s.core_api.list_node()
            ips = []
            for n in nodes.items:
                for addr in (n.status.addresses or []):
                    if addr.type == "InternalIP" and addr.address:
                        ips.append(addr.address)
            if ips:
                octets = ips[0].split(".")
                if len(octets) == 4:
                    mgmt_cidr = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
        except ApiException as e:
            logger.warning(f"Failed to autodiscover mgmt CIDR from Nodes: {e}")
            mgmt_cidr = None

    _cluster_config = {
        "ingress_ip": ingress_ip,
        "ingress_class": ingress_class,
        "ingress_controller": ingress_controller,
        "mgmt_cidr": mgmt_cidr,
        "fetched_at": now,
    }
    logger.info(
        f"Cluster config: ingress_ip={ingress_ip} class={ingress_class} "
        f"controller={ingress_controller} mgmt_cidr={mgmt_cidr}"
    )
    return _cluster_config


def _require_cluster_config() -> dict[str, Any]:
    if _cluster_config is None:
        raise RuntimeError(
            "_ensure_cluster_config(k8s) must be awaited before this call"
        )
    return _cluster_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_ns(name: str) -> str:
    return f"{TENANT_NS_PREFIX}{name}"


def _endpoint_host(name: str) -> str:
    return f"{name}.{_require_cluster_config()['ingress_ip']}.nip.io"


def _ingress_class() -> str:
    return _require_cluster_config()["ingress_class"]


def _ingress_controller() -> str:
    return _require_cluster_config()["ingress_controller"]


def _mgmt_cidr_drop() -> str | None:
    return _require_cluster_config()["mgmt_cidr"]


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
