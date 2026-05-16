"""Per-namespace watch-backed VM list cache.

A thin registry around :class:`app.core.watch_cache.K8sWatchCache`. Each
namespace gets its own cache instance, started lazily on first access and
shared across requests. Callers fall back to a direct LIST when the cache
fails to start or never reaches ready — so a misbehaving watch never blocks
the API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.constants import KUBEVIRT_API_GROUP, KUBEVIRT_API_VERSION
from app.core.k8s_client import K8sClient
from app.core.watch_cache import K8sWatchCache

logger = logging.getLogger(__name__)


class VMCacheRegistry:
    """Lazy per-namespace cache registry for VirtualMachines.

    First call to :meth:`list_vms` for a namespace creates and starts a watch
    cache for that namespace; later calls read from the in-memory dict.
    """

    _WAIT_READY_TIMEOUT = 5.0

    def __init__(self, k8s_client: K8sClient) -> None:
        self._k8s_client = k8s_client
        self._caches: dict[str, K8sWatchCache[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def _ensure_cache(self, namespace: str) -> K8sWatchCache[dict[str, Any]] | None:
        cache = self._caches.get(namespace)
        if cache is not None:
            return cache
        async with self._lock:
            cache = self._caches.get(namespace)
            if cache is not None:
                return cache
            cache = K8sWatchCache(
                api_client=self._k8s_client._api_client,
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                plural="virtualmachines",
                namespace=namespace,
            )
            try:
                await cache.start()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"VMCacheRegistry: failed to start cache for {namespace}: {e}")
                return None
            self._caches[namespace] = cache
            return cache

    async def list_vms(self, namespace: str) -> list[dict[str, Any]]:
        """Return all cached VMs in ``namespace``.

        Falls back to a direct LIST if the cache cannot be started or isn't
        ready within ``_WAIT_READY_TIMEOUT``.
        """
        cache = await self._ensure_cache(namespace)
        if cache is None:
            return await self._k8s_client.list_virtual_machines(namespace=namespace)
        if not cache.ready:
            try:
                await cache.wait_ready(timeout=self._WAIT_READY_TIMEOUT)
            except TimeoutError:
                logger.warning(
                    f"VMCacheRegistry: cache for {namespace} not ready, falling back to LIST"
                )
                return await self._k8s_client.list_virtual_machines(namespace=namespace)
        return cache.all()

    async def close(self) -> None:
        for ns, cache in list(self._caches.items()):
            try:
                await cache.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"VMCacheRegistry: stop failed for {ns}: {e}")
        self._caches.clear()
