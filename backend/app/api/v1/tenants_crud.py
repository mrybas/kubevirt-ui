"""Tenant management API endpoints.

Creates CAPI resources (Cluster, KamajiControlPlane, MachineDeployment, etc.)
and Flux HelmRelease CRs per addon per tenant. Addon catalog read from ConfigMap.
"""

import base64
import logging
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from kubernetes_asyncio.client import ApiException

from app.core.auth import User, require_auth
from app.core.errors import k8s_error_to_http
from app.models.tenant import (
    AddonCatalog,
    DiscoveryResponse,
    LoggingDiscovery,
    MonitoringDiscovery,
    RegistryDiscovery,
    StorageDiscovery,
    StoragePoolInfo,
    TenantAddon,
    TenantAddonStatus,
    TenantCondition,
    TenantCreateRequest,
    TenantKubeconfigResponse,
    TenantListResponse,
    TenantResponse,
    TenantScaleRequest,
)
from app.models.template import GoldenImage, GoldenImageCreate, GoldenImageListResponse

from app.api.v1.tenants_common import (
    CAPI_GROUP,
    CAPI_VERSION,
    FLUX_HELM_GROUP,
    FLUX_HELM_VERSION,
    KUBEVIRT_INFRA_GROUP,
    KUBEVIRT_INFRA_VERSION,
    OIDC_ISSUER,
    OIDC_CLIENT_ID,
    _tenant_ns,
    _endpoint_host,
    _get_addon_catalog,
    _namespace_exists,
    _create_namespace,
)
from app.api.v1.tenants_vpc import _create_tenant_vpc, _delete_tenant_vpc
from app.api.v1.tenants_capi import _create_capi_resources
from app.api.v1.tenants_addons import (
    _build_helm_values,
    _build_flux_helmrelease_cr,
    _create_addon_resources,
    _get_addon_statuses,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parse tenant status from CAPI Cluster CR
# ---------------------------------------------------------------------------

def _parse_tenant_response(
    cluster: dict[str, Any],
    addon_statuses: list[TenantAddonStatus] | None = None,
) -> TenantResponse:
    metadata = cluster.get("metadata", {})
    spec = cluster.get("spec", {})
    cluster_status = cluster.get("status", {})
    name = metadata.get("name", "")

    # Parse phase
    phase = cluster_status.get("phase")

    # Check Ready condition from conditions list (authoritative source)
    ready_condition_true = False
    for c in cluster_status.get("conditions", []):
        if c.get("type") == "Ready" and c.get("status") == "True":
            ready_condition_true = True
            break

    # Determine status: Ready condition takes priority over phase
    if phase == "Deleting":
        status_str = "Deleting"
    elif phase == "Failed":
        status_str = "Failed"
    elif ready_condition_true:
        status_str = "Ready"
    elif phase in ("Provisioning", "Provisioned", "Pending"):
        status_str = "Provisioning"
    else:
        status_str = phase or "Unknown"

    # Control plane
    cp_ready = cluster_status.get("controlPlaneReady", False)

    # Worker count from MachineDeployment (will be enriched later)
    infra_ready = cluster_status.get("infrastructureReady", False)

    # Parse conditions
    conditions = []
    for c in cluster_status.get("conditions", []):
        conditions.append(TenantCondition(
            type=c.get("type", ""),
            status=c.get("status", "Unknown"),
            message=c.get("message", ""),
            reason=c.get("reason", ""),
            last_transition_time=c.get("lastTransitionTime"),
        ))

    # Network
    cluster_network = spec.get("clusterNetwork", {})
    pod_cidrs = cluster_network.get("pods", {}).get("cidrBlocks", [])
    svc_cidrs = cluster_network.get("services", {}).get("cidrBlocks", [])

    return TenantResponse(
        name=name,
        display_name=metadata.get("annotations", {}).get("kubevirt-ui.io/display-name", name),
        namespace=metadata.get("namespace", ""),
        kubernetes_version=cluster_status.get("version", ""),
        status=status_str,
        phase=phase,
        endpoint=f"https://{_endpoint_host(name)}",
        control_plane_replicas=0,
        control_plane_ready=cp_ready,
        worker_type=metadata.get("annotations", {}).get("kubevirt-ui.io/worker-type", "vm"),
        worker_count=0,
        workers_ready=0,
        worker_vcpu=0,
        worker_memory="",
        pod_cidr=pod_cidrs[0] if pod_cidrs else "",
        service_cidr=svc_cidrs[0] if svc_cidrs else "",
        created=metadata.get("creationTimestamp"),
        conditions=conditions,
        addons=addon_statuses or [],
    )


async def _enrich_with_workers(
    k8s, tenant: TenantResponse,
) -> TenantResponse:
    """Add worker count info from MachineDeployment."""
    try:
        result = await k8s.custom_api.list_namespaced_custom_object(
            group=CAPI_GROUP,
            version=CAPI_VERSION,
            namespace=tenant.namespace,
            plural="machinedeployments",
        )
        for md in result.get("items", []):
            md_status = md.get("status", {})
            md_spec = md.get("spec", {})
            tenant.worker_count = md_spec.get("replicas", 0)
            tenant.workers_ready = md_status.get("readyReplicas", 0)

            # Extract VM spec from infrastructure template
            infra_ref = md_spec.get("template", {}).get("spec", {}).get("infrastructureRef", {})
            if infra_ref.get("kind") == "KubevirtMachineTemplate":
                try:
                    tpl = await k8s.custom_api.get_namespaced_custom_object(
                        group=KUBEVIRT_INFRA_GROUP,
                        version=KUBEVIRT_INFRA_VERSION,
                        namespace=tenant.namespace,
                        plural="kubevirtmachinetemplates",
                        name=infra_ref.get("name", ""),
                    )
                    vm_spec = (
                        tpl.get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("virtualMachineTemplate", {})
                        .get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("domain", {})
                    )
                    tenant.worker_vcpu = vm_spec.get("cpu", {}).get("cores", 0)
                    tenant.worker_memory = vm_spec.get("memory", {}).get("guest", "")
                except ApiException:
                    pass
            break  # only first MachineDeployment
    except ApiException as e:
        logger.debug(f"Could not fetch MachineDeployments for {tenant.name}: {e}")

    return tenant


# ---------------------------------------------------------------------------
# Host cluster discovery
# ---------------------------------------------------------------------------

LINSTOR_API_GROUP = "internal.linstor.linbit.com"
LINSTOR_API_VERSION = "v1"

# Known service selectors / names for auto-discovery
_DISCOVERY_PATTERNS = {
    "linstor": {
        "label_selectors": [
            "app.kubernetes.io/name=linstor-controller",
            "app=linstor-controller",
        ],
        "service_names": ["linstor-controller"],
    },
    "victoria-metrics": {
        "label_selectors": [
            "app=vminsert",
            "app.kubernetes.io/name=victoria-metrics-cluster-vminsert",
            "app.kubernetes.io/name=vminsert",
            "app.kubernetes.io/name=vmsingle",
        ],
        "service_names": ["vminsert", "vminsert-cluster", "vmsingle-vmsingle"],
    },
    "loki": {
        "label_selectors": [
            "app=loki",
            "app.kubernetes.io/name=loki",
        ],
        "service_names": ["loki", "loki-gateway", "loki-write"],
    },
    "harbor": {
        "label_selectors": [
            "app=harbor",
            "app.kubernetes.io/name=harbor",
        ],
        "service_names": ["harbor-core"],
    },
}


def _svc_url(svc: Any, port_name: str | None = None) -> str:
    """Build internal service URL from a V1Service object."""
    ns = svc.metadata.namespace
    name = svc.metadata.name
    port = None
    if svc.spec.ports:
        if port_name:
            for p in svc.spec.ports:
                if p.name == port_name:
                    port = p.port
                    break
        if port is None:
            port = svc.spec.ports[0].port
    return f"http://{name}.{ns}.svc:{port}"


async def _find_service(core_api, patterns: dict) -> Any | None:
    """Find a service by label selectors or name across all namespaces."""
    for selector in patterns.get("label_selectors", []):
        try:
            result = await core_api.list_service_for_all_namespaces(
                label_selector=selector,
            )
            if result.items:
                return result.items[0]
        except ApiException:
            pass

    for svc_name in patterns.get("service_names", []):
        try:
            result = await core_api.list_service_for_all_namespaces(
                field_selector=f"metadata.name={svc_name}",
            )
            if result.items:
                return result.items[0]
        except ApiException:
            pass

    return None


async def _discover_linstor(k8s) -> StorageDiscovery | None:
    """Discover Linstor controller and query storage pools via its API."""
    svc = await _find_service(k8s.core_api, _DISCOVERY_PATTERNS["linstor"])
    if not svc:
        return None

    api_url = _svc_url(svc)
    pools: list[StoragePoolInfo] = []

    # Query Linstor REST API via K8s API server proxy (avoids cluster DNS issues)
    svc_ns = svc.metadata.namespace
    svc_name = svc.metadata.name
    svc_port = svc.spec.ports[0].port if svc.spec.ports else 3370
    try:
        raw = await k8s.core_api.connect_get_namespaced_service_proxy_with_path(
            name=f"{svc_name}:{svc_port}",
            namespace=svc_ns,
            path="v1/view/storage-pools",
        )
        import json as _json
        data = _json.loads(raw.replace("'", '"')) if isinstance(raw, str) else raw
        # Aggregate pools by name
        pool_agg: dict[str, dict] = {}
        for sp in data:
            pname = sp.get("storage_pool_name", "")
            if pname in ("DfltDisklessStorPool",):
                continue
            if pname not in pool_agg:
                pool_agg[pname] = {
                    "driver": sp.get("provider_kind", ""),
                    "free": 0,
                    "total": 0,
                    "nodes": 0,
                }
            free_cap = sp.get("free_capacity", 0) or 0
            total_cap = sp.get("total_capacity", 0) or 0
            pool_agg[pname]["free"] += free_cap
            pool_agg[pname]["total"] += total_cap
            pool_agg[pname]["nodes"] += 1

        for pname, info in pool_agg.items():
            pools.append(StoragePoolInfo(
                name=pname,
                driver=info["driver"],
                free_gb=round(info["free"] / (1024 ** 2), 1),  # KiB → GiB
                total_gb=round(info["total"] / (1024 ** 2), 1),
                node_count=info["nodes"],
            ))
    except Exception as e:
        logger.debug(f"Could not query Linstor API via proxy: {e}")

    return StorageDiscovery(type="linstor", api_url=api_url, pools=pools)


async def _discover_victoria_metrics(k8s) -> MonitoringDiscovery | None:
    """Discover VictoriaMetrics vminsert or vmsingle service for remote_write."""
    svc = await _find_service(k8s.core_api, _DISCOVERY_PATTERNS["victoria-metrics"])
    if not svc:
        return None

    svc_name = svc.metadata.name
    is_single = "vmsingle" in svc_name

    write_url = _svc_url(svc)
    if is_single:
        # vmsingle: unified endpoint, Prometheus-compatible write
        write_url = f"{write_url}/api/v1/write"
    else:
        # vminsert (cluster mode): standard insert path
        write_url = f"{write_url}/insert/0/prometheus/api/v1/write"

    # Try to find query URL
    query_url = ""
    if is_single:
        # vmsingle serves both read and write on the same service
        query_url = _svc_url(svc)
    else:
        try:
            for selector in [
                "app=vmselect",
                "app.kubernetes.io/name=victoria-metrics-cluster-vmselect",
            ]:
                result = await k8s.core_api.list_service_for_all_namespaces(
                    label_selector=selector,
                )
                if result.items:
                    query_url = f"{_svc_url(result.items[0])}/select/0/prometheus"
                    break
        except ApiException:
            pass

    return MonitoringDiscovery(
        type="victoria-metrics", write_url=write_url, query_url=query_url,
    )


async def _discover_loki(k8s) -> LoggingDiscovery | None:
    """Discover Loki push endpoint."""
    svc = await _find_service(k8s.core_api, _DISCOVERY_PATTERNS["loki"])
    if not svc:
        return None

    push_url = f"{_svc_url(svc)}/loki/api/v1/push"
    return LoggingDiscovery(type="loki", push_url=push_url)


async def _discover_registry(k8s) -> RegistryDiscovery | None:
    """Discover Harbor or generic registry."""
    svc = await _find_service(k8s.core_api, _DISCOVERY_PATTERNS["harbor"])
    if not svc:
        return None

    url = _svc_url(svc)
    return RegistryDiscovery(type="harbor", url=url)


# ===========================================================================
# Endpoints
# ===========================================================================

@router.get("/discovery", response_model=DiscoveryResponse)
async def discover_host_infrastructure(request: Request, user: User = Depends(require_auth)) -> DiscoveryResponse:
    """Auto-discover storage, monitoring, logging, registry from host cluster."""
    k8s = request.app.state.k8s_client

    storage: list[StorageDiscovery] = []
    monitoring: list[MonitoringDiscovery] = []
    logging_list: list[LoggingDiscovery] = []
    registry: list[RegistryDiscovery] = []

    # Run all discovery in parallel-ish
    linstor = await _discover_linstor(k8s)
    if linstor:
        storage.append(linstor)

    vm = await _discover_victoria_metrics(k8s)
    if vm:
        monitoring.append(vm)

    loki = await _discover_loki(k8s)
    if loki:
        logging_list.append(loki)

    reg = await _discover_registry(k8s)
    if reg:
        registry.append(reg)

    return DiscoveryResponse(
        storage=storage,
        monitoring=monitoring,
        logging=logging_list,
        registry=registry,
    )


@router.get("/catalog")
async def get_addon_catalog(request: Request, user: User = Depends(require_auth)) -> dict[str, Any]:
    """Get addon catalog for tenant wizard."""
    k8s = request.app.state.k8s_client
    catalog = await _get_addon_catalog(k8s)
    return catalog.model_dump()


@router.get("", response_model=TenantListResponse)
async def list_tenants(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user: User = Depends(require_auth),
) -> TenantListResponse:
    """List all tenants (CAPI Clusters in tenant-* namespaces)."""
    k8s = request.app.state.k8s_client

    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=CAPI_GROUP,
            version=CAPI_VERSION,
            plural="clusters",
            label_selector="kubevirt-ui.io/tenant",
        )

        items = []
        for cluster in result.get("items", []):
            tenant = _parse_tenant_response(cluster)
            tenant = await _enrich_with_workers(k8s, tenant)
            items.append(tenant)

        total = len(items)
        start = (page - 1) * per_page
        paginated = items[start:start + per_page]
        return TenantListResponse(
            items=paginated,
            total=total,
            page=page,
            per_page=per_page,
            pages=(total + per_page - 1) // per_page,
        )

    except ApiException as e:
        if e.status == 404:
            return TenantListResponse(items=[], total=0)
        logger.error(f"Failed to list tenants: {e}")
        raise k8s_error_to_http(e, "tenant operation")


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(request: Request, req: TenantCreateRequest, user: User = Depends(require_auth)) -> TenantResponse:
    """Create a new tenant cluster."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(req.name)

    # Check if already exists
    if await _namespace_exists(k8s, ns):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant '{req.name}' already exists",
        )

    try:
        # 1. Create namespace
        await _create_namespace(k8s, ns, req.name, req.worker_type)

        # 2. Create VPC BEFORE CAPI resources (NAD must exist when TCP pod starts)
        vpc_info: dict[str, str] | None = None
        if req.network_isolation:
            vpc_info = await _create_tenant_vpc(k8s, req.name)

        # 3. Create CAPI resources + Ingress (passes vpc_info for Multus annotation)
        await _create_capi_resources(k8s, req, vpc_info)

        # 4. Create addon resources (Flux HelmRelease CRs)
        catalog = await _get_addon_catalog(k8s)

        # Auto-add required addons
        addon_ids = {a.addon_id for a in req.addons}
        all_addons = list(req.addons)
        for component in catalog.components:
            if component.required and component.id not in addon_ids:
                all_addons.insert(0, TenantAddon(addon_id=component.id))

        if all_addons and catalog.git_repository_ref:
            await _create_addon_resources(k8s, req.name, all_addons, catalog)

        # Return initial status
        return TenantResponse(
            name=req.name,
            display_name=req.display_name,
            namespace=ns,
            kubernetes_version=req.kubernetes_version,
            status="Provisioning",
            phase="Pending",
            endpoint=f"https://{_endpoint_host(req.name)}",
            control_plane_replicas=req.control_plane_replicas,
            control_plane_ready=False,
            worker_type=req.worker_type,
            worker_count=req.worker_count,
            workers_ready=0,
            worker_vcpu=req.worker_vcpu,
            worker_memory=req.worker_memory,
            pod_cidr=req.pod_cidr,
            service_cidr=req.service_cidr,
            addons=[
                TenantAddonStatus(addon_id=a.addon_id, name=f"{req.name}-{a.addon_id}")
                for a in all_addons
            ],
        )

    except (ApiException, RuntimeError) as e:
        logger.error(f"Failed to create tenant {req.name}: {e}")
        # Cleanup: try to delete cluster-scoped resources + namespace (cascade)
        try:
            await _delete_tenant_vpc(k8s, req.name)
        except Exception:
            pass
        try:
            await k8s.core_api.delete_namespace(name=ns)
        except Exception:
            pass
        logger.error(f"Failed to create tenant: {e}")
        raise HTTPException(status_code=500, detail="Failed to create tenant")


@router.get("/{name}", response_model=TenantResponse)
async def get_tenant(request: Request, name: str, user: User = Depends(require_auth)) -> TenantResponse:
    """Get tenant details."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    try:
        cluster = await k8s.custom_api.get_namespaced_custom_object(
            group=CAPI_GROUP,
            version=CAPI_VERSION,
            namespace=ns,
            plural="clusters",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Tenant '{name}' not found")
        raise k8s_error_to_http(e, "tenant operation")

    addon_statuses = await _get_addon_statuses(k8s, name)
    tenant = _parse_tenant_response(cluster, addon_statuses)
    tenant = await _enrich_with_workers(k8s, tenant)
    return tenant


@router.delete("/{name}", status_code=204)
async def delete_tenant(request: Request, name: str, user: User = Depends(require_auth)) -> None:
    """Delete a tenant (delete namespace → cascade delete everything)."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    if not await _namespace_exists(k8s, ns):
        raise HTTPException(status_code=404, detail=f"Tenant '{name}' not found")

    # Clean up cluster-scoped resources (not cascade-deleted with namespace)
    try:
        await _delete_tenant_vpc(k8s, name)
    except Exception as exc:
        logger.warning(f"Failed to clean up VPC for tenant {name}: {exc}")

    try:
        await k8s.core_api.delete_namespace(name=ns)
    except ApiException as e:
        logger.error(f"Failed to delete tenant {name}: {e}")
        raise k8s_error_to_http(e, "tenant operation")


@router.post("/{name}/scale", response_model=TenantResponse)
async def scale_tenant(
    request: Request, name: str, scale: TenantScaleRequest,
    user: User = Depends(require_auth),
) -> TenantResponse:
    """Scale tenant worker nodes."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    try:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=CAPI_GROUP,
            version=CAPI_VERSION,
            namespace=ns,
            plural="machinedeployments",
            name=f"{name}-workers",
            body={"spec": {"replicas": scale.worker_count}},
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Tenant '{name}' not found")
        raise k8s_error_to_http(e, "tenant operation")

    return await get_tenant(request, name)


@router.get("/{name}/kubeconfig", response_model=TenantKubeconfigResponse)
async def get_tenant_kubeconfig(
    request: Request,
    name: str,
    type: str = "admin",  # "admin" or "oidc"
    user: User = Depends(require_auth),
) -> TenantKubeconfigResponse:
    """Get tenant kubeconfig.

    type=admin  → certificate-based admin kubeconfig (from Kamaji secret)
    type=oidc   → OIDC kubeconfig for end-users (uses current user's token or exec plugin)
    """
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    # Try CAPI kubeconfig first ({name}-kubeconfig, key: value), fall back to
    # Kamaji admin-kubeconfig ({name}-admin-kubeconfig, key: super-admin.conf)
    secret = None
    for secret_name in [f"{name}-kubeconfig", f"{name}-admin-kubeconfig"]:
        try:
            secret = await k8s.core_api.read_namespaced_secret(
                name=secret_name, namespace=ns,
            )
            break
        except ApiException as e:
            if e.status != 404:
                raise k8s_error_to_http(e, "tenant operation")

    if secret is None:
        raise HTTPException(
            status_code=404,
            detail=f"Kubeconfig not ready yet for tenant '{name}'",
        )

    kubeconfig_data = secret.data or {}

    if type == "admin":
        # Return admin kubeconfig with external endpoint
        raw = kubeconfig_data.get("admin.conf") or kubeconfig_data.get(
            "super-admin.conf"
        ) or kubeconfig_data.get("value", "")

        if not raw:
            raise HTTPException(
                status_code=404, detail="Kubeconfig data not found in secret"
            )

        kubeconfig_str = base64.b64decode(raw).decode("utf-8")
        # Replace internal server URL with external ingress endpoint
        admin_kc = yaml.safe_load(kubeconfig_str)
        external_server = f"https://{_endpoint_host(name)}"
        for cluster in admin_kc.get("clusters", []):
            cluster.setdefault("cluster", {})["server"] = external_server
        kubeconfig_str = yaml.dump(admin_kc, default_flow_style=False)
        return TenantKubeconfigResponse(kubeconfig=kubeconfig_str)

    # type == "oidc"
    if not OIDC_ISSUER:
        raise HTTPException(
            status_code=400,
            detail="OIDC not configured on this cluster. Set OIDC_ISSUER env var.",
        )

    # Extract CA and server from admin kubeconfig
    raw_admin = kubeconfig_data.get("admin.conf") or kubeconfig_data.get(
        "super-admin.conf"
    ) or kubeconfig_data.get("value", "")
    if not raw_admin:
        raise HTTPException(
            status_code=404, detail="Admin kubeconfig not found in secret"
        )

    admin_kc = yaml.safe_load(base64.b64decode(raw_admin).decode("utf-8"))
    clusters = admin_kc.get("clusters", [])
    ca_data = ""
    server = f"https://{_endpoint_host(name)}"
    if clusters:
        cluster_info = clusters[0].get("cluster", {})
        ca_data = cluster_info.get("certificate-authority-data", "")
        # Use the external endpoint, not the internal one from kubeconfig
        # server is already set above

    # Try to get user's current token from Authorization header
    user_token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        user_token = auth_header[7:]

    # Build OIDC kubeconfig
    if user_token:
        # Token-based: embed current user's OIDC token directly
        oidc_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{
                "name": name,
                "cluster": {
                    "server": server,
                    "certificate-authority-data": ca_data,
                },
            }],
            "contexts": [{
                "name": name,
                "context": {
                    "cluster": name,
                    "user": "oidc-user",
                },
            }],
            "current-context": name,
            "users": [{
                "name": "oidc-user",
                "user": {
                    "token": user_token,
                },
            }],
        }
    else:
        # Exec-based: use kubelogin/oidc-login plugin
        oidc_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{
                "name": name,
                "cluster": {
                    "server": server,
                    "certificate-authority-data": ca_data,
                },
            }],
            "contexts": [{
                "name": name,
                "context": {
                    "cluster": name,
                    "user": "oidc-user",
                },
            }],
            "current-context": name,
            "users": [{
                "name": "oidc-user",
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "kubectl",
                        "args": [
                            "oidc-login",
                            "get-token",
                            f"--oidc-issuer-url={OIDC_ISSUER}",
                            f"--oidc-client-id={OIDC_CLIENT_ID}",
                            "--oidc-extra-scope=email",
                            "--oidc-extra-scope=groups",
                            "--oidc-extra-scope=openid",
                        ],
                    },
                },
            }],
        }

    kubeconfig_str = yaml.dump(oidc_kubeconfig, default_flow_style=False)
    return TenantKubeconfigResponse(kubeconfig=kubeconfig_str)


