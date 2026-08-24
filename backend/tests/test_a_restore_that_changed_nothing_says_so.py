"""A restore reports what it did, and an environment is one environment.

UAT run 4, E6. A VM backup was taken and restored, and the machine came back
with a disk that had none of the backed-up content. The report's mechanism —
Velero restored a "clone the golden image" manifest and CDI obeyed it — turned
out not to be what happened: the restored DataVolumes carry
`cdi.kubevirt.io/storage.prePopulated`, which tells CDI to adopt the claim
rather than clone anything.

What the objects on the stand actually say:

    existingResourcePolicy: (unset)   → Velero's default: skip what exists
    phase: Completed, errors: 0, warnings: 66
    progress: 279/279

Sixty-six warnings, one per object left exactly as it was because the target
namespace still had it. Nothing was restored, the restore said Completed, and
the product had no screen for restores at all — no phase, no counts, no
warnings — so the only place the truth existed was a CR read by hand.

Two things follow. The caller decides what happens to what is already there,
and the answer comes back where it can be read.

And separately: an environment backup covered three namespaces, because
tenant control-plane namespaces carry the same folder and environment labels
that an environment does.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.velero_backups import (
    VeleroRestoreCreateRequest,
    _namespaces_for_env,
    _parse_restore,
)


def _ns(name: str, labels: dict) -> SimpleNamespace:
    return SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels))


@pytest.mark.asyncio
class TestWhatAnEnvironmentCovers:
    async def _resolve(self, namespaces: list) -> list[str]:
        core = MagicMock()
        core.list_namespace = AsyncMock(
            return_value=SimpleNamespace(items=namespaces))
        k8s = MagicMock()
        with patch("app.api.v1.velero_backups.client.CoreV1Api", return_value=core):
            return await _namespaces_for_env(k8s, "poc-transit", "dev")

    async def test_the_environment_namespace_and_nothing_else(self) -> None:
        got = await self._resolve([
            _ns("poc-transit-dev", {
                "kubevirt-ui.io/folder": "poc-transit",
                "kubevirt-ui.io/environment": "dev",
            }),
            # Tenants are scoped into a folder and an environment too, which is
            # how they ended up in a backup somebody asked to be one env wide.
            _ns("tenant-uat-t1", {
                "kubevirt-ui.io/folder": "poc-transit",
                "kubevirt-ui.io/environment": "dev",
                "kubevirt-ui.io/tenant": "uat-t1",
            }),
            _ns("tenant-uat-t2", {
                "kubevirt-ui.io/folder": "poc-transit",
                "kubevirt-ui.io/environment": "dev",
                "kubevirt-ui.io/tenant": "uat-t2",
            }),
        ])
        assert got == ["poc-transit-dev"]

    async def test_an_environment_with_nothing_in_it_resolves_to_nothing(self) -> None:
        assert await self._resolve([]) == []


class TestWhatARestoreReportsBack:
    def _parsed(self, status: dict, spec: dict | None = None) -> dict:
        return _parse_restore({
            "metadata": {"name": "restore-1", "namespace": "o0-velero"},
            "spec": {"backupName": "uat-vpc-vm-backup", **(spec or {})},
            "status": status,
        })

    def test_the_warnings_come_through(self) -> None:
        """The number that was the whole story, and had nowhere to be shown."""
        parsed = self._parsed({
            "phase": "Completed", "errors": 0, "warnings": 66,
            "progress": {"itemsRestored": 279, "totalItems": 279},
        })
        assert parsed["warnings"] == 66
        assert parsed["items_restored"] == 279
        assert parsed["total_items"] == 279

    def test_the_policy_it_ran_under_comes_through(self) -> None:
        """Reading "Completed" without it does not say whether anything moved."""
        assert self._parsed({}, {"existingResourcePolicy": "update"})[
            "existing_resource_policy"] == "update"

    def test_velero_default_is_reported_as_the_default(self) -> None:
        assert self._parsed({})["existing_resource_policy"] == "none"

    def test_a_restore_still_running_does_not_invent_counts(self) -> None:
        parsed = self._parsed({"phase": "InProgress"})
        assert parsed["items_restored"] == 0 and parsed["total_items"] == 0


class TestTheCallerChooses:
    def test_the_default_is_velero_s_own(self) -> None:
        assert VeleroRestoreCreateRequest().existing_resource_policy == "none"

    def test_overwriting_is_expressible(self) -> None:
        assert VeleroRestoreCreateRequest(
            existing_resource_policy="update").existing_resource_policy == "update"

    def test_nothing_else_is(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            VeleroRestoreCreateRequest(existing_resource_policy="delete-everything")


def test_the_restore_spec_carries_the_choice() -> None:
    import inspect

    from app.api.v1.velero_backups import create_velero_restore

    source = inspect.getsource(create_velero_restore)
    assert "existingResourcePolicy" in source


def test_restores_can_be_read_at_all() -> None:
    """There was no endpoint. A restore is the operation people run when
    something has already gone wrong; it is the last one to be fire-and-forget."""
    from app.api.v1.velero_backups import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/restores" in paths


@pytest.mark.asyncio
class TestWhatTheListingsShow:
    """Every velero listing was `require_auth` and unfiltered.

    A backup names the namespaces it covers, so reading all of them told any
    authenticated user what folders and environments exist and what is in
    them — the same leak as `GET /folders`, in another set of pages. The new
    restores listing would have inherited it.
    """

    async def _visible(self, namespaces: list[str], *, admin: bool) -> bool:
        from app.api.v1.velero_backups import _visible_to

        user = SimpleNamespace(
            email="someone@ipa.test", username="someone",
            groups=["kubevirt-ui-admins"] if admin else ["kv-poc-transit-viewers"],
            is_admin=admin,
        )
        return await _visible_to(MagicMock(), user, namespaces)

    async def test_an_admin_sees_everything(self) -> None:
        assert await self._visible(["poc-transit-dev"], admin=True)
        assert await self._visible([], admin=True)

    async def test_a_cluster_wide_one_is_admin_only(self) -> None:
        """No namespaces named means every namespace, which takes an admin —
        the same answer `_authorise_scope` gives for acting on one."""
        assert not await self._visible([], admin=False)
