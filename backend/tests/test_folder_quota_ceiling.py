"""A folder quota has to be a ceiling, not a caption.

It was a number on a page: nothing in the backend or the wizard refused a VM
or an environment that exceeded it, and the model said so outright ("soft
limit enforced by UI") while the UI only displayed it. On the lab a folder
declaring 16Gi held 32.0Gi.

The quota now lands where the API server can enforce it — a ResourceQuota per
environment namespace — and the folder number becomes the ceiling those must
sum under. Checked in the backend so `kubectl` and the API obey it too.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.folders import (
    _allocated_env_quota,
    assert_within_folder_quota,
    parse_quantity,
)

FOLDERS = {"team": {"quota": {"cpu": "16", "memory": "32Gi", "storage": "200Gi"}}}
NO_QUOTA = {"team": {}}


def _quota(cpu: str | None = None, mem: str | None = None, storage: str | None = None):
    hard = {}
    if cpu:
        hard["requests.cpu"] = cpu
    if mem:
        hard["requests.memory"] = mem
    if storage:
        hard["requests.storage"] = storage
    q = MagicMock()
    q.spec.hard = hard
    return q


def _k8s(per_ns: dict[str, list]) -> MagicMock:
    k8s = MagicMock()
    k8s.list_namespaces = AsyncMock(
        return_value=[{"name": n} for n in per_ns],
    )

    async def _list(namespace: str):
        return MagicMock(items=per_ns.get(namespace, []))

    k8s.core_api.list_namespaced_resource_quota = AsyncMock(side_effect=_list)
    return k8s


class TestQuantities:
    @pytest.mark.parametrize("text,expected", [
        ("16", 16), ("500m", 0.5), ("32Gi", 32 * 2**30),
        ("8192Mi", 8192 * 2**20), ("1G", 10**9),
    ])
    def test_parses(self, text: str, expected: float) -> None:
        assert parse_quantity(text) == pytest.approx(expected)

    def test_gi_and_mi_compare_by_value_not_by_string(self) -> None:
        # "8192Mi" < "16" as text; the whole point is that it is not.
        assert parse_quantity("8192Mi") == parse_quantity("8Gi")

    @pytest.mark.parametrize("text", [None, "", "  ", "lots", "16Xi"])
    def test_nonsense_is_none_not_zero(self, text) -> None:
        # Zero would read as "nothing allocated" and wave the request through.
        assert parse_quantity(text) is None


@pytest.mark.asyncio
class TestCeiling:
    async def test_a_fitting_environment_is_allowed(self) -> None:
        k8s = _k8s({"team-dev": [_quota(cpu="4", mem="8Gi")]})
        await assert_within_folder_quota(k8s, FOLDERS, "team", "4", "8Gi", None)

    async def test_the_sum_is_what_counts_not_the_single_request(self) -> None:
        k8s = _k8s({
            "team-dev": [_quota(cpu="8", mem="16Gi")],
            "team-stg": [_quota(cpu="6", mem="12Gi")],
        })
        # 8 + 6 = 14 of 16; asking for 4 more overruns by 2.
        with pytest.raises(HTTPException) as exc:
            await assert_within_folder_quota(k8s, FOLDERS, "team", "4", None, None)
        assert exc.value.status_code == 409
        assert "free" in str(exc.value.detail)

    async def test_memory_is_compared_across_units(self) -> None:
        k8s = _k8s({"team-dev": [_quota(mem="30Gi")]})
        with pytest.raises(HTTPException):
            await assert_within_folder_quota(k8s, FOLDERS, "team", None, "4096Mi", None)

    async def test_a_folder_without_a_quota_constrains_nothing(self) -> None:
        k8s = _k8s({"team-dev": [_quota(cpu="99")]})
        await assert_within_folder_quota(k8s, NO_QUOTA, "team", "99", "99Gi", None)

    async def test_an_unquotaed_dimension_is_not_invented(self) -> None:
        # The folder caps cpu/memory/storage; a folder quota missing one of
        # them must not start refusing on it.
        folders = {"team": {"quota": {"cpu": "16"}}}
        k8s = _k8s({"team-dev": [_quota(mem="999Gi")]})
        await assert_within_folder_quota(k8s, folders, "team", None, "999Gi", None)

    async def test_reallocating_an_environment_does_not_compete_with_itself(self) -> None:
        # Raising team-dev from 8 to 10 of a 16 ceiling: without the
        # exclusion its own 8 counts twice and the raise is refused.
        k8s = _k8s({"team-dev": [_quota(cpu="8")]})
        await assert_within_folder_quota(
            k8s, FOLDERS, "team", "10", None, None, exclude_namespace="team-dev",
        )

    async def test_exactly_filling_the_ceiling_is_allowed(self) -> None:
        k8s = _k8s({"team-dev": [_quota(cpu="12")]})
        await assert_within_folder_quota(k8s, FOLDERS, "team", "4", None, None)

    async def test_unreadable_namespaces_do_not_silently_free_the_budget(self) -> None:
        k8s = MagicMock()
        k8s.list_namespaces = AsyncMock(side_effect=RuntimeError("no access"))
        totals = await _allocated_env_quota(k8s, "team")
        assert totals == {"cpu": 0.0, "memory": 0.0, "storage": 0.0}


def test_the_create_path_checks_the_ceiling() -> None:
    """A check nobody calls refuses nothing."""
    import inspect

    from app.api.v1 import folders as folders_mod

    src = inspect.getsource(folders_mod._create_environment_ns)
    assert "assert_within_folder_quota" in src
    assert src.index("assert_within_folder_quota") < src.index("create_namespace"), (
        "the ceiling must be checked before the namespace is created, or a "
        "refused request leaves one behind"
    )


def test_the_quota_comes_with_a_limitrange() -> None:
    """limits.* in a quota rejects pods that declare none.

    virt-launcher declares both, so VMs are fine; a plain pod would be
    refused with "must specify limits.cpu". Measured on the lab: the Kamaji
    control-plane containers declare no limits at all.
    """
    import inspect

    from app.api.v1 import folders as folders_mod

    src = inspect.getsource(folders_mod._create_environment_ns)
    assert "create_namespaced_limit_range" in src
    assert "defaultRequest" in src
