"""Discover the externally-reachable Kubernetes API server URL.

The in-cluster client always reports `https://10.96.0.1:443` (the
`kubernetes.default` service IP), which is unreachable from outside the
cluster. For kubeconfig generation we need the URL admins published —
typically a Talos VIP, kubeadm endpoint, or LB IP.

Resolution cascade (highest priority first):
  1. Env `KUBE_API_EXTERNAL_URL` — explicit admin override.
  2. `kube-public/cluster-info` ConfigMap — published by kubeadm and Talos,
     carries a full kubeconfig snippet in `data["kubeconfig"]`; the
     `clusters[0].cluster.server` field is the canonical external URL.
  3. Fallback — `https://kubernetes.default.svc:443` plus a warning so the
     UI can prompt the admin to fix one of the above.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import yaml
from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)

CLUSTER_INFO_NAMESPACE = "kube-public"
CLUSTER_INFO_CONFIGMAP = "cluster-info"
FALLBACK_URL = "https://kubernetes.default.svc:443"

_CACHE_TTL_SEC = 300

# {"url": str, "source": str, "fetched_at": float}
_cache: dict[str, Any] | None = None


def _reset_cache_for_tests() -> None:
    """Test hook — drop the cached value so the next call re-discovers."""
    global _cache
    _cache = None


def _from_env() -> str | None:
    """Return KUBE_API_EXTERNAL_URL if set and non-empty."""
    value = os.getenv("KUBE_API_EXTERNAL_URL", "").strip()
    return value or None


async def _from_cluster_info(k8s) -> str | None:
    """Parse kube-public/cluster-info and extract clusters[0].cluster.server.

    Returns None on 404, malformed YAML, or missing fields — caller falls
    through to the next strategy. Logs a warning for everything except 404
    (a missing ConfigMap is normal on clusters that don't publish it).
    """
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=CLUSTER_INFO_CONFIGMAP, namespace=CLUSTER_INFO_NAMESPACE,
        )
    except ApiException as e:
        if e.status == 404:
            logger.info(
                "kube-public/cluster-info ConfigMap not found — admin can "
                "publish it (kubeadm-style) or set KUBE_API_EXTERNAL_URL",
            )
        else:
            logger.warning(f"Failed to read kube-public/cluster-info: {e}")
        return None

    data = cm.data or {}
    raw = data.get("kubeconfig")
    if not raw:
        logger.warning(
            "kube-public/cluster-info has no 'kubeconfig' data key — "
            "non-standard layout, skipping",
        )
        return None

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning(f"kube-public/cluster-info kubeconfig is not valid YAML: {e}")
        return None

    try:
        clusters = parsed.get("clusters") or []
        if not clusters:
            logger.warning("kube-public/cluster-info kubeconfig has no clusters[]")
            return None
        server = clusters[0].get("cluster", {}).get("server")
        if not server or not isinstance(server, str):
            logger.warning(
                "kube-public/cluster-info kubeconfig clusters[0].cluster.server missing",
            )
            return None
        return server.strip()
    except (AttributeError, TypeError) as e:
        logger.warning(f"kube-public/cluster-info kubeconfig has unexpected shape: {e}")
        return None


async def discover_external_api_url(k8s) -> tuple[str, str]:
    """Return `(url, source)` where source ∈ {"env", "cluster-info", "fallback"}.

    Module-level TTL cache (5 min). The cache key is implicit — a given
    backend process only ever talks to one cluster, so a single slot is
    enough. Same pattern as `tenants_common._ensure_cluster_config`.
    """
    global _cache
    now = time.monotonic()
    if _cache and (now - _cache["fetched_at"]) < _CACHE_TTL_SEC:
        return _cache["url"], _cache["source"]

    url = _from_env()
    if url:
        source = "env"
    else:
        url = await _from_cluster_info(k8s)
        if url:
            source = "cluster-info"
        else:
            url = FALLBACK_URL
            source = "fallback"

    # Don't cache the fallback — admins frequently fix this by publishing
    # the cluster-info ConfigMap after the fact; we want subsequent calls
    # to retry the cluster-info read instead of returning the stale fallback.
    if source != "fallback":
        _cache = {"url": url, "source": source, "fetched_at": now}
    logger.info(f"External K8s API URL: {url} (source={source})")
    return url, source
