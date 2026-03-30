"""Addon management helpers for tenants.

Builds Flux HelmRelease CRs from the addon catalog and manages
addon lifecycle (create, status).
"""

import copy
import logging
from typing import Any

from kubernetes_asyncio.client import ApiException

from app.models.tenant import (
    AddonCatalog,
    AddonComponent,
    TenantAddon,
    TenantAddonStatus,
)

from app.api.v1.tenants_common import (
    FLUX_HELM_GROUP,
    FLUX_HELM_VERSION,
    _tenant_ns,
)

logger = logging.getLogger(__name__)


def _build_helm_values(
    tenant_name: str,
    component: AddonComponent,
    user_params: dict[str, str],
) -> dict[str, Any]:
    """Build Helm values by merging component defaults with user/discovery params.

    For alloy, we inject the remote_write URL and cluster label directly
    into the Alloy config template. For linstor-csi, params map to nested
    Helm values paths.
    """
    values = copy.deepcopy(component.defaultValues)

    # Handle alloy specially — inject URL into configMap content
    if component.id == "alloy":
        remote_write_url = user_params.get("VM_REMOTE_WRITE_URL", "")
        scrape_interval = user_params.get("SCRAPE_INTERVAL", "30s")
        if remote_write_url:
            alloy_config = values.get("alloy", {}).get("alloy", {}).get("configMap", {}).get("content", "")
            if alloy_config:
                alloy_config = alloy_config.replace(
                    'url = ""',
                    f'url = "{remote_write_url}"',
                )
                alloy_config = alloy_config.replace(
                    'cluster = ""',
                    f'cluster = "{tenant_name}"',
                )
                alloy_config = alloy_config.replace(
                    'scrape_interval = "30s"',
                    f'scrape_interval = "{scrape_interval}"',
                )
                values.setdefault("alloy", {}).setdefault("alloy", {}).setdefault("configMap", {})["content"] = alloy_config
        return values

    # Handle linstor-cluster — piraeus chart in CSI-only mode
    if component.id in ("linstor-cluster", "linstor-csi"):
        linstor_api = user_params.get("LINSTOR_API_URL", "")
        storage_pool = user_params.get("STORAGE_POOL", "")
        replica_count = user_params.get("REPLICA_COUNT", "2")
        lc = values.setdefault("linstor-cluster", {})
        if linstor_api:
            lc.setdefault("linstorCluster", {}).setdefault(
                "externalController", {}
            )["url"] = linstor_api
        sc_list = lc.get("storageClasses", [{}])
        if sc_list:
            sc_params = sc_list[0].setdefault("parameters", {})
            if storage_pool:
                sc_params["linstor.csi.linbit.com/storagePool"] = storage_pool
            sc_params["linstor.csi.linbit.com/autoPlace"] = replica_count
        return values

    # Generic: no special handling needed (e.g. calico uses defaults as-is)
    return values


