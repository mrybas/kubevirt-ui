"""Taking room from a sibling has to be one operation, or it half-happens.

Environments could be created with a quota and deleted, never re-sized, so
"free up 2 CPU from dev" meant deleting dev. And once a rebalance is two
requests, the second can fail after the first landed and leave a sibling
smaller for nothing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.folders import (
    _plan_frees,
    _quota_hard,
    _undo_reallocations,
    _write_env_quota,
)


def _item(source: str, kind: str = "environment", **q) -> SimpleNamespace:
    return SimpleNamespace(
        source=source, kind=kind,
        cpu=q.get("cpu"), memory=q.get("memory"), storage=q.get("storage"),
    )


class TestQuotaHard:
    def test_requests_and_limits_move_together(self) -> None:
        # A quota naming only requests lets a caller declare a small request
        # and an unbounded limit and take the node anyway.
        hard = _quota_hard("8", "16Gi", "100Gi")
        assert hard["requests.cpu"] == hard["limits.cpu"] == "8"
        assert hard["requests.memory"] == hard["limits.memory"] == "16Gi"

    def test_storage_has_no_limit_counterpart(self) -> None:
        # There is no limits.storage in Kubernetes; inventing one would make
        # every PVC fail admission.
        assert "limits.storage" not in _quota_hard(None, None, "100Gi")

    def test_nothing_asked_is_nothing_written(self) -> None:
        assert _quota_hard(None, None, None) == {}


class TestPlanArithmetic:
    def test_shrinking_a_sibling_frees_exactly_the_difference(self) -> None:
        allocated = {"cpu": 14.0, "memory": 0.0, "storage": 0.0}
        before = {"dev": {"cpu": "8"}}
        planned = _plan_frees(allocated, [_item("dev", cpu="6")], before)
        assert planned["cpu"] == 12.0

    def test_growing_a_sibling_costs(self) -> None:
        allocated = {"cpu": 14.0, "memory": 0.0, "storage": 0.0}
        planned = _plan_frees(allocated, [_item("dev", cpu="10")], {"dev": {"cpu": "8"}})
        assert planned["cpu"] == 16.0

    def test_clearing_a_sibling_frees_all_of_it(self) -> None:
        allocated = {"cpu": 14.0, "memory": 0.0, "storage": 0.0}
        planned = _plan_frees(allocated, [_item("dev")], {"dev": {"cpu": "8"}})
        assert planned["cpu"] == 6.0

    def test_several_sources_add_up(self) -> None:
        allocated = {"cpu": 14.0, "memory": 0.0, "storage": 0.0}
        planned = _plan_frees(
            allocated,
            [_item("dev", cpu="6"), _item("prod", cpu="4")],
            {"dev": {"cpu": "8"}, "prod": {"cpu": "6"}},
        )
        assert planned["cpu"] == 10.0

    def test_a_dimension_nobody_touched_is_unchanged(self) -> None:
        allocated = {"cpu": 14.0, "memory": 99.0, "storage": 0.0}
        planned = _plan_frees(allocated, [_item("dev", cpu="6")], {"dev": {"cpu": "8"}})
        assert planned["memory"] == 99.0


@pytest.mark.asyncio
class TestWriteQuota:
    async def test_an_existing_quota_is_replaced(self) -> None:
        k8s = MagicMock()
        k8s.core_api.replace_namespaced_resource_quota = AsyncMock()
        await _write_env_quota(k8s, "lab-dev", "6", None, None)
        body = k8s.core_api.replace_namespaced_resource_quota.await_args.kwargs["body"]
        assert body["spec"]["hard"]["requests.cpu"] == "6"

    async def test_a_missing_quota_is_created(self) -> None:
        from kubernetes_asyncio.client import ApiException

        k8s = MagicMock()
        k8s.core_api.replace_namespaced_resource_quota = AsyncMock(
            side_effect=ApiException(status=404, reason="NotFound"),
        )
        k8s.core_api.create_namespaced_resource_quota = AsyncMock()
        await _write_env_quota(k8s, "lab-dev", "6", None, None)
        k8s.core_api.create_namespaced_resource_quota.assert_awaited_once()

    async def test_clearing_deletes_rather_than_writing_an_empty_quota(self) -> None:
        # An empty spec.hard caps nothing and reads as "quota configured".
        k8s = MagicMock()
        k8s.core_api.delete_namespaced_resource_quota = AsyncMock()
        await _write_env_quota(k8s, "lab-dev", None, None, None)
        k8s.core_api.delete_namespaced_resource_quota.assert_awaited_once()


@pytest.mark.asyncio
class TestUndo:
    async def test_a_shrunk_sibling_is_restored(self) -> None:
        k8s = MagicMock()
        k8s.core_api.replace_namespaced_resource_quota = AsyncMock()
        await _undo_reallocations(
            k8s, {}, "lab", [(_item("dev", cpu="6"), {"cpu": "8"})],
        )
        body = k8s.core_api.replace_namespaced_resource_quota.await_args.kwargs["body"]
        assert body["spec"]["hard"]["requests.cpu"] == "8"

    async def test_a_sibling_that_had_none_is_cleared_again(self) -> None:
        k8s = MagicMock()
        k8s.core_api.delete_namespaced_resource_quota = AsyncMock()
        await _undo_reallocations(k8s, {}, "lab", [(_item("dev", cpu="6"), None)])
        k8s.core_api.delete_namespaced_resource_quota.assert_awaited_once()

    async def test_a_failing_restore_does_not_mask_the_original_error(self) -> None:
        # The caller is already raising; a second exception here would
        # replace the reason the rebalance was rolled back at all.
        k8s = MagicMock()
        k8s.core_api.replace_namespaced_resource_quota = AsyncMock(
            side_effect=RuntimeError("gone"),
        )
        await _undo_reallocations(
            k8s, {}, "lab", [(_item("dev", cpu="6"), {"cpu": "8"})],
        )


def test_add_environment_validates_before_it_shrinks_anything() -> None:
    import inspect

    from app.api.v1 import folders as folders_mod

    src = inspect.getsource(folders_mod.add_environment)
    assert src.index("_plan_frees") < src.index("_apply_reallocations"), (
        "the plan has to be checked before a sibling is made smaller"
    )
    assert "_undo_reallocations" in src, (
        "a failed create must give the room back"
    )
