"""Metrics proxy endpoint — queries VictoriaMetrics/Prometheus.

Auto-discovers the metrics backend:
1. VMSingle CRD (VictoriaMetrics Operator)
2. Service with label app.kubernetes.io/name=vmsingle
3. Service with label app=prometheus or app.kubernetes.io/name=prometheus
4. Fallback to METRICS_SERVICE env var (format: namespace/service:port)

Two modes of operation:
- **Direct** (in-cluster): HTTP call to http://{svc}.{ns}.svc.cluster.local:{port}/...
- **Proxy** (out-of-cluster): via K8s API service proxy
  /api/v1/namespaces/{ns}/services/{svc}:{port}/proxy/api/v1/query_range

Mode is auto-detected from K8S_IN_CLUSTER / SA token presence, or forced
via METRICS_DIRECT=true|false.
"""

import hashlib
import json
import logging
import os
import re
import time
from string import Template
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.v1.storage import require_namespace_access
from app.core.auth import User, require_admin, require_auth
from app.core.groups import get_user_namespaces, is_admin
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 30
MAX_CACHE_ENTRIES = 200


def _cache_key(query: str, params: dict) -> str:
    raw = f"{query}|{sorted(params.items())}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data: Any) -> None:
    # Evict oldest if too many entries
    if len(_cache) >= MAX_CACHE_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest_key]
    _cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# Metrics backend discovery
# ---------------------------------------------------------------------------
class MetricsBackend:
    """Stores discovered metrics backend connection info."""

    def __init__(self) -> None:
        self.namespace: str | None = None
        self.service: str | None = None
        self.port: int | None = None
        self.discovered: bool = False
        self._last_discovery: float = 0
        self._discovery_interval = 300  # re-discover every 5 min

    @property
    def needs_discovery(self) -> bool:
        if not self.discovered:
            return True
        return time.time() - self._last_discovery > self._discovery_interval

    def set(self, namespace: str, service: str, port: int) -> None:
        self.namespace = namespace
        self.service = service
        self.port = port
        self.discovered = True
        self._last_discovery = time.time()
        logger.info(f"Metrics backend: {namespace}/{service}:{port}")

    def reset(self) -> None:
        self.discovered = False


_backend = MetricsBackend()


async def _discover_metrics_backend(k8s_client: Any) -> MetricsBackend:
    """Auto-discover VictoriaMetrics or Prometheus in the cluster."""
    if not _backend.needs_discovery:
        return _backend

    import os

    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    core_api = k8s_client.core_api

    # 1. Try VMSingle CRD (VictoriaMetrics Operator)
    try:
        result = await custom_api.list_cluster_custom_object(
            group="operator.victoriametrics.com",
            version="v1beta1",
            plural="vmsingles",
        )
        items = result.get("items", [])
        if items:
            vm_single = items[0]
            ns = vm_single["metadata"]["namespace"]
            name = vm_single["metadata"]["name"]
            # Service name follows pattern: vmsingle-{name}
            svc_name = f"vmsingle-{name}"
            # Get port from service
            try:
                svc = await core_api.read_namespaced_service(svc_name, ns)
                port = 8429  # default
                for p in svc.spec.ports or []:
                    if p.name == "http" or p.port in (8428, 8429):
                        port = p.port
                        break
                _backend.set(ns, svc_name, port)
                return _backend
            except ApiException:
                pass
    except ApiException:
        pass

    # 2. Try service with label app.kubernetes.io/name=vmsingle
    try:
        svcs = await core_api.list_service_for_all_namespaces(
            label_selector="app.kubernetes.io/name=vmsingle"
        )
        if svcs.items:
            svc = svcs.items[0]
            port = svc.spec.ports[0].port if svc.spec.ports else 8429
            _backend.set(svc.metadata.namespace, svc.metadata.name, port)
            return _backend
    except ApiException:
        pass

    # 3. Try Prometheus service
    for selector in [
        "app=prometheus",
        "app.kubernetes.io/name=prometheus",
        "app=kube-prometheus-stack-prometheus",
    ]:
        try:
            svcs = await core_api.list_service_for_all_namespaces(
                label_selector=selector
            )
            if svcs.items:
                svc = svcs.items[0]
                port = svc.spec.ports[0].port if svc.spec.ports else 9090
                _backend.set(svc.metadata.namespace, svc.metadata.name, port)
                return _backend
        except ApiException:
            pass

    # 4. Fallback to env var: METRICS_SERVICE=victoria-metrics/vmsingle-vmsingle:8429
    env_val = os.environ.get("METRICS_SERVICE")
    if env_val:
        try:
            ns_svc, port_str = env_val.rsplit(":", 1)
            ns, svc_name = ns_svc.split("/", 1)
            _backend.set(ns, svc_name, int(port_str))
            return _backend
        except (ValueError, IndexError):
            logger.warning(f"Invalid METRICS_SERVICE format: {env_val}")

    logger.warning("No metrics backend discovered")
    _backend.reset()
    return _backend