# ---------------------------------------------------------------------------
# Addon management (post-creation enable/disable/update)
# ---------------------------------------------------------------------------

@router.post("/{name}/addons", status_code=201)
async def enable_addon(
    request: Request, name: str, addon: TenantAddon,
    user: User = Depends(require_auth),
) -> TenantAddonStatus:
    """Enable an addon for a tenant."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)
    catalog = await _get_addon_catalog(k8s)

    component = catalog.get_component(addon.addon_id)
    if not component:
        raise HTTPException(status_code=404, detail=f"Addon '{addon.addon_id}' not in catalog")

    # Merge defaults with user params
    params: dict[str, str] = {}
    for p in component.parameters:
        params[p.id] = addon.parameters.get(p.id, p.default)

    try:
        # If new addon has a target namespace, patch the namespaces HelmRelease
        if component.namespace:
            # Prefix namespace with tenant name for non-core addons
            target_ns = (
                f"{name}-{component.namespace}"
                if component.category != "core"
                else component.namespace
            )
            ns_hr_name = f"{name}-namespaces"
            try:
                ns_hr = await k8s.custom_api.get_namespaced_custom_object(
                    group=FLUX_HELM_GROUP, version=FLUX_HELM_VERSION,
                    namespace=ns, plural="helmreleases", name=ns_hr_name,
                )
                existing_ns = ns_hr.get("spec", {}).get("values", {}).get("namespaces", [])
                ns_names = {n["name"] for n in existing_ns}
                if target_ns not in ns_names:
                    existing_ns.append({"name": target_ns})
                    await k8s.custom_api.patch_namespaced_custom_object(
                        group=FLUX_HELM_GROUP, version=FLUX_HELM_VERSION,
                        namespace=ns, plural="helmreleases", name=ns_hr_name,
                        body={"spec": {"values": {"namespaces": existing_ns}}},
                    )
            except ApiException:
                pass  # namespaces HR not found — skip

        # Build Helm values and create HelmRelease
        helm_values = _build_helm_values(name, component, params)

        # dependsOn: everything depends on CNI
        cni_addon_id = None
        for c in catalog.components:
            if c.required and c.category == "networking":
                cni_addon_id = c.id
                break

        depends_on = None
        if cni_addon_id and addon.addon_id != cni_addon_id:
            depends_on = [f"{name}-{cni_addon_id}"]

        hr_body = _build_flux_helmrelease_cr(
            tenant_name=name,
            addon_id=addon.addon_id,
            component=component,
            catalog=catalog,
            helm_values=helm_values,
            depends_on=depends_on,
        )
        await k8s.custom_api.create_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            body=hr_body,
        )

        return TenantAddonStatus(
            addon_id=addon.addon_id,
            name=f"{name}-{addon.addon_id}",
            ready=False,
            message="Created, waiting for reconciliation",
        )

    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409,
                detail=f"Addon '{addon.addon_id}' already enabled",
            )
        raise k8s_error_to_http(e, "tenant operation")


@router.delete("/{name}/addons/{addon_id}", status_code=204)
async def disable_addon(request: Request, name: str, addon_id: str, user: User = Depends(require_auth)) -> None:
    """Disable an addon for a tenant."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    # Check it's not required
    catalog = await _get_addon_catalog(k8s)
    component = catalog.get_component(addon_id)
    if component and component.required:
        raise HTTPException(
            status_code=400, detail=f"Cannot disable required addon '{addon_id}'"
        )

    # Delete Flux HelmRelease
    try:
        await k8s.custom_api.delete_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            name=f"{name}-{addon_id}",
        )
    except ApiException as e:
        if e.status != 404:
            raise k8s_error_to_http(e, "tenant operation")



