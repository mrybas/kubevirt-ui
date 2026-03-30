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
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.auth import User, require_auth
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
        "mode": "direct" if _is_direct_mode() else "proxy",
        "backend": (
            f"{backend.namespace}/{backend.service}:{backend.port}"
            if backend.discovered
            else None
        ),
    }


@router.get("/query")
async def metrics_query(
    request: Request,
    query: str = Query(..., description="PromQL query"),
    time: str | None = Query(None, description="Evaluation timestamp (Unix or RFC3339)"),
) -> dict:
    """Execute an instant PromQL query."""
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
    query: str = Query(..., description="PromQL query"),
    start: float = Query(..., description="Start timestamp (Unix epoch)"),
    end: float = Query(..., description="End timestamp (Unix epoch)"),
    step: str | None = Query(None, description="Step duration (e.g. 15s, 1m, 5m). Auto-calculated if omitted."),
) -> dict:
    """Execute a range PromQL query. Step is auto-calculated if omitted."""
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