def _is_direct_mode() -> bool:
    """Determine if we should call metrics service directly (in-cluster)."""
    env = os.environ.get("METRICS_DIRECT", "").lower()
    if env in ("true", "1", "yes"):
        return True
    if env in ("false", "0", "no"):
        return False
    # Auto-detect: direct if running in-cluster
    return (
        os.environ.get("K8S_IN_CLUSTER", "").lower() in ("true", "1", "yes")
        or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
    )


async def _query_direct(
    backend: MetricsBackend,
    path: str,
    params: dict[str, str],
) -> dict:
    """Call metrics service directly via HTTP (in-cluster)."""
    url = f"http://{backend.service}.{backend.namespace}.svc.cluster.local:{backend.port}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Metrics direct request failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Metrics query failed: {e.response.status_code}",
        )
    except Exception as e:
        logger.error(f"Metrics direct error: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Metrics direct error: {e}",
        )
    if data.get("status") != "success":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Metrics query failed: {data.get('error', 'unknown error')}",
        )
    return data


async def _query_via_proxy(
    k8s_client: Any,
    backend: MetricsBackend,
    path: str,
    params: dict[str, str],
) -> dict:
    """Proxy a PromQL query through K8s API service proxy (out-of-cluster)."""
    proxy_path = (
        f"/api/v1/namespaces/{backend.namespace}"
        f"/services/{backend.service}:{backend.port}"
        f"/proxy/{path}"
    )
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    if query_string:
        proxy_path += f"?{query_string}"

    try:
        api_client = k8s_client._api_client
        response = await api_client.call_api(
            proxy_path,
            "GET",
            _return_http_data_only=True,
            _preload_content=False,
            response_types_map={"200": "object"},
        )
        body = await response.read()
        data = json.loads(body)

        if data.get("status") != "success":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Metrics query failed: {data.get('error', 'unknown error')}",
            )
        return data

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Metrics proxy request failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to proxy metrics query: {e.reason}",
        )
    except Exception as e:
        logger.error(f"Metrics proxy error: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Metrics proxy error: {str(e)}",
        )


async def _proxy_query(
    k8s_client: Any,
    path: str,
    params: dict[str, str],
) -> dict:
    """Route to direct or proxy mode based on environment."""
    backend = await _discover_metrics_backend(k8s_client)
    if not backend.discovered:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metrics backend not available. No VictoriaMetrics or Prometheus found in cluster.",
        )

    if _is_direct_mode():
        return await _query_direct(backend, path, params)
    return await _query_via_proxy(k8s_client, backend, path, params)


# ---------------------------------------------------------------------------
# Utility: auto step calculation
# ---------------------------------------------------------------------------
def _auto_step(start: float, end: float, max_points: int = 500) -> str:
    """Calculate a reasonable step size to limit data points."""
    duration = end - start
    step = max(int(duration / max_points), 15)
    return f"{step}s"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