def _build_flux_helmrelease_cr(
    tenant_name: str,
    addon_id: str,
    component: AddonComponent,
    catalog: AddonCatalog,
    helm_values: dict[str, Any],
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    ns = _tenant_ns(tenant_name)
    chart_path = f"./{catalog.base_path}/{component.chartPath}"

    spec: dict[str, Any] = {
        "interval": "30m",
        "timeout": "15m",
        "releaseName": addon_id,
        "storageNamespace": "kube-system",
        "kubeConfig": {
            "secretRef": {
                "name": f"{tenant_name}-admin-kubeconfig",
                "key": "super-admin.svc",
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
            "remediation": {"retries": 5},
        },
        "upgrade": {
            "crds": "CreateReplace",
            "remediation": {"retries": 5},
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

    return {
        "apiVersion": f"{FLUX_HELM_GROUP}/{FLUX_HELM_VERSION}",
        "kind": "HelmRelease",
        "metadata": {
            "name": f"{tenant_name}-{addon_id}",
            "namespace": ns,
            "labels": {
                "kubevirt-ui.io/tenant": tenant_name,
                "kubevirt-ui.io/addon": addon_id,
            },
        },
        "spec": spec,
    }


async def _create_addon_resources(
    k8s, tenant_name: str, addons: list[TenantAddon], catalog: AddonCatalog,
) -> None:
    """Create Flux HelmRelease CRs for selected addons.

    Dependency chain: namespaces → calico (CNI) → everything else.
    The namespaces addon auto-collects target namespaces from all enabled addons.
    """
    custom = k8s.custom_api
    ns = _tenant_ns(tenant_name)

    # Identify special addon IDs
    ns_addon_id: str | None = None
    cni_addon_id: str | None = None
    for component in catalog.components:
        if component.id == "namespaces":
            ns_addon_id = component.id
        if component.required and component.category == "networking":
            cni_addon_id = component.id

    # Collect target namespaces from all enabled addons (for the namespaces chart)
    # Cross-cluster: use namespace directly (tenant cluster is independent)
    addon_namespaces: list[dict[str, str]] = []
    for addon in addons:
        comp = catalog.get_component(addon.addon_id)
        if comp and comp.namespace:
            addon_namespaces.append({"name": comp.namespace})

    for addon in addons:
        component = catalog.get_component(addon.addon_id)
        if not component:
            logger.warning(f"Addon {addon.addon_id} not found in catalog, skipping")
            continue

        # Merge defaults with user-provided params
        params: dict[str, str] = {}
        for p in component.parameters:
            params[p.id] = addon.parameters.get(p.id, p.default)

        # Build Helm values from component defaults + user params
        helm_values = _build_helm_values(tenant_name, component, params)

        # Special: inject collected namespaces into the namespaces addon
        if component.id == "namespaces":
            helm_values = {"namespaces": addon_namespaces}

        # Dependency chain: namespaces → CNI → everything else
        depends_on = None
        if component.id == "namespaces":
            depends_on = None  # namespaces has no dependencies
        elif component.category == "networking" and component.required:
            # CNI depends on namespaces
            if ns_addon_id:
                depends_on = [f"{tenant_name}-{ns_addon_id}"]
        else:
            # Everything else depends on CNI (which transitively depends on namespaces)
            if cni_addon_id:
                depends_on = [f"{tenant_name}-{cni_addon_id}"]

        # Create Flux HelmRelease CR
        hr_body = _build_flux_helmrelease_cr(
            tenant_name=tenant_name,
            addon_id=addon.addon_id,
            component=component,
            catalog=catalog,
            helm_values=helm_values,
            depends_on=depends_on,
        )
        await custom.create_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            body=hr_body,
        )


async def _get_addon_statuses(k8s, tenant_name: str) -> list[TenantAddonStatus]:
    """List Flux HelmRelease statuses for a tenant."""
    ns = _tenant_ns(tenant_name)
    try:
        result = await k8s.custom_api.list_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            label_selector=f"kubevirt-ui.io/tenant={tenant_name}",
        )
        statuses = []
        for item in result.get("items", []):
            addon_id = item.get("metadata", {}).get("labels", {}).get(
                "kubevirt-ui.io/addon", ""
            )
            conditions = item.get("status", {}).get("conditions", [])
            ready = False
            message = None
            for c in conditions:
                if c.get("type") == "Ready":
                    ready = c.get("status") == "True"
                    message = c.get("message")
                    break

            last_reconcile = item.get("status", {}).get(
                "lastAppliedRevision"
            ) or item.get("status", {}).get("lastAttemptedRevision")

            statuses.append(TenantAddonStatus(
                addon_id=addon_id,
                name=item.get("metadata", {}).get("name", ""),
                ready=ready,
                last_reconcile=last_reconcile,
                message=message,
            ))
        return statuses
    except ApiException as e:
        if e.status == 404:
            return []
        logger.debug(f"Could not fetch Flux HelmReleases for {tenant_name}: {e}")
        return []
