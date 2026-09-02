"""A disk the quota has no room for is refused, out loud, before it exists.

UAT run 4, G3: a 100Gi disk against a 12Gi environment quota with 10Gi used.
`POST` answered 201, the dialog closed the way it closes on success, the disk
was nowhere in the list — and the cluster kept a Pending DataVolume with
`ErrExceededQuota` that no screen showed and that went on counting against
the quota. The plan asked for an honest error in human language; the product
gave the one answer worse than a 500, which is "yes".

The mechanism is not subtle once seen: the quota counts PersistentVolumeClaims
and a DataVolume is not one. CDI makes the claim a moment later, the API
server refuses *that*, and by then the request has been answered.

So the room is asked for first. The API server is still the enforcer — this
can be raced, and `status.used` is updated asynchronously — but a refusal is
now an answer to the request instead of an object nobody is looking at.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.storage_headroom import assert_storage_headroom


def _k8s(quotas: list[tuple[str | None, str | None]]):
    k8s = MagicMock()
    items = [
        SimpleNamespace(
            spec=SimpleNamespace(hard={"requests.storage": hard} if hard else {}),
            status=SimpleNamespace(used={"requests.storage": used} if used else {}),
        )
        for hard, used in quotas
    ]
    k8s.core_api.list_namespaced_resource_quota = AsyncMock(
        return_value=SimpleNamespace(items=items))
    return k8s


@pytest.mark.asyncio
class TestTheRoomIsAskedForFirst:
    async def test_the_reported_case_is_refused(self) -> None:
        with pytest.raises(HTTPException) as e:
            await assert_storage_headroom(
                _k8s([("12Gi", "10Gi")]), "poc-transit-dev", "100Gi",
                what="'uat-over'",
            )
        assert e.value.status_code == 409
        # The numbers are the actionable part; a bare "quota exceeded" leaves
        # the person to go and look them up.
        for fragment in ("12Gi", "10Gi", "2Gi", "100Gi", "poc-transit-dev"):
            assert fragment in e.value.detail, e.value.detail

    async def test_what_fits_is_not_refused(self) -> None:
        await assert_storage_headroom(_k8s([("12Gi", "10Gi")]), "ns", "1Gi")

    async def test_exactly_the_free_space_fits(self) -> None:
        """Off-by-one here would refuse the last disk that does fit."""
        await assert_storage_headroom(_k8s([("12Gi", "10Gi")]), "ns", "2Gi")

    async def test_a_namespace_with_no_quota_constrains_nothing(self) -> None:
        await assert_storage_headroom(_k8s([]), "ns", "500Gi")

    async def test_a_quota_that_caps_something_else_is_not_a_storage_cap(self) -> None:
        await assert_storage_headroom(_k8s([(None, None)]), "ns", "500Gi")

    async def test_the_tightest_of_several_quotas_binds(self) -> None:
        """Kubernetes satisfies every quota, so the smallest room is the room."""
        with pytest.raises(HTTPException):
            await assert_storage_headroom(
                _k8s([("500Gi", "0"), ("20Gi", "19Gi")]), "ns", "5Gi",
            )

    async def test_a_quota_that_cannot_be_read_has_not_said_no(self) -> None:
        from kubernetes_asyncio.client.rest import ApiException

        k8s = MagicMock()
        k8s.core_api.list_namespaced_resource_quota = AsyncMock(
            side_effect=ApiException(status=403))
        await assert_storage_headroom(k8s, "ns", "500Gi")

    async def test_used_missing_is_treated_as_nothing_used(self) -> None:
        await assert_storage_headroom(_k8s([("12Gi", None)]), "ns", "12Gi")


def test_every_path_that_makes_a_disk_asks_first() -> None:
    """Three endpoints create DataVolumes, and one of them being right is how
    this stayed alive: the wizard used a different one from the API docs."""
    import inspect

    from app.api.v1.disks import create_persistent_disk
    from app.api.v1.storage import create_datavolume
    from app.api.v1.images import create_golden_image

    for fn in (create_persistent_disk, create_datavolume, create_golden_image):
        source = inspect.getsource(fn)
        assert "assert_storage_headroom" in source, fn.__name__
        # Before the object, or it is the same silent success with extra steps.
        assert source.index("assert_storage_headroom") < source.index(
            "create_namespaced_custom_object"
        ), fn.__name__