@router.patch("/{name}/addons/{addon_id}")
async def update_addon_params(
    request: Request, name: str, addon_id: str, params: dict[str, str],
    user: User = Depends(require_auth),
) -> TenantAddonStatus:
    """Update addon parameters (patch HelmRelease values)."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)
    catalog = await _get_addon_catalog(k8s)

    component = catalog.get_component(addon_id)
    if not component:
        raise HTTPException(status_code=404, detail=f"Addon '{addon_id}' not in catalog")

    hr_name = f"{name}-{addon_id}"
    try:
        hr = await k8s.custom_api.get_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            name=hr_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"HelmRelease '{hr_name}' not found")
        raise k8s_error_to_http(e, "tenant operation")

    # Rebuild values with updated params
    helm_values = _build_helm_values(name, component, params)
    patch = {"spec": {"values": helm_values}}

    try:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=FLUX_HELM_GROUP,
            version=FLUX_HELM_VERSION,
            namespace=ns,
            plural="helmreleases",
            name=hr_name,
            body=patch,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "tenant operation")

    # Return current addon status
    statuses = await _get_addon_statuses(k8s, name)
    for s in statuses:
        if s.addon_id == addon_id:
            return s

    return TenantAddonStatus(
        addon_id=addon_id, name=f"{name}-{addon_id}", message="Updated"
    )


# ---------------------------------------------------------------------------
# Tenant images (DataVolumes in tenant namespace)
# ---------------------------------------------------------------------------

def _parse_tenant_image_dv(dv: dict[str, Any]) -> GoldenImage:
    """Parse a CDI DataVolume into a GoldenImage response."""
    metadata = dv.get("metadata", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    spec = dv.get("spec", {})
    dv_status = dv.get("status", {})

    # Determine source URL
    source = spec.get("source", {})
    source_url = None
    if "http" in source:
        source_url = source["http"].get("url")
    elif "registry" in source:
        source_url = source["registry"].get("url")
    elif "pvc" in source:
        pvc = source["pvc"]
        source_url = f"pvc:{pvc.get('namespace', '')}/{pvc.get('name', '')}"

    # Status
    phase = dv_status.get("phase", "Pending")
    error_message = None
    if phase == "Succeeded":
        display_status = "Ready"
    elif phase == "Failed":
        display_status = "Error"
        for cond in dv_status.get("conditions", []):
            if cond.get("type") == "Running" and cond.get("status") == "False":
                error_message = cond.get("message")
                break
    else:
        display_status = phase

    # Size
    size = spec.get("storage", {}).get("resources", {}).get("requests", {}).get("storage")

    return GoldenImage(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace", ""),
        display_name=annotations.get("kubevirt-ui.io/display-name", metadata.get("name", "")),
        os_type=labels.get("kubevirt-ui.io/os-type"),
        size=size,
        status=display_status,
        error_message=error_message,
        source_url=source_url,
        created=metadata.get("creationTimestamp"),
    )


@router.get("/{name}/images", response_model=GoldenImageListResponse)
async def list_tenant_images(request: Request, name: str, user: User = Depends(require_auth)) -> GoldenImageListResponse:
    """List images (DataVolumes) in a tenant namespace."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    if not await _namespace_exists(k8s, ns):
        raise HTTPException(status_code=404, detail=f"Tenant '{name}' not found")

    try:
        result = await k8s.custom_api.list_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=ns,
            plural="datavolumes",
            label_selector="kubevirt-ui.io/tenant-image=true",
        )
        images = [_parse_tenant_image_dv(dv) for dv in result.get("items", [])]
        return GoldenImageListResponse(items=images, total=len(images))
    except ApiException as e:
        raise k8s_error_to_http(e, "tenant operation")


