"""Tests for app.core.watch_cache.K8sWatchCache."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from app.core.watch_cache import K8sWatchCache


def _make_vm(name: str, namespace: str = "default", rv: str = "1", **extra: Any) -> dict:
    md: dict[str, Any] = {"name": name, "namespace": namespace, "resourceVersion": rv}
    md.update(extra.pop("metadata", {}) or {})
    return {"metadata": md, **extra}


class _FakeStream:
    """Async context manager that yields a pre-baked sequence of watch events."""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def __aenter__(self) -> AsyncIterator[dict]:
        async def _gen() -> AsyncIterator[dict]:
            for e in self._events:
                yield e

        return _gen()

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeWatch:
    """Replacement for kubernetes_asyncio.watch.Watch.

    Successive calls to ``stream`` return successive scripted event sequences.
    """

    def __init__(self, scripts: list[list[dict]]) -> None:
        self._scripts = list(scripts)
        self.stream_calls: list[dict] = []

    def stream(self, func: Any, **kwargs: Any) -> _FakeStream:  # noqa: ARG002
        self.stream_calls.append(kwargs)
        events = self._scripts.pop(0) if self._scripts else []
        return _FakeStream(events)


def _watch_factory(scripts: list[list[dict]]) -> tuple[Any, _FakeWatch]:
    instance = _FakeWatch(scripts)
    return (lambda: instance), instance


def _make_cache(
    list_return: dict | None = None,
    list_side_effect: list[Any] | None = None,
    namespace: str | None = "default",
    project: Any = None,
) -> tuple[K8sWatchCache[Any], MagicMock]:
    api_client = MagicMock()
    cache: K8sWatchCache[Any] = K8sWatchCache(
        api_client=api_client,
        group="kubevirt.io",
        version="v1",
        plural="virtualmachines",
        namespace=namespace,
        project=project,
    )
    list_mock = AsyncMock()
    if list_side_effect is not None:
        list_mock.side_effect = list_side_effect
    else:
        list_mock.return_value = list_return or {"items": [], "metadata": {"resourceVersion": "1"}}
    if namespace is not None:
        cache._api.list_namespaced_custom_object = list_mock  # type: ignore[method-assign]
    else:
        cache._api.list_cluster_custom_object = list_mock  # type: ignore[method-assign]
    return cache, list_mock


@pytest.mark.asyncio
async def test_initial_list_populates_cache() -> None:
    cache, _ = _make_cache(
        list_return={
            "items": [_make_vm("a"), _make_vm("b")],
            "metadata": {"resourceVersion": "42"},
        }
    )
    await cache._initial_list()
    assert cache.ready is True
    assert {e["metadata"]["name"] for e in cache.all()} == {"a", "b"}
    assert cache._resource_version == "42"


@pytest.mark.asyncio
async def test_projection_applied_to_entries() -> None:
    cache, _ = _make_cache(
        list_return={
            "items": [_make_vm("a"), _make_vm("b")],
            "metadata": {"resourceVersion": "1"},
        },
        project=lambda r: r["metadata"]["name"].upper(),
    )
    await cache._initial_list()
    assert sorted(cache.all()) == ["A", "B"]


@pytest.mark.asyncio
async def test_get_returns_entry_by_key() -> None:
    cache, _ = _make_cache(
        list_return={
            "items": [_make_vm("a"), _make_vm("b")],
            "metadata": {"resourceVersion": "1"},
        }
    )
    await cache._initial_list()
    assert cache.get("default/a") is not None
    assert cache.get("default/missing") is None


@pytest.mark.asyncio
async def test_filter_returns_subset() -> None:
    cache, _ = _make_cache(
        list_return={
            "items": [_make_vm("alpha"), _make_vm("beta"), _make_vm("alphabet")],
            "metadata": {"resourceVersion": "1"},
        }
    )
    await cache._initial_list()
    matches = cache.filter(lambda v: v["metadata"]["name"].startswith("alpha"))
    assert {v["metadata"]["name"] for v in matches} == {"alpha", "alphabet"}


@pytest.mark.asyncio
async def test_wait_ready_returns_after_initial_list() -> None:
    cache, _ = _make_cache(
        list_return={"items": [_make_vm("a")], "metadata": {"resourceVersion": "1"}}
    )
    # Not ready yet
    with pytest.raises(asyncio.TimeoutError):
        await cache.wait_ready(timeout=0.05)
    await cache._initial_list()
    await cache.wait_ready(timeout=0.5)  # returns immediately


@pytest.mark.asyncio
async def test_added_event_upserts_entry() -> None:
    cache, _ = _make_cache(list_return={"items": [], "metadata": {"resourceVersion": "1"}})
    scripts = [[{"type": "ADDED", "object": _make_vm("new", rv="2")}]]
    factory, _ = _watch_factory(scripts)
    with patch("app.core.watch_cache.watch.Watch", factory):
        await cache.start()
        for _ in range(5):
            await asyncio.sleep(0)
        assert cache.get("default/new") is not None
        await cache.stop()


@pytest.mark.asyncio
async def test_modified_event_updates_projection() -> None:
    cache, _ = _make_cache(
        list_return={
            "items": [_make_vm("a", rv="1", spec={"v": 1})],
            "metadata": {"resourceVersion": "1"},
        },
        project=lambda r: r.get("spec", {}).get("v"),
    )
    scripts = [[{"type": "MODIFIED", "object": _make_vm("a", rv="2", spec={"v": 99})}]]
    factory, _ = _watch_factory(scripts)
    with patch("app.core.watch_cache.watch.Watch", factory):
        await cache.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await cache.stop()
    # cache.stop clears entries — capture pre-stop. Re-run without stop:
    cache2, _ = _make_cache(
        list_return={
            "items": [_make_vm("a", rv="1", spec={"v": 1})],
            "metadata": {"resourceVersion": "1"},
        },
        project=lambda r: r.get("spec", {}).get("v"),
    )
    scripts2 = [[{"type": "MODIFIED", "object": _make_vm("a", rv="2", spec={"v": 99})}]]
    factory2, _ = _watch_factory(scripts2)
    with patch("app.core.watch_cache.watch.Watch", factory2):
        await cache2.start()
        for _ in range(5):
            await asyncio.sleep(0)
        assert cache2.get("default/a") == 99
        await cache2.stop()


@pytest.mark.asyncio
async def test_deleted_event_removes_entry() -> None:
    cache, _ = _make_cache(
        list_return={
            "items": [_make_vm("doomed")],
            "metadata": {"resourceVersion": "1"},
        }
    )
    scripts = [[{"type": "DELETED", "object": _make_vm("doomed", rv="2")}]]
    factory, _ = _watch_factory(scripts)
    with patch("app.core.watch_cache.watch.Watch", factory):
        await cache.start()
        for _ in range(5):
            await asyncio.sleep(0)
        assert cache.get("default/doomed") is None
        await cache.stop()


async def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    """Poll ``predicate()`` every event-loop tick until true or timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class _HangStream:
    """Stream that yields nothing and then blocks until cancelled — keeps
    the watch loop from busy-spinning once the scripted streams run out."""

    async def __aenter__(self) -> AsyncIterator[dict]:
        async def _gen() -> AsyncIterator[dict]:
            await asyncio.sleep(3600)
            if False:
                yield {}

        return _gen()

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_410_gone_triggers_relist() -> None:
    list_initial = {
        "items": [_make_vm("a", rv="1")],
        "metadata": {"resourceVersion": "1"},
    }
    list_after_410 = {
        "items": [_make_vm("a", rv="5"), _make_vm("b", rv="5")],
        "metadata": {"resourceVersion": "5"},
    }
    cache, list_mock = _make_cache(list_side_effect=[list_initial, list_after_410, list_after_410])
    cache._BACKOFF_INITIAL = 0.001  # type: ignore[attr-defined]
    cache._BACKOFF_MAX = 0.001  # type: ignore[attr-defined]

    class _Stream410:
        async def __aenter__(self) -> Any:
            raise ApiException(status=410, reason="Gone")

        async def __aexit__(self, *args: Any) -> None:
            return None

    streams: list[Any] = [_Stream410()]

    class _W:
        def stream(self, *a: Any, **k: Any) -> Any:  # noqa: ARG002
            return streams.pop(0) if streams else _HangStream()

    with patch("app.core.watch_cache.watch.Watch", lambda: _W()):
        await cache.start()
        assert await _wait_until(lambda: list_mock.await_count >= 2, timeout=1.0)
        # re-LIST repopulated cache from list_after_410 (2 items)
        assert {v["metadata"]["name"] for v in cache.all()} == {"a", "b"}
        await cache.stop()


