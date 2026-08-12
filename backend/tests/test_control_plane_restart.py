"""Unit tests for restarting a Kamaji control plane.

`rollout restart` is a no-op here: Kamaji owns the Deployment and reverts the
pod-template annotation, so the rollout reports success while the pods keep
their original start time. Deleting the pods is the only thing that works.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1.tenants_common import (
    KAMAJI_CP_POD_LABEL,
    restart_control_plane_pods,
)


def _pod(name: str) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    return pod


def _k8s(pods: list[str]) -> MagicMock:
    k8s = MagicMock()
    k8s.core_api.list_namespaced_pod = AsyncMock(
        return_value=MagicMock(items=[_pod(p) for p in pods]),
    )
    k8s.core_api.delete_namespaced_pod = AsyncMock()
    return k8s


@pytest.mark.asyncio
class TestRestartControlPlanePods:
    async def test_deletes_every_matching_pod(self) -> None:
        k8s = _k8s(["demo-abc", "demo-def"])

        assert await restart_control_plane_pods(k8s, "demo") == 2
        assert k8s.core_api.delete_namespaced_pod.await_count == 2

    async def test_selects_by_the_kamaji_label_in_the_tenant_namespace(self) -> None:
        k8s = _k8s(["demo-abc"])

        await restart_control_plane_pods(k8s, "demo")

        kwargs = k8s.core_api.list_namespaced_pod.await_args.kwargs
        assert kwargs["namespace"] == "tenant-demo"
        assert kwargs["label_selector"] == f"{KAMAJI_CP_POD_LABEL}=demo"

    async def test_never_touches_deployments(self) -> None:
        # The whole point: a rollout would report success and change nothing.
        k8s = _k8s(["demo-abc"])

        await restart_control_plane_pods(k8s, "demo")

        assert not k8s.apps_api.method_calls

    async def test_no_pods_is_not_an_error(self) -> None:
        # The control plane may simply not be up yet.
        assert await restart_control_plane_pods(_k8s([]), "demo") == 0

    async def test_pod_deleted_by_someone_else_is_skipped(self) -> None:
        k8s = _k8s(["demo-abc", "demo-def"])
        k8s.core_api.delete_namespaced_pod = AsyncMock(side_effect=[
            ApiException(status=404, reason="Not Found"), None,
        ])

        assert await restart_control_plane_pods(k8s, "demo") == 1

    async def test_a_real_delete_failure_propagates(self) -> None:
        k8s = _k8s(["demo-abc"])
        k8s.core_api.delete_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden"),
        )

        with pytest.raises(ApiException):
            await restart_control_plane_pods(k8s, "demo")

    async def test_a_list_failure_propagates(self) -> None:
        k8s = MagicMock()
        k8s.core_api.list_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=500, reason="boom"),
        )

        with pytest.raises(ApiException):
            await restart_control_plane_pods(k8s, "demo")
