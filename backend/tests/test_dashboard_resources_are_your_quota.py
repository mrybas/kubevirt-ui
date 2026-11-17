"""The dashboard's usage cards show the caller's quota, the cluster only to admins.

`GET /cluster/resources` listed every node, every running pod and every PV for
every caller. A backend without cluster-wide pod and node reads answered 500,
so the cards showed zeros, VM counts included; one with them showed every
tenant the whole cluster's capacity.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import cluster
from app.core.auth import User

MINE = "tenant-mine"


def _user(groups: list[str]) -> User:
    return User(id="u", email="u@example.test", username="u", groups=groups)


def _k8s() -> MagicMock:
    k8s = MagicMock()
    k8s.core_api.list_namespaced_resource_quota = AsyncMock(return_value=SimpleNamespace(items=[
        SimpleNamespace(
            spec=SimpleNamespace(hard={"requests.storage": "50Gi", "persistentvolumeclaims": "6"}),
            status=SimpleNamespace(used={"requests.storage": "25Gi"}),
        ),
    ]))
    forbidden = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    k8s.core_api.list_node = forbidden
    k8s.core_api.list_pod_for_all_namespaces = forbidden
    k8s.core_api.list_persistent_volume = forbidden
    return k8s


async def _custom(**kwargs: Any) -> dict:
    if kwargs["plural"] == "applicationawareresourcequotas":
        return {"items": [{
            "spec": {"hard": {"cpu/vmi": "2", "memory/vmi": "4Gi", "instances/vmi": "1"}},
            "status": {"used": {"cpu/vmi": "1", "memory/vmi": "2Gi"}},
        }]}
    if kwargs["plural"] == "virtualmachines":
        return {"items": [{}, {}]}
    return {"items": [{}]}


async def _resources(groups: list[str], k8s: MagicMock | None = None):
    k8s = k8s or _k8s()
    request = MagicMock()
    request.app.state.k8s_client = k8s
    api = MagicMock()
    api.list_namespaced_custom_object = AsyncMock(side_effect=_custom)
    with (
        patch.object(cluster, "get_user_namespaces", AsyncMock(return_value=[MINE])),
        patch.object(cluster.client, "CustomObjectsApi", return_value=api),
    ):
        return await cluster.get_user_resources(request=request, user=_user(groups)), k8s


@pytest.mark.asyncio
class TestATenant:
    async def test_sees_their_quota(self) -> None:
        got, _ = await _resources(["team-mine"])
        assert (got.cpu.used, got.cpu.total, got.cpu.percentage) == ("1.0", "2", 50.0)
        assert (got.memory.used, got.memory.total) == ("2.0Gi", "4Gi")
        assert (got.storage.used, got.storage.total, got.storage.percentage) == ("25.0Gi", "50Gi", 50.0)
        assert got.max_schedulable is None

    async def test_never_reads_the_cluster(self) -> None:
        _, k8s = await _resources(["team-mine"])
        k8s.core_api.list_node.assert_not_awaited()
        k8s.core_api.list_pod_for_all_namespaces.assert_not_awaited()

    async def test_gets_their_vm_counts(self) -> None:
        got, _ = await _resources(["team-mine"])
        assert (got.vms_total, got.vms_running) == (2, 1)


@pytest.mark.asyncio
class TestAnAdmin:
    async def test_without_cluster_reads_still_gets_an_answer(self) -> None:
        """The 500 this replaced: one forbidden list zeroed every card."""
        got, k8s = await _resources(["kubevirt-ui-admins"])
        k8s.core_api.list_node.assert_awaited()
        assert got.vms_total == 2
        assert got.storage.total == "50Gi"
