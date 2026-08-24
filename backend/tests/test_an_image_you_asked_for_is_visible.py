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

from app.api.v1.templates import _described_but_unbuilt_images


def _described(name: str, *, phase: str = "", conditions: list | None = None,
               size: str = "100Gi", ns: str = "poc-transit-dev") -> dict:
    return {
        "metadata": {
            "name": name, "namespace": ns,
            "creationTimestamp": "2026-08-22T11:45:55Z",
            "labels": {"kubevirt-ui.io/disk-type": "data",
                       "kubevirt-ui.io/persistent": "true"},
        },
        "spec": {"displayName": "Over quota", "size": size,
                 "source": {"url": "http://example.test/x.qcow2"}},
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

    from app.api.v1.templates import list_golden_images

    source = inspect.getsource(list_golden_images)
    assert "_described_but_unbuilt_images" in source
    assert source.index("datavolumes") < source.index("_described_but_unbuilt_images")
