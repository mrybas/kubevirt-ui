"""Generic in-memory cache backed by a Kubernetes watch stream.

Pattern: initial LIST captures the current state + a ``resourceVersion``, then a
background watch loop streams ADDED/MODIFIED/DELETED events and incrementally
updates an in-memory dict. On stream timeout or transient errors the loop
reconnects with exponential backoff; on ``410 Gone`` (resourceVersion expired)
it performs a full re-LIST.

Reads are lock-free (CPython dict ``get``/iteration are safe with the GIL given
a single writer); writes happen sequentially on the event-loop task that owns
the watch stream.

Intended consumers: VM search, DataVolume status panels, Snapshot lists, Tenant
listings — anywhere where the API would otherwise repeatedly pay the cost of a
full LIST.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from kubernetes_asyncio import watch
from kubernetes_asyncio.client import ApiClient, CustomObjectsApi
from kubernetes_asyncio.client.exceptions import ApiException

T = TypeVar("T")

_DEFAULT_LOGGER = logging.getLogger(__name__)


def _default_key(raw: dict) -> str:
    md = raw.get("metadata", {})
    ns = md.get("namespace", "")
    return f"{ns}/{md.get('name', '')}" if ns else md.get("name", "")


class K8sWatchCache(Generic[T]):
    """Watch-backed cache for a single K8s custom resource type.

    ``project`` reduces a raw resource dict to a smaller value stored in the
    cache; callers that need the full object should re-fetch directly from the
    API. ``key_func`` derives the dictionary key (default: ``"{ns}/{name}"``).
    """

    # Watch stream timeout. Triggers a clean reconnect rather than hanging.
    _WATCH_TIMEOUT_SECONDS = 300

    # Backoff schedule for transient reconnect errors.
    _BACKOFF_INITIAL = 1.0
    _BACKOFF_MAX = 30.0

    def __init__(
        self,
        api_client: ApiClient,
        group: str,
        version: str,
        plural: str,
        namespace: str | None = None,
        project: Callable[[dict], T] | None = None,
        key_func: Callable[[dict], str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._api = CustomObjectsApi(api_client)
        self._group = group
        self._version = version
        self._plural = plural
        self._namespace = namespace
        self._project: Callable[[dict], Any] = project or (lambda r: r)
        self._key_func: Callable[[dict], str] = key_func or _default_key
        self._logger = logger or _DEFAULT_LOGGER

        self._entries: dict[str, T] = {}
        self._resource_version: str | None = None
        self._ready_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self.last_error: str | None = None

    # -- public API -----------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready_event.is_set()

    def get(self, key: str) -> T | None:
        return self._entries.get(key)

    def all(self) -> list[T]:
        return list(self._entries.values())

    def filter(self, predicate: Callable[[T], bool]) -> list[T]:
        return [v for v in self._entries.values() if predicate(v)]

    async def wait_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def start(self) -> None:
        """Run initial LIST and start the watch loop.

        Returns once the initial LIST has populated the cache; the watch loop
        continues in a background task until :meth:`stop` is called.
        """
        if self._task is not None:
            return
        await self._initial_list()
        self._task = asyncio.create_task(self._watch_loop(), name=f"watch-cache:{self._plural}")

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._entries.clear()
        self._ready_event.clear()

    # -- internal -------------------------------------------------------------

    async def _list(self) -> dict[str, Any]:
        if self._namespace is not None:
            return await self._api.list_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
            )
        return await self._api.list_cluster_custom_object(
            group=self._group,
            version=self._version,
            plural=self._plural,
        )

    async def _initial_list(self) -> None:
        result = await self._list()
        new_entries: dict[str, T] = {}
        for item in result.get("items", []):
            try:
                new_entries[self._key_func(item)] = self._project(item)
            except Exception as e:  # noqa: BLE001
                self._logger.warning(
                    f"watch-cache[{self._plural}] projection failed during LIST: {e}"
                )
        self._entries = new_entries
        self._resource_version = result.get("metadata", {}).get("resourceVersion")
        self._ready_event.set()
        self._logger.info(
            f"watch-cache[{self._plural}] LIST populated {len(new_entries)} entries"
            f" (rv={self._resource_version})"
        )

    def _list_callable(self) -> Callable[..., Awaitable[Any]]:
        if self._namespace is not None:

            async def _call(**kwargs: Any) -> Any:
                return await self._api.list_namespaced_custom_object(
                    group=self._group,
                    version=self._version,
                    namespace=self._namespace,
                    plural=self._plural,
                    **kwargs,
                )

            return _call

        async def _call_cluster(**kwargs: Any) -> Any:
            return await self._api.list_cluster_custom_object(
                group=self._group,
                version=self._version,
                plural=self._plural,
                **kwargs,
            )

        return _call_cluster

    def _apply_event(self, event_type: str, obj: dict) -> None:
        if not isinstance(obj, dict):
            return
        try:
            key = self._key_func(obj)
        except Exception as e:  # noqa: BLE001
            self._logger.warning(f"watch-cache[{self._plural}] key_func failed: {e}")
            return

        if event_type == "DELETED":
            self._entries.pop(key, None)
            return

        if event_type in ("ADDED", "MODIFIED"):
            try:
                self._entries[key] = self._project(obj)
            except Exception as e:  # noqa: BLE001
                self._logger.warning(
                    f"watch-cache[{self._plural}] projection failed for {key}: {e}"
                )

    async def _watch_loop(self) -> None:
        backoff = self._BACKOFF_INITIAL
        while not self._stopped:
            try:
                w = watch.Watch()
                list_callable = self._list_callable()
                kwargs: dict[str, Any] = {
                    "timeout_seconds": self._WATCH_TIMEOUT_SECONDS,
                }
                if self._resource_version:
                    kwargs["resource_version"] = self._resource_version

                async with w.stream(list_callable, **kwargs) as stream:
                    async for event in stream:
                        event_type = event.get("type", "")
                        obj = event.get("object") or event.get("raw_object") or {}
                        if event_type == "BOOKMARK":
                            rv = obj.get("metadata", {}).get("resourceVersion")
                            if rv:
                                self._resource_version = rv
                            continue
                        if event_type == "ERROR":
                            # Watch error — fall through to reconnect path
                            self._logger.warning(f"watch-cache[{self._plural}] ERROR event: {obj}")
                            break
                        self._apply_event(event_type, obj)
                        rv = obj.get("metadata", {}).get("resourceVersion")
                        if rv:
                            self._resource_version = rv

                # Stream ended cleanly (timeout). Reset backoff and reconnect.
                backoff = self._BACKOFF_INITIAL
                # Yield so cancellation / stop() can be observed promptly.
                await asyncio.sleep(0)

            except asyncio.CancelledError:
                raise
            except ApiException as e:
                if e.status == 410:
                    self._logger.warning(f"watch-cache[{self._plural}] 410 Gone, re-listing")
                    self._resource_version = None
                    try:
                        await self._initial_list()
                    except Exception as relist_err:  # noqa: BLE001
                        self.last_error = str(relist_err)
                        self._logger.error(
                            f"watch-cache[{self._plural}] re-LIST failed: {relist_err}"
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, self._BACKOFF_MAX)
                    else:
                        backoff = self._BACKOFF_INITIAL
                    continue

                self.last_error = f"ApiException({e.status}): {e.reason}"
                self._logger.warning(f"watch-cache[{self._plural}] watch failed: {self.last_error}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_MAX)
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                self._logger.warning(f"watch-cache[{self._plural}] watch error: {self.last_error}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_MAX)