@router.post("/{name}/images", response_model=GoldenImage, status_code=status.HTTP_201_CREATED)
async def create_tenant_image(
    request: Request, name: str, image: GoldenImageCreate,
    user: User = Depends(require_auth),
) -> GoldenImage:
    """Import a new image into a tenant namespace."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    if not await _namespace_exists(k8s, ns):
        raise HTTPException(status_code=404, detail=f"Tenant '{name}' not found")

    # Resolve name
    if not image.name and not image.display_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'name' or 'display_name' must be provided",
        )
    if not image.name:
        # Simple slug from display_name
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", (image.display_name or "").lower()).strip("-")[:50]
        image.name = slug or "image"

    # Determine source
    if image.source_url:
        source = {"http": {"url": image.source_url}}
    elif image.source_registry:
        source = {"registry": {"url": image.source_registry}}
    elif image.source_pvc:
        pvc_ns = image.source_pvc_namespace or ns
        source = {"pvc": {"name": image.source_pvc, "namespace": pvc_ns}}
    else:
        source = {"blank": {}}

    dv_labels: dict[str, str] = {
        "kubevirt-ui.io/managed": "true",
        "kubevirt-ui.io/tenant-image": "true",
        "kubevirt-ui.io/tenant": name,
    }
    dv_annotations: dict[str, str] = {}
    if image.os_type:
        dv_labels["kubevirt-ui.io/os-type"] = image.os_type
    if image.display_name:
        dv_annotations["kubevirt-ui.io/display-name"] = image.display_name

    dv_body = {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": image.name,
            "namespace": ns,
            "labels": dv_labels,
            "annotations": dv_annotations,
        },
        "spec": {
            "source": source,
            "storage": {
                "resources": {
                    "requests": {
                        "storage": image.size,
                    }
                },
            },
        },
    }

    try:
        result = await k8s.custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=ns,
            plural="datavolumes",
            body=dv_body,
        )
        return _parse_tenant_image_dv(result)
    except ApiException as e:
        raise k8s_error_to_http(e, "tenant operation")


@router.delete("/{name}/images/{image_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant_image(request: Request, name: str, image_name: str, user: User = Depends(require_auth)) -> None:
    """Delete an image from a tenant namespace."""
    k8s = request.app.state.k8s_client
    ns = _tenant_ns(name)

    if not await _namespace_exists(k8s, ns):
        raise HTTPException(status_code=404, detail=f"Tenant '{name}' not found")

    try:
        await k8s.custom_api.delete_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=ns,
            plural="datavolumes",
            name=image_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Image '{image_name}' not found")
        raise k8s_error_to_http(e, "tenant operation")