async def metrics_status(request: Request, user: User = Depends(require_auth)) -> dict:
    """Check metrics backend availability."""
    k8s_client = request.app.state.k8s_client
    backend = await _discover_metrics_backend(k8s_client)
    return {
        "available": backend.discovered,
        # Node metrics describe the whole cluster, not the caller's namespaces.
        "cluster": is_admin(user.groups, user),
        "mode": "direct" if _is_direct_mode() else "proxy",
        "backend": (
            f"{backend.namespace}/{backend.service}:{backend.port}"
            if backend.discovered
            else None
        ),
    }



# PromQL is a query language with cost, not a read-only view. Authentication
# (router-level now) stops anonymous callers; these bounds stop an authenticated
# one from taking VictoriaMetrics — and the platform's monitoring — down with a
# single wide range query. Numbers are generous for a dashboard and hostile to
# a scrape of the whole retention window.
MAX_QUERY_CHARS = 2048
MAX_RANGE_SECONDS = 32 * 24 * 3600  # 32 days: a month view, not a year


def _reject_oversized_query(query: str) -> None:
    if len(query) > MAX_QUERY_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"PromQL query too long ({len(query)} chars, max {MAX_QUERY_CHARS})",
        )


def _reject_oversized_range(start: float, end: float) -> None:
    if end < start:
        raise HTTPException(status_code=422, detail="end is before start")
    span = end - start
    if span > MAX_RANGE_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"range too wide ({int(span)}s, max {MAX_RANGE_SECONDS}s) — "
                f"narrow the window or increase the step"
            ),
        )


@router.get("/query")
async def metrics_query(
    request: Request,
    user: User = Depends(require_admin),
    query: str = Query(..., description="PromQL query"),
    time: str | None = Query(None, description="Evaluation timestamp (Unix or RFC3339)"),
) -> dict:
    """Execute an instant PromQL query."""
    _reject_oversized_query(query)
    k8s_client = request.app.state.k8s_client

    params: dict[str, str] = {"query": query}
    if time:
        params["time"] = time

    key = _cache_key("instant", params)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = await _proxy_query(k8s_client, "api/v1/query", params)
    _cache_set(key, result)
    return result


@router.get("/query_range")
async def metrics_query_range(
    request: Request,
    user: User = Depends(require_admin),
    query: str = Query(..., description="PromQL query"),
    start: float = Query(..., description="Start timestamp (Unix epoch)"),
    end: float = Query(..., description="End timestamp (Unix epoch)"),
    step: str | None = Query(None, description="Step duration (e.g. 15s, 1m, 5m). Auto-calculated if omitted."),
) -> dict:
    """Execute a range PromQL query. Step is auto-calculated if omitted."""
    _reject_oversized_query(query)
    _reject_oversized_range(start, end)
    k8s_client = request.app.state.k8s_client

    resolved_step = step or _auto_step(start, end)

    params: dict[str, str] = {
        "query": query,
        "start": str(start),
        "end": str(end),
        "step": resolved_step,
    }

    key = _cache_key("range", params)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = await _proxy_query(k8s_client, "api/v1/query_range", params)
    _cache_set(key, result)
    return result


# ---------------------------------------------------------------------------
# Scoped queries
#
# Raw PromQL reads everything the metrics backend holds, so it is admin-only.
# Everyone else names a metric; the query is built here with the namespace
# selector the caller is allowed.
# ---------------------------------------------------------------------------

# The label carrying the VM's namespace. Behind a ServiceMonitor without
# honorLabels the exporter's own `namespace` becomes `exported_namespace`.
NAMESPACE_LABEL = os.getenv("METRICS_NAMESPACE_LABEL", "exported_namespace")

_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_STEP = re.compile(r"^[0-9]+[smhd]$")

VM_QUERIES: dict[str, str] = {
    "cpu": "rate(kubevirt_vmi_cpu_usage_seconds_total{$sel}[2m]) * 100",
    "memory_used": "kubevirt_vmi_memory_domain_bytes{$sel} - kubevirt_vmi_memory_unused_bytes{$sel}",
    "memory_total": "kubevirt_vmi_memory_domain_bytes{$sel}",
    "disk_read": "sum(rate(kubevirt_vmi_storage_read_traffic_bytes_total{$sel}[2m]))",
    "disk_write": "sum(rate(kubevirt_vmi_storage_write_traffic_bytes_total{$sel}[2m]))",
    "net_rx": "rate(kubevirt_vmi_network_receive_bytes_total{$sel}[2m])",
    "net_tx": "rate(kubevirt_vmi_network_transmit_bytes_total{$sel}[2m])",
}

