"""Background reconciler for required tenant addons.

Periodically checks all tenant clusters and ensures required addons
(from the addon catalog) are deployed as Flux HelmRelease CRs.

Required addons are only created after:
  1. Tenant CAPI Cluster reaches Ready state
  2. Tenant kubeconfig secret exists (needed by Flux HelmRelease)
"""

import asyncio
import logging
from typing import Any

import yaml
from kubernetes_asyncio.client import ApiException

from app.core.k8s_client import K8sClient
from app.models.tenant import AddonCatalog, AddonComponent

logger = logging.getLogger(__name__)

# Constants (same as tenants.py)
TENANT_NS_PREFIX = "tenant-"
CATALOG_CONFIGMAP = "tenant-addon-catalog"
CATALOG_NAMESPACE = "flux-system"
CAPI_GROUP = "cluster.x-k8s.io"
CAPI_VERSION = "v1beta1"
FLUX_HELM_GROUP = "helm.toolkit.fluxcd.io"
FLUX_HELM_VERSION = "v2"

RECONCILE_INTERVAL = 30  # seconds


async def _get_addon_catalog(k8s: K8sClient) -> AddonCatalog:
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
            return AddonCatalog()
        raise


def _is_tenant_ready(cluster: dict[str, Any]) -> bool:
    """Check if CAPI Cluster has Ready condition = True."""
    for c in cluster.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready" and c.get("status") == "True":
            return True
    return False


async def _kubeconfig_secret_exists(k8s: K8sClient, tenant_name: str, ns: str) -> bool:
    """Check if tenant kubeconfig secret exists."""
    for secret_name in [f"{tenant_name}-kubeconfig", f"{tenant_name}-admin-kubeconfig"]:
        try:
            await k8s.core_api.read_namespaced_secret(name=secret_name, namespace=ns)
            return True
        except ApiException as e:
            if e.status != 404:
                logger.debug(f"Error checking secret {secret_name}: {e}")
    return False


async def _get_existing_addon_ids(k8s: K8sClient, tenant_name: str, ns: str) -> set[str]:
    """Get set of addon IDs that already have HelmRelease CRs."""
    try:
        result = await k8s.custom_api.list_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            label_selector=f"kubevirt-ui.io/tenant={tenant_name}",
        )
        return {
            item.get("metadata", {}).get("labels", {}).get("kubevirt-ui.io/addon", "")
            for item in result.get("items", [])
        }
    except ApiException as e:
        if e.status == 404:
            return set()
        logger.debug(f"Error listing HelmReleases for {tenant_name}: {e}")
        return set()


async def _create_required_addon(
    k8s: K8sClient,
    tenant_name: str,
    ns: str,
    component: AddonComponent,
    catalog: AddonCatalog,
    all_required: list[AddonComponent],
    existing_ids: set[str],
) -> None:
    """Create a single required addon HelmRelease for a tenant."""
    import copy

    # Build dependency chain: namespaces → CNI → everything else
    depends_on = None
    ns_addon_id = next((c.id for c in all_required if c.id == "namespaces"), None)
    cni_addon_id = next(
        (c.id for c in all_required if c.required and c.category == "networking"),
        None,
    )

    if component.id == "namespaces":
        depends_on = None
    elif component.category == "networking" and component.required:
        if ns_addon_id and ns_addon_id in existing_ids:
            depends_on = [f"{tenant_name}-{ns_addon_id}"]
    else:
        if cni_addon_id and cni_addon_id in existing_ids:
            depends_on = [f"{tenant_name}-{cni_addon_id}"]

    # Collect target namespaces for the namespaces addon
    helm_values = copy.deepcopy(component.defaultValues)
    if component.id == "namespaces":
        # Collect namespaces from ALL catalog components (not just required),
        # because optional addons created at tenant creation also need their
        # namespaces pre-created. If the namespaces HR already exists (created
        # by _create_addon_resources), this code path won't be reached anyway.
        addon_namespaces = []
        seen = set()
        for comp in catalog.components:
            if comp.namespace and comp.namespace not in seen:
                # Cross-cluster: use namespace directly (tenant cluster is independent)
                addon_namespaces.append({"name": comp.namespace})
                seen.add(comp.namespace)
        helm_values = {"namespaces": addon_namespaces}

    chart_path = f"./{catalog.base_path}/{component.chartPath}"

    spec: dict[str, Any] = {
        "interval": "30m",
        "timeout": "15m",
        "releaseName": component.id,
        "storageNamespace": "kube-system",
        "kubeConfig": {
            "secretRef": {
                "name": f"{tenant_name}-kubeconfig",
                "key": "value",
            },
        },
        "chart": {
            "spec": {
                "chart": chart_path,
                "sourceRef": {
                    "kind": "GitRepository",
                    **catalog.git_repository_ref,
                },
                "interval": "12h",
            },
        },
        "install": {
            "crds": "CreateReplace",
            "createNamespace": True,
            "remediation": {"retries": 5, "retryInterval": "30s"},
        },
        "upgrade": {
            "crds": "CreateReplace",
            "remediation": {"retries": 5, "retryInterval": "30s"},
        },
    }

    # Cross-cluster HelmRelease: use component namespace directly
    # (each tenant cluster is independent, no prefix needed)
    if component.namespace:
        spec["targetNamespace"] = component.namespace

    if helm_values:
        spec["values"] = helm_values

    if depends_on:
        spec["dependsOn"] = [{"name": d, "namespace": ns} for d in depends_on]

    hr_body = {
        "apiVersion": f"{FLUX_HELM_GROUP}/{FLUX_HELM_VERSION}",
        "kind": "HelmRelease",
        "metadata": {
            "name": f"{tenant_name}-{component.id}",
            "namespace": ns,
            "labels": {
                "kubevirt-ui.io/tenant": tenant_name,
                "kubevirt-ui.io/addon": component.id,
                "kubevirt-ui.io/reconciler-managed": "true",
            },
        },
        "spec": spec,
    }

    await k8s.custom_api.create_namespaced_custom_object(
        group=FLUX_HELM_GROUP,
        version=FLUX_HELM_VERSION,
        namespace=ns,
        plural="helmreleases",
        body=hr_body,
    )
    logger.info(f"Reconciler created HelmRelease {tenant_name}-{component.id} in {ns}")


