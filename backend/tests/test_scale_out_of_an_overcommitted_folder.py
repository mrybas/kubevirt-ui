"""Scaling when the folder is already over its ceiling, and the gap in between.

Two defects with one report behind them: a scale that said there was nowhere
to scale and scaled anyway, on a folder whose namespaces held 72 CPU of quota
under a 32 CPU ceiling.

  * The ceiling compared the whole request against what was free, and a
    folder can be over its ceiling without this function ever having agreed
    to it — a quota lowered under what is already handed out, or a tenant
    namespace that joined the folder later. Then nothing at all can be asked,
    including the request that hands room back: 3 workers → 2 was refused for
    lack of room to shrink into.

  * The tenant's own quota was written after its shape. Between the two
    writes the new machines existed and the old quota refused their pods, so
    the tenant reported "namespace quota has no room for the replacement pod"
    and then scaled — a true diagnosis of a state that was already over.

The numbers below are the ones measured on the stand.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.folders import assert_within_folder_quota
from app.api.v1 import tenants_crud

# poc-transit: two environments and two tenant namespaces, 40+8+7+17 CPU.
NS_QUOTA = {
    "poc-transit-dev": [("40", "80Gi", "500Gi")],
    "poc-transit-prod": [("8", "16Gi", "100Gi")],
    "tenant-op-t1": [("7", "9425408Ki", "60Gi"), (None, None, "100Gi")],
    "tenant-test3": [("17", "11670Mi", "180Gi")],
}
FOLDERS = {"poc-transit": {"quota": {"cpu": "32", "memory": "64Gi", "storage": "500Gi"}}}


def _k8s():
    k = MagicMock()

    async def list_ns(label_selector=None):
        return [{"name": n} for n in NS_QUOTA]

    async def list_rq(namespace):
        items = []
        for cpu, mem, stor in NS_QUOTA.get(namespace, []):
            hard = {}
            if cpu:
                hard["requests.cpu"] = cpu
            if mem:
                hard["requests.memory"] = mem
            if stor:
                hard["requests.storage"] = stor
            items.append(SimpleNamespace(
                metadata=SimpleNamespace(name=f"{namespace}-quota"),
                spec=SimpleNamespace(hard=hard),
            ))
        return SimpleNamespace(items=items)

    k.list_namespaces = AsyncMock(side_effect=list_ns)
    k.core_api.list_namespaced_resource_quota = AsyncMock(side_effect=list_rq)
    return k


async def _ask(cpu: str) -> None:
    await assert_within_folder_quota(
        _k8s(), FOLDERS, "poc-transit", cpu, None, None,
        exclude_namespace="tenant-test3", asking="tenant 'test3'",
    )


@pytest.mark.asyncio
class TestTheWayOutIsDown:
    async def test_growing_past_the_ceiling_is_still_refused(self) -> None:
        with pytest.raises(HTTPException) as e:
            await _ask("21")
        assert e.value.status_code == 409
        assert "asks for 21" in e.value.detail

    async def test_shrinking_is_allowed_even_with_nothing_free(self) -> None:
        """3 workers → 2. It hands room back; refusing it is a locked door."""
        await _ask("13")

    async def test_standing_still_is_allowed(self) -> None:
        """A scale that changes only memory must not fail on CPU."""
        await _ask("17")

    async def test_a_newcomer_still_meets_the_ceiling(self) -> None:
        """Nothing is excluded for a namespace that holds nothing yet."""
        with pytest.raises(HTTPException) as e:
            await assert_within_folder_quota(
                _k8s(), FOLDERS, "poc-transit", "1", None, None,
                exclude_namespace="tenant-new", asking="tenant 'new'",
            )
        assert e.value.status_code == 409


@pytest.mark.asyncio
class TestHowTheFolderGotOverItsCeilingInTheFirstPlace:
    """The reported incident: 72 CPU of quota under a 32 CPU ceiling.

    Not a hole in the arithmetic — the arithmetic never ran. `poc-transit` is
    a root folder (measured: `parent_id` is null, and it is the only folder in
    the ConfigMap) and it carried no `quota` key at all until one was typed in.
    `_ceiling_holder` climbs to the nearest ancestor that caps the dimension
    and there was none, so the check returned before comparing anything, while
    the tenant namespaces went on being *counted* — which is what produced the
    72 that the folder page now shows.

    Counting is not checking. These two pin the difference, because the state
    they describe is the normal state of every folder created without a quota.
    """

    async def test_a_folder_with_no_ceiling_checks_nothing(self) -> None:
        await assert_within_folder_quota(
            _k8s(), {"poc-transit": {"parent_id": None}}, "poc-transit",
            "999", "999Gi", "9999Gi",
            exclude_namespace="tenant-test3", asking="tenant 'test3'",
        )

    async def test_the_ceiling_is_looked_for_above_as_well(self) -> None:
        """A folder silent about a dimension is not permission for it."""
        folders = {
            "root": {"quota": {"cpu": "32"}},
            "poc-transit": {"parent_id": "root"},
        }
        with pytest.raises(HTTPException) as e:
            await assert_within_folder_quota(
                _k8s(), folders, "poc-transit", "999", None, None,
                exclude_namespace="tenant-test3", asking="tenant 'test3'",
            )
        assert "folder 'root'" in e.value.detail

    async def test_what_a_tenant_is_charged_comes_from_its_namespace_label(
        self,
    ) -> None:
        """The 72 is the sum over label-selected namespaces, tenants included.

        40 + 8 (environments) + 7 + 17 (tenants) — the tenant namespaces carry
        `kubevirt-ui.io/folder`, which is what makes a tenant visible to the
        ceiling at all, and equally what makes it count against it.
        """
        from app.api.v1.folders import _own_env_quota

        totals = await _own_env_quota(_k8s(), "poc-transit")
        assert totals["cpu"] == 72


@pytest.mark.asyncio
class TestRoomBeforeTheShapeThatNeedsIt:
    async def _widen(self, current: dict[str, str], planned: dict[str, str]):
        written: dict[str, str] = {}

        async def _write(k8s, ns, quota):
            written.update(quota)

        k = MagicMock()
        k.core_api.list_namespaced_resource_quota = AsyncMock(
            return_value=SimpleNamespace(items=[SimpleNamespace(
                metadata=SimpleNamespace(name="tenant-test3-quota"),
                spec=SimpleNamespace(hard=current),
            )]),
        )
        original = tenants_crud._write_tenant_quota
        tenants_crud._write_tenant_quota = _write
        try:
            await tenants_crud._widen_tenant_quota(k, "tenant-test3", planned)
        finally:
            tenants_crud._write_tenant_quota = original
        return written

    async def test_a_growing_dimension_is_raised(self) -> None:
        written = await self._widen(
            {"requests.cpu": "9", "requests.memory": "8Gi", "requests.storage": "60Gi"},
            {"cpu": "17", "memory": "12Gi", "storage": "180Gi"},
        )
        assert written["cpu"] == "17"

    async def test_a_shrinking_dimension_keeps_the_larger_figure(self) -> None:
        """Taking room away before the machines are gone is the same window."""
        written = await self._widen(
            {"requests.cpu": "17", "requests.memory": "12Gi", "requests.storage": "180Gi"},
            {"cpu": "9", "memory": "8Gi", "storage": "60Gi"},
        )
        assert written["cpu"] == "17"
        assert written["memory"] == "12Gi"
        assert written["storage"] == "180Gi"

    async def test_a_tenant_with_no_quota_yet_gets_the_planned_one(self) -> None:
        k = MagicMock()
        k.core_api.list_namespaced_resource_quota = AsyncMock(
            return_value=SimpleNamespace(items=[]),
        )
        written: dict[str, str] = {}

        async def _write(k8s, ns, quota):
            written.update(quota)

        original = tenants_crud._write_tenant_quota
        tenants_crud._write_tenant_quota = _write
        try:
            await tenants_crud._widen_tenant_quota(
                k, "tenant-test3", {"cpu": "4", "memory": "8Gi", "storage": "40Gi"},
            )
        finally:
            tenants_crud._write_tenant_quota = original
        assert written == {"cpu": "4", "memory": "8Gi", "storage": "40Gi"}


def test_the_quota_is_widened_before_the_shape_is_written() -> None:
    """Order is the whole fix; a test on the writes alone would not see it."""
    import inspect

    source = inspect.getsource(tenants_crud.scale_tenant)
    widen = source.index("_widen_tenant_quota")
    described = source.index("_write_described_workers")
    rotate = source.index("_rotate_worker_template")
    assert widen < described, "described tenants: room comes first"
    assert widen < rotate, "CAPI tenants: room comes first"
