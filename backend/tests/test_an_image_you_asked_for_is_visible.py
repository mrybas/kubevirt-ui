"""An image that was asked for shows up before it is built — and if it never is.

The list reads DataVolumes. A DataVolume is what the operator *makes* from a
ManagedImage, so the seconds between the request and the disk show nothing,
and a request that can never be satisfied shows nothing for ever.

UAT run 4, G3: a 100Gi disk against a 12Gi quota. No row in any list, no
error, no trace — while the object consumed the quota that refused it, and
later, when the quota was raised, quietly bound 100GiB of Ceph for a disk
nobody could see and nobody had successfully ordered. The tester deleted the
DataVolume by hand and the operator built it again, correctly: the request
was still there.

So the list shows what was asked for as well as what exists, and carries
whatever the resource says about itself, so "where is my disk" is answered on
the page instead of in a controller's log.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.images import _described_but_unbuilt_images


def _described(name: str, *, phase: str = "", conditions: list | None = None,
               size: str = "100Gi", ns: str = "poc-transit-dev",
               source: dict | None = None) -> dict:
    return {
        "metadata": {
            "name": name, "namespace": ns,
            "creationTimestamp": "2026-08-22T11:45:55Z",
            "labels": {"kubevirt-ui.io/disk-type": "data",
                       "kubevirt-ui.io/persistent": "true"},
        },
        "spec": {"displayName": "Over quota", "size": size,
                 # NB: the flat {"url": ...} form. It is NOT what
                 # `_create_managed_image` writes — that writes CDI's nested
                 # shape — and this fixture's unrealistic default is why a
                 # registry source went untested for so long. Kept as the
                 # default because older resources carry it; the tests below
                 # pass the real shapes explicitly.
                 "source": source or {"url": "http://example.test/x.qcow2"}},
        "status": {"phase": phase, "conditions": conditions or []},
    }


def _api(items: list[dict]):
    api = MagicMock()
    api.list_namespaced_custom_object = AsyncMock(return_value={"items": items})
    return api


async def _rows(items, already=frozenset(), filter_ns=None):
    return await _described_but_unbuilt_images(
        _api(items), ["poc-transit-dev"], filter_ns, set(already),
        {"poc-transit-dev": {"kubevirt-ui.io/environment": "dev"}},
    )


@pytest.mark.asyncio
class TestTheAskedForIsVisible:
    async def test_a_request_with_no_disk_yet_is_a_row(self) -> None:
        rows = await _rows([_described("uat-over-g9gd6")])
        assert [r.name for r in rows] == ["uat-over-g9gd6"]
        assert rows[0].status == "Pending"
        assert rows[0].size == "100Gi"

    async def test_a_request_that_cannot_be_satisfied_says_so(self) -> None:
        rows = await _rows([_described("uat-over-g9gd6", conditions=[{
            "type": "Ready", "status": "False", "reason": "QuotaExceeded",
            "message": "persistentvolumeclaims is forbidden: exceeded quota",
        }])])
        assert rows[0].status == "Error"
        assert "exceeded quota" in rows[0].error_message

    async def test_one_that_is_built_is_not_listed_twice(self) -> None:
        rows = await _rows([_described("uat-ubuntu-dkvpb")],
                           already={"poc-transit-dev/uat-ubuntu-dkvpb"})
        assert rows == []

    async def test_the_environment_comes_from_the_namespace(self) -> None:
        rows = await _rows([_described("x")])
        assert rows[0].environment == "dev"

    async def test_an_unknown_phase_reads_as_pending_not_as_a_status(self) -> None:
        rows = await _rows([_described("x", phase="ImportScheduled")])
        assert rows[0].status == "Pending"

    async def test_a_sibling_namespace_is_filtered_out_like_a_disk_is(self) -> None:
        rows = await _rows([_described("x", ns="poc-transit-dev")],
                           filter_ns="poc-transit-prod")
        assert rows == []

    async def test_a_cluster_with_no_such_crd_is_not_an_error(self) -> None:
        from kubernetes_asyncio.client.rest import ApiException

        api = MagicMock()
        api.list_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=404))
        rows = await _described_but_unbuilt_images(
            api, ["poc-transit-dev"], None, set(), {},
        )
        assert rows == []


def test_the_lister_asks_for_both() -> None:
    """The property is that the list is a union, not that a helper exists."""
    import inspect

    from app.api.v1.images import list_golden_images

    source = inspect.getsource(list_golden_images)
    assert "_described_but_unbuilt_images" in source
    assert source.index("datavolumes") < source.index("_described_but_unbuilt_images")


class TestTheSourceUrlOfSomethingNotBuiltYet:
    """`source.get("url") or source.get("registry")` was wrong twice over.

    `_create_managed_image` writes CDI's nested source dict, so the first half
    matched nothing and the second returned a **dict** for a registry source.
    `VMImage.source_url` is `str | None`, so that is a Pydantic
    ValidationError raised inside the list handler's `try` — whose only
    `except` is `ApiException`. It escaped as a 500 that took out the entire
    image list, in every namespace, for as long as one registry-sourced
    ManagedImage stayed unbuilt.
    """

    @pytest.mark.asyncio
    async def test_a_registry_source_does_not_500_the_whole_list(self) -> None:
        """The regression, stated as what the user would have seen: not a
        broken row — no list at all."""
        rows = await _rows([
            _described("catalogue-image", source={
                "registry": {
                    "url": "docker://harbor.example/vm-images-tenant-a/ubuntu:1",
                    "secretRef": "harbor-robot",
                    "certConfigMap": "harbor-ca",
                },
            }),
        ])

        assert len(rows) == 1
        assert rows[0].source_url == (
            "docker://harbor.example/vm-images-tenant-a/ubuntu:1"
        )

    @pytest.mark.asyncio
    async def test_an_unbuilt_catalogue_image_still_joins_its_catalogue_row(
        self,
    ) -> None:
        """Not just "does not crash" — the URL has to be the merge key.

        `merge()` re-joins a row with its catalogue entry through
        `catalog_ref_from_source_url`. An unbuilt catalogue image whose
        source_url is None (or a dict) shows up as a SECOND row beside the
        catalogue row it was made from, during exactly the window between
        materialising an image and the operator building it.
        """
        from app.api.v1.images_catalog import catalog_ref_from_source_url

        rows = await _rows([
            _described("catalogue-image", source={
                "registry": {
                    "url": "docker://harbor.example/vm-images-public/ubuntu-2204:20260901",
                },
            }),
        ])

        assert catalog_ref_from_source_url(rows[0].source_url) == (
            "vm-images-public/ubuntu-2204:20260901"
        )

    @pytest.mark.asyncio
    async def test_the_nested_http_shape_is_read_too(self) -> None:
        """The shape `_create_managed_image` actually writes for an HTTP
        source. The old expression returned None for this as well — harmless,
        but wrong, and it is why nothing noticed the registry case."""
        rows = await _rows([
            _described("http-image", source={
                "http": {"url": "https://cloud-images.example/x.qcow2"},
            }),
        ])

        assert rows[0].source_url == "https://cloud-images.example/x.qcow2"

    @pytest.mark.asyncio
    async def test_a_pvc_clone_reads_the_same_way_the_built_row_renders_it(
        self,
    ) -> None:
        rows = await _rows([
            _described("clone", source={
                "pvc": {"name": "ubuntu-disk", "namespace": "tenant-a"},
            }),
        ])

        assert rows[0].source_url == "pvc:tenant-a/ubuntu-disk"

    @pytest.mark.asyncio
    async def test_the_legacy_flat_form_still_works(self) -> None:
        """Older resources carry `{"url": ...}` directly."""
        rows = await _rows([_described("legacy")])

        assert rows[0].source_url == "http://example.test/x.qcow2"