@pytest.mark.asyncio
async def test_watch_disconnect_reconnects_with_backoff() -> None:
    cache, _ = _make_cache(list_return={"items": [], "metadata": {"resourceVersion": "1"}})
    cache._BACKOFF_INITIAL = 0.001  # type: ignore[attr-defined]
    cache._BACKOFF_MAX = 0.001  # type: ignore[attr-defined]

    class _StreamRaises:
        async def __aenter__(self) -> Any:
            raise RuntimeError("connection dropped")

        async def __aexit__(self, *args: Any) -> None:
            return None

    streams: list[Any] = [_StreamRaises()]

    class _W:
        def stream(self, *a: Any, **k: Any) -> Any:  # noqa: ARG002
            return streams.pop(0) if streams else _HangStream()

    with patch("app.core.watch_cache.watch.Watch", lambda: _W()):
        await cache.start()
        assert await _wait_until(lambda: cache.last_error is not None, timeout=1.0)
        assert "connection dropped" in (cache.last_error or "")
        await cache.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    cache, _ = _make_cache(list_return={"items": [], "metadata": {"resourceVersion": "1"}})

    class _W:
        def stream(self, *a: Any, **k: Any) -> Any:  # noqa: ARG002
            class _S:
                async def __aenter__(self) -> AsyncIterator[dict]:
                    async def _gen() -> AsyncIterator[dict]:
                        if False:
                            yield {}

                    return _gen()

                async def __aexit__(self, *args: Any) -> None:
                    return None

            return _S()

    with patch("app.core.watch_cache.watch.Watch", lambda: _W()):
        await cache.start()
        task = cache._task
        await cache.start()  # second call is a no-op
        assert cache._task is task
        await cache.stop()