async def _reconcile_tenant(
    k8s: K8sClient,
    cluster: dict[str, Any],
    catalog: AddonCatalog,
    required_components: list[AddonComponent],
) -> None:
    """Reconcile required addons for a single tenant."""
    name = cluster["metadata"]["name"]
    ns = cluster["metadata"]["namespace"]

    # Only reconcile Ready tenants
    if not _is_tenant_ready(cluster):
        return

    # Check kubeconfig secret exists
    if not await _kubeconfig_secret_exists(k8s, name, ns):
        return

    # Get existing addon HelmReleases
    existing_ids = await _get_existing_addon_ids(k8s, name, ns)

    # Find missing required addons
    missing = [c for c in required_components if c.id not in existing_ids]
    if not missing:
        return

    logger.info(f"Tenant {name}: missing required addons: {[c.id for c in missing]}")

    # Create missing addons in dependency order: namespaces first, then CNI, then rest
    ordered = sorted(missing, key=lambda c: (
        0 if c.id == "namespaces" else
        1 if c.category == "networking" and c.required else
        2
    ))

    for component in ordered:
        try:
            await _create_required_addon(
                k8s, name, ns, component, catalog, required_components, existing_ids,
            )
            existing_ids.add(component.id)
        except ApiException as e:
            if e.status == 409:
                logger.debug(f"HelmRelease {name}-{component.id} already exists")
                existing_ids.add(component.id)
            else:
                logger.error(f"Failed to create required addon {component.id} for {name}: {e}")


async def reconcile_loop(k8s: K8sClient) -> None:
    """Main reconciliation loop. Runs every RECONCILE_INTERVAL seconds.

    Uses exponential backoff on errors (30s → 300s max), resets on success.
    """
    logger.info(f"Tenant addon reconciler started (interval={RECONCILE_INTERVAL}s)")
    backoff = RECONCILE_INTERVAL

    try:
        while True:
            try:
                await _reconcile_once(k8s)
                backoff = RECONCILE_INTERVAL  # reset on success
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Reconciler error: {e}")
                backoff = min(backoff * 2, 300)  # max 5min

            await asyncio.sleep(backoff)
    except asyncio.CancelledError:
        logger.info("Tenant addon reconciler stopped")


async def _reconcile_once(k8s: K8sClient) -> None:
    """Run one reconciliation pass across all tenants."""
    # Load catalog
    catalog = await _get_addon_catalog(k8s)
    if not catalog.components or not catalog.git_repository_ref:
        return

    # Find required components
    required = [c for c in catalog.components if c.required]
    if not required:
        return

    # List all tenant clusters
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=CAPI_GROUP,
            version=CAPI_VERSION,
            plural="clusters",
            label_selector="kubevirt-ui.io/tenant",
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise

    for cluster in result.get("items", []):
        try:
            await _reconcile_tenant(k8s, cluster, catalog, required)
        except Exception as e:
            tenant_name = cluster.get("metadata", {}).get("name", "?")
            logger.error(f"Error reconciling tenant {tenant_name}: {e}")
