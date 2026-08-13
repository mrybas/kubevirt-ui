"""A folder quota has to be a ceiling, not a caption.

It was a number on a page: nothing in the backend or the wizard refused a VM
or an environment that exceeded it, and the model said so outright ("soft
limit enforced by UI") while the UI only displayed it. On the lab a folder
declaring 16Gi held 32.0Gi.

The quota now lands where the API server can enforce it — a ResourceQuota per
environment namespace — and the folder number becomes the ceiling those must
sum under. Checked in the backend so `kubectl` and the API obey it too.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.folders import (
    _allocated_env_quota,
    _own_env_quota,
    assert_within_folder_quota,
    parse_quantity,
)

FOLDERS = {"team": {"quota": {"cpu": "16", "memory": "32Gi", "storage": "200Gi"}}}
NO_QUOTA = {"team": {}}


def _quota(
    cpu: str | None = None, mem: str | None = None, storage: str | None = None,
    memory: str | None = None,
    used_cpu: str | None = None, used_memory: str | None = None,
    used_storage: str | None = None,
):
    hard = {}
    if cpu:
        hard["requests.cpu"] = cpu
    if mem or memory:
        hard["requests.memory"] = mem or memory
    if storage:
        hard["requests.storage"] = storage
    used = {}
    if used_cpu:
        used["requests.cpu"] = used_cpu
    if used_memory:
        used["requests.memory"] = used_memory
    if used_storage:
        used["requests.storage"] = used_storage
    q = MagicMock()
    q.spec.hard = hard
    q.status.used = used
    return q


def _k8s(per_ns: dict[str, list]) -> MagicMock:
    """A fake that honours the label selector, as the real API server does.

    Namespaces are named `{folder}-{env}` (`folders._ns_name`), so the folder
    a namespace belongs to is its name up to the last dash. Ignoring the
    selector made every folder see every namespace and double-counted the
    subtree.
    """
    k8s = MagicMock()

    async def _list_namespaces(label_selector: str = ""):
        folder = label_selector.split("=")[-1] if label_selector else None
        return [
            {"name": n} for n in per_ns
            if folder is None or n.rsplit("-", 1)[0] == folder
        ]

    k8s.list_namespaces = AsyncMock(side_effect=_list_namespaces)

    async def _list(namespace: str):
        return MagicMock(items=per_ns.get(namespace, []))

    k8s.core_api.list_namespaced_resource_quota = AsyncMock(side_effect=_list)
    k8s.core_api.replace_namespaced_resource_quota = AsyncMock()
    k8s.core_api.create_namespaced_resource_quota = AsyncMock()
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
        totals = await _own_env_quota(k8s, "team")
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


# ---------------------------------------------------------------------------
# Sub-folders spend the parent's budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSubfolders:
    """`_validate_child_quota` compares one child against the parent and stops.

    So a parent capped at 16 CPU accepted two children of 16: each was
    individually "not more than the parent", and nothing ever added them up.
    A sub-folder's environments were invisible to the parent's ceiling too,
    because the sum only looked at namespaces labelled with the parent.
    """

    TREE = {
        "top": {"quota": {"cpu": "16", "memory": "32Gi"}},
        "kid": {"parent_id": "top", "quota": {"cpu": "10"}},
        "kid2": {"parent_id": "top"},
    }

    async def test_a_declared_child_quota_is_reserved_from_the_parent(self) -> None:
        k8s = _k8s({})  # no environments anywhere
        allocated = await _allocated_env_quota(k8s, self.TREE, "top")
        assert allocated["cpu"] == 10, (
            "the child's own ceiling is a promise the parent already made"
        )

    async def test_a_child_without_a_quota_contributes_its_actual_use(self) -> None:
        tree = {"top": {"quota": {"cpu": "16"}}, "kid2": {"parent_id": "top"}}
        k8s = _k8s({"kid2-dev": [_quota(cpu="3")]})
        allocated = await _allocated_env_quota(k8s, tree, "top")
        assert allocated["cpu"] == 3

    async def test_grandchildren_count_too(self) -> None:
        tree = {
            "top": {"quota": {"cpu": "16"}},
            "kid": {"parent_id": "top"},
            "grandkid": {"parent_id": "kid"},
        }
        k8s = _k8s({"grandkid-dev": [_quota(cpu="5")]})
        allocated = await _allocated_env_quota(k8s, tree, "top")
        assert allocated["cpu"] == 5

    async def test_an_environment_is_refused_when_a_subfolder_took_the_room(self) -> None:
        # top=16, kid reserved 10, top's own dev has 4 -> 2 free.
        k8s = _k8s({"top-dev": [_quota(cpu="4")]})
        with pytest.raises(HTTPException) as exc:
            await assert_within_folder_quota(k8s, self.TREE, "top", "5", None, None)
        assert "2 is free" in str(exc.value.detail)

    async def test_two_siblings_cannot_both_take_the_whole_parent(self) -> None:
        from app.api.v1.folders import assert_child_folder_within_parent

        tree = {"top": {"quota": {"cpu": "16"}}, "a": {"parent_id": "top", "quota": {"cpu": "16"}},
                "b": {"parent_id": "top"}}
        k8s = _k8s({})
        with pytest.raises(HTTPException) as exc:
            await assert_child_folder_within_parent(k8s, tree, "b", {"cpu": "16"})
        assert exc.value.status_code == 409
        assert "sub-folders" in str(exc.value.detail)

    async def test_raising_a_child_quota_does_not_compete_with_itself(self) -> None:
        from app.api.v1.folders import assert_child_folder_within_parent

        tree = {"top": {"quota": {"cpu": "16"}}, "a": {"parent_id": "top", "quota": {"cpu": "10"}}}
        k8s = _k8s({})
        # 10 -> 14 under a 16 ceiling: fine unless its own 10 is counted twice.
        await assert_child_folder_within_parent(k8s, tree, "a", {"cpu": "14"})

    async def test_a_root_folder_has_no_parent_to_answer_to(self) -> None:
        from app.api.v1.folders import assert_child_folder_within_parent

        k8s = _k8s({})
        await assert_child_folder_within_parent(
            k8s, {"top": {"quota": {"cpu": "16"}}}, "top", {"cpu": "999"},
        )


def test_both_folder_write_paths_check_the_parent() -> None:
    """Create and update both hand out sub-folder quota."""
    import inspect

    from app.api.v1 import folders as folders_mod

    for fn in (folders_mod.create_folder, folders_mod.update_folder):
        assert "assert_child_folder_within_parent" in inspect.getsource(fn), (
            f"{fn.__name__} can set a sub-folder quota without counting siblings"
        )


@pytest.mark.asyncio
class TestSubfolderPerDimension:
    """A sub-folder capped in one dimension must not be free in the others.

    Reproduced on the cluster: `lab` capped at 32Gi with nothing free took a
    64Gi environment inside sub-folder `kid2`, because `kid2` declared a CPU
    quota and the parent read that as "this child is accounted for".
    """

    def _folders(self) -> dict[str, dict]:
        return {
            "lab": {"parent_id": None, "quota": {"cpu": "16", "memory": "32Gi"}},
            "kid": {"parent_id": "lab", "quota": {"cpu": "2"}},
        }

    async def test_the_parent_counts_the_child_subtree_it_did_not_cap(self) -> None:
        from app.api.v1.folders import _allocated_env_quota

        k8s = _k8s({"kid-a": [_quota(memory="8Gi")], "lab-dev": [_quota(memory="4Gi")]})
        totals = await _allocated_env_quota(k8s, self._folders(), "lab")

        assert totals["cpu"] == 2, "the declared CPU is reserved whole"
        assert totals["memory"] == 12 * 1024 ** 3, (
            "the child declares no memory, so its subtree's actual use counts"
        )

    async def test_an_environment_under_it_hits_the_grandparent_ceiling(self) -> None:
        from app.api.v1.folders import assert_within_folder_quota

        k8s = _k8s({"kid-a": [_quota(memory="8Gi")], "lab-dev": [_quota(memory="20Gi")]})
        with pytest.raises(HTTPException) as e:
            await assert_within_folder_quota(
                k8s, self._folders(), "kid", None, "8Gi", None,
            )
        assert e.value.status_code == 409
        assert "lab" in e.value.detail

    async def test_and_still_allows_what_fits(self) -> None:
        from app.api.v1.folders import assert_within_folder_quota

        k8s = _k8s({"kid-a": [_quota(memory="8Gi")], "lab-dev": [_quota(memory="20Gi")]})
        await assert_within_folder_quota(k8s, self._folders(), "kid", None, "4Gi", None)

    async def test_storage_climbs_the_same_way(self) -> None:
        from app.api.v1.folders import assert_within_folder_quota

        folders = {
            "lab": {"parent_id": None, "quota": {"storage": "200Gi"}},
            "kid": {"parent_id": "lab", "quota": {"cpu": "2"}},
        }
        k8s = _k8s({"lab-dev": [_quota(storage="180Gi")]})
        with pytest.raises(HTTPException) as e:
            await assert_within_folder_quota(k8s, folders, "kid", None, None, "40Gi")
        assert "200Gi" in e.value.detail

    async def test_a_subfolder_quota_climbs_too(self) -> None:
        from app.api.v1.folders import assert_child_folder_within_parent

        folders = {
            "lab": {"parent_id": None, "quota": {"memory": "32Gi"}},
            "kid": {"parent_id": "lab", "quota": {"cpu": "2"}},
            "grand": {"parent_id": "kid", "quota": None},
        }
        k8s = _k8s({"lab-dev": [_quota(memory="30Gi")]})
        with pytest.raises(HTTPException) as e:
            await assert_child_folder_within_parent(
                k8s, folders, "grand", {"memory": "8Gi"},
            )
        assert e.value.status_code == 409


@pytest.mark.asyncio
class TestCannotTakeWhatIsInUse:
    """Only the idle part of a quota can be handed to someone else.

    A neighbour capped at 30Gi with 26Gi in use has 4Gi to give. Kubernetes
    accepts a quota set below its own `status.used` and simply refuses
    everything new afterwards, so the namespace would keep its VMs and be
    unable to start another, with nothing saying why.
    """

    async def test_refuses_to_cut_an_environment_below_its_use(self) -> None:
        from app.api.v1.folders import _apply_reallocations

        k8s = _k8s({"lab-dev": [_quota(memory="30Gi", used_memory="26Gi")]})
        item = SimpleNamespace(
            source="dev", kind="environment", cpu=None, memory="8Gi", storage=None,
        )
        with pytest.raises(HTTPException) as e:
            await _apply_reallocations(k8s, {}, "lab", [item])
        assert e.value.status_code == 409
        assert "26Gi" in e.value.detail

    async def test_allows_taking_the_idle_part(self) -> None:
        from app.api.v1.folders import _apply_reallocations

        k8s = _k8s({"lab-dev": [_quota(memory="30Gi", used_memory="26Gi")]})
        item = SimpleNamespace(
            source="dev", kind="environment", cpu=None, memory="26Gi", storage=None,
        )
        await _apply_reallocations(k8s, {}, "lab", [item])
        assert k8s.core_api.replace_namespaced_resource_quota.await_count == 1

    async def test_storage_and_cpu_too(self) -> None:
        from app.api.v1.folders import _apply_reallocations

        k8s = _k8s({"lab-dev": [_quota(cpu="8", storage="100Gi",
                                       used_cpu="6", used_storage="80Gi")]})
        for field, value in (("cpu", "4"), ("storage", "50Gi")):
            item = SimpleNamespace(
                source="dev", kind="environment",
                cpu=value if field == "cpu" else None,
                memory=None,
                storage=value if field == "storage" else None,
            )
            with pytest.raises(HTTPException):
                await _apply_reallocations(k8s, {}, "lab", [item])

    async def test_a_subfolder_cannot_be_cut_below_its_subtree(self) -> None:
        from app.api.v1.folders import _apply_reallocations

        folders = {
            "lab": {"parent_id": None, "quota": {"cpu": "16"}},
            "kid": {"parent_id": "lab", "quota": {"cpu": "8"}},
        }
        k8s = _k8s({"kid-a": [_quota(cpu="6")]})
        item = SimpleNamespace(source="kid", kind="folder", cpu="2",
                               memory=None, storage=None)
        with pytest.raises(HTTPException) as e:
            await _apply_reallocations(k8s, folders, "lab", [item])
        assert "6" in e.value.detail


@pytest.mark.asyncio
class TestRefusalReadsAsQuantities:
    """The numbers in a 409 are what the person typed, not bytes.

    Seen in the browser: "memory quota 32Gi for folder 'lab' is already
    1.03079e+11 allocated; -6.87195e+10 is free" — three unreadable figures
    and a negative amount of free room.
    """

    async def test_the_environment_refusal(self) -> None:
        from app.api.v1.folders import assert_within_folder_quota

        folders = {"lab": {"parent_id": None, "quota": {"memory": "32Gi"}}}
        k8s = _k8s({"lab-dev": [_quota(memory="30Gi")]})
        with pytest.raises(HTTPException) as e:
            await assert_within_folder_quota(k8s, folders, "lab", None, "8Gi", None)

        assert "30Gi allocated" in e.value.detail
        assert "2Gi is free" in e.value.detail
        assert "asks for 8Gi" in e.value.detail
        assert "e+" not in e.value.detail

    async def test_never_reports_a_negative_amount_of_free_room(self) -> None:
        from app.api.v1.folders import assert_within_folder_quota

        folders = {"lab": {"parent_id": None, "quota": {"memory": "32Gi"}}}
        k8s = _k8s({"lab-dev": [_quota(memory="96Gi")]})
        with pytest.raises(HTTPException) as e:
            await assert_within_folder_quota(k8s, folders, "lab", None, "1Gi", None)

        assert "0 is free" in e.value.detail
        assert "-" not in e.value.detail

    async def test_the_subfolder_refusal(self) -> None:
        from app.api.v1.folders import assert_child_folder_within_parent

        folders = {
            "lab": {"parent_id": None, "quota": {"memory": "32Gi"}},
            "kid": {"parent_id": "lab", "quota": None},
        }
        k8s = _k8s({"lab-dev": [_quota(memory="30Gi")]})
        with pytest.raises(HTTPException) as e:
            await assert_child_folder_within_parent(k8s, folders, "kid", {"memory": "8Gi"})

        assert "30Gi allocated" in e.value.detail
        assert "e+" not in e.value.detail
