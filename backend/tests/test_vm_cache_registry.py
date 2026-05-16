"""Smoke tests for VMCacheRegistry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.vm_cache import VMCacheRegistry


def _make_vm(name: str, namespace: str = "default", rv: str = "1") -> dict:
    return {"metadata": {"name": name, "namespace": namespace, "resourceVersion": rv}}


class _HangStream:
    async def __aenter__(self) -> AsyncIterator[dict]:
        async def _gen() -> AsyncIterator[dict]:
            await asyncio.sleep(3600)
            if False:
                yield {}

        return _gen()

    async def __aexit__(self, *args: Any) -> None:
        return None


class _W:
    def stream(self, *a: Any, **k: Any) -> Any:  # noqa: ARG002
        return _HangStream()


def _fake_k8s_client(list_return: dict) -> MagicMock:
    """Build a fake K8sClient with the bare minimum surface VMCacheRegistry uses."""
    client = MagicMock()
    client._api_client = MagicMock()
    # Stub list_virtual_machines for the fallback path
    client.list_virtual_machines = AsyncMock(return_value=list_return.get("items", []))
    return client


@pytest.mark.asyncio
async def test_list_vms_uses_cache_after_warmup() -> None:
    list_return = {
        "items": [_make_vm("a"), _make_vm("b")],
        "metadata": {"resourceVersion": "1"},
    }
    k8s_client = _fake_k8s_client(list_return)
    registry = VMCacheRegistry(k8s_client)

    list_mock = AsyncMock(return_value=list_return)
    with (
        patch("app.core.watch_cache.CustomObjectsApi") as mock_api_cls,
        patch("app.core.watch_cache.watch.Watch", lambda: _W()),
    ):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object = list_mock
        mock_api_cls.return_value = mock_api

        vms = await registry.list_vms("default")
        assert {v["metadata"]["name"] for v in vms} == {"a", "b"}
        # Second call hits the in-memory cache (no extra LIST).
        vms2 = await registry.list_vms("default")
        assert {v["metadata"]["name"] for v in vms2} == {"a", "b"}
        # Only one initial LIST was issued.
        assert list_mock.await_count == 1
        await registry.close()


@pytest.mark.asyncio
async def test_separate_caches_per_namespace() -> None:
    k8s_client = _fake_k8s_client({"items": []})
    registry = VMCacheRegistry(k8s_client)

    responses = {
        "ns-a": {"items": [_make_vm("x", "ns-a")], "metadata": {"resourceVersion": "1"}},
        "ns-b": {"items": [_make_vm("y", "ns-b")], "metadata": {"resourceVersion": "1"}},
    }

    async def _list(*, namespace: str, **_: Any) -> dict:
        return responses[namespace]

    with (
        patch("app.core.watch_cache.CustomObjectsApi") as mock_api_cls,
        patch("app.core.watch_cache.watch.Watch", lambda: _W()),
    ):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object = AsyncMock(side_effect=_list)
        mock_api_cls.return_value = mock_api

        vms_a = await registry.list_vms("ns-a")
        vms_b = await registry.list_vms("ns-b")
        assert [v["metadata"]["name"] for v in vms_a] == ["x"]
        assert [v["metadata"]["name"] for v in vms_b] == ["y"]
        await registry.close()


@pytest.mark.asyncio
async def test_falls_back_to_direct_list_when_cache_start_fails() -> None:
    fallback_items = [_make_vm("fallback")]
    k8s_client = _fake_k8s_client({"items": fallback_items})
    registry = VMCacheRegistry(k8s_client)

    # Force cache start to fail by making the initial LIST raise.
    with patch("app.core.watch_cache.CustomObjectsApi") as mock_api_cls:
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object = AsyncMock(side_effect=RuntimeError("boom"))
        mock_api_cls.return_value = mock_api

        vms = await registry.list_vms("default")
        assert [v["metadata"]["name"] for v in vms] == ["fallback"]
        k8s_client.list_virtual_machines.assert_awaited_once_with(namespace="default")
        # No cache was registered for the namespace.
        assert "default" not in registry._caches
        await registry.close()