@pytest.mark.asyncio
async def test_stop_cancels_watch_task() -> None:
    cache, _ = _make_cache(list_return={"items": [], "metadata": {"resourceVersion": "1"}})

    class _W:
        def stream(self, *a: Any, **k: Any) -> Any:  # noqa: ARG002
            class _S:
                async def __aenter__(self) -> AsyncIterator[dict]:
                    async def _gen() -> AsyncIterator[dict]:
                        # Yield nothing, just sit; the test cancels via stop().
                        await asyncio.sleep(10)
                        if False:
                            yield {}

                    return _gen()

                async def __aexit__(self, *args: Any) -> None:
                    return None

            return _S()

    with patch("app.core.watch_cache.watch.Watch", lambda: _W()):
        await cache.start()
        assert cache._task is not None and not cache._task.done()
        await cache.stop()
        assert cache._task is None
        assert cache.all() == []


@pytest.mark.asyncio
async def test_cluster_scope_uses_cluster_list() -> None:
    cache, list_mock = _make_cache(
        list_return={"items": [], "metadata": {"resourceVersion": "1"}},
        namespace=None,
    )
    await cache._initial_list()
    list_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_key_func_override() -> None:
    api_client = MagicMock()
    cache: K8sWatchCache[Any] = K8sWatchCache(
        api_client=api_client,
        group="kubevirt.io",
        version="v1",
        plural="virtualmachines",
        namespace="default",
        key_func=lambda r: r["metadata"]["name"],
    )
    list_mock = AsyncMock(
        return_value={
            "items": [_make_vm("a"), _make_vm("b")],
            "metadata": {"resourceVersion": "1"},
        }
    )
    cache._api.list_namespaced_custom_object = list_mock  # type: ignore[method-assign]
    await cache._initial_list()
    assert cache.get("a") is not None
    assert cache.get("default/a") is None


@pytest.mark.asyncio
async def test_bookmark_event_updates_resource_version() -> None:
    cache, _ = _make_cache(list_return={"items": [], "metadata": {"resourceVersion": "1"}})
    scripts = [
        [
            {
                "type": "BOOKMARK",
                "object": {"metadata": {"resourceVersion": "99"}},
            }
        ]
    ]
    factory, _ = _watch_factory(scripts)
    with patch("app.core.watch_cache.watch.Watch", factory):
        await cache.start()
        for _ in range(5):
            await asyncio.sleep(0)
        assert cache._resource_version == "99"
        await cache.stop()