TOP_QUERIES: dict[str, str] = {
    "cpu": "topk(5, rate(kubevirt_vmi_cpu_usage_seconds_total{$sel}[5m]) * 100)",
    "memory": "topk(5, kubevirt_vmi_memory_domain_bytes{$sel} - kubevirt_vmi_memory_unused_bytes{$sel})",
}

CLUSTER_QUERIES: dict[str, str] = {
    "cpu": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "memory_used": "sum(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)",
    "memory_total": "sum(node_memory_MemTotal_bytes)",
}


def _k8s_name(value: str) -> str:
    """Names go into PromQL label values; only what Kubernetes allows passes."""
    if not _K8S_NAME.match(value):
        raise HTTPException(status_code=422, detail=f"invalid name: {value!r}")
    return value


def _range_params(query: str, start: float, end: float, step: str | None) -> dict[str, str]:
    _reject_oversized_range(start, end)
    if step is not None and not _STEP.match(step):
        raise HTTPException(status_code=422, detail=f"invalid step: {step!r}")
    return {
        "query": query,
        "start": str(start),
        "end": str(end),
        "step": step or _auto_step(start, end),
    }


async def _cached(k8s_client: Any, kind: str, path: str, params: dict[str, str]) -> dict:
    key = _cache_key(kind, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = await _proxy_query(k8s_client, path, params)
    _cache_set(key, result)
    return result


@router.get("/vms/{namespace}/{name}")
async def vm_metric_range(
    request: Request,
    namespace: str,
    name: str,
    metric: Literal[tuple(VM_QUERIES)] = Query(...),  # type: ignore[valid-type]
    start: float = Query(...),
    end: float = Query(...),
    step: str | None = Query(None),
    user: User = Depends(require_auth),
) -> dict:
    """One metric of one VM over time."""
    await require_namespace_access(request, user, namespace)
    sel = f'name="{_k8s_name(name)}", {NAMESPACE_LABEL}="{_k8s_name(namespace)}"'
    query = Template(VM_QUERIES[metric]).substitute(sel=sel)
    return await _cached(
        request.app.state.k8s_client, "range", "api/v1/query_range",
        _range_params(query, start, end, step),
    )


@router.get("/top")
async def top_vms(
    request: Request,
    metric: Literal[tuple(TOP_QUERIES)] = Query(...),  # type: ignore[valid-type]
    user: User = Depends(require_auth),
) -> dict:
    """The five busiest VMs among the caller's namespaces.

    Each series carries the namespace as `namespace`, whatever label the
    backend stores it under.
    """
    k8s_client = request.app.state.k8s_client
    namespaces = sorted(await get_user_namespaces(k8s_client, user))
    if not namespaces:
        return {"status": "success", "data": {"resultType": "vector", "result": []}}
    pattern = "|".join(_k8s_name(n) for n in namespaces)
    query = Template(TOP_QUERIES[metric]).substitute(sel=f'{NAMESPACE_LABEL}=~"{pattern}"')
    result = await _cached(k8s_client, "instant", "api/v1/query", {"query": query})
    for series in result.get("data", {}).get("result", []):
        labels = series.get("metric", {})
        labels["namespace"] = labels.get(NAMESPACE_LABEL, labels.get("namespace", ""))
    return result


@router.get("/cluster")
async def cluster_metric_range(
    request: Request,
    metric: Literal[tuple(CLUSTER_QUERIES)] = Query(...),  # type: ignore[valid-type]
    start: float = Query(...),
    end: float = Query(...),
    step: str | None = Query(None),
    user: User = Depends(require_admin),
) -> dict:
    """Node CPU and memory of the whole cluster over time."""
    return await _cached(
        request.app.state.k8s_client, "range", "api/v1/query_range",
        _range_params(CLUSTER_QUERIES[metric], start, end, step),
    )
