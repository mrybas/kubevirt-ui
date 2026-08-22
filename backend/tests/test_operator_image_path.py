"""Golden images routed through the operator instead of written directly.

Two claims are worth guarding, and neither is about the happy path.

The first is that the flag switches the *writer* and nothing else: same
request in, same response shape out, same naming rule, so the UI cannot tell
which side of the flag it is on and turning the flag off restores the old
behaviour exactly.

The second is that deletion follows *ownership*, not the flag. A disk created
while the flag was on stays owned by its ManagedImage after the flag goes off;
deleting the disk directly would only make the controller rebuild it, and the
user would watch a deleted image come back.
"""

from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.exceptions import ApiException

from app.models.template import GoldenImageCreate


def _k8s_with_namespace(project: str | None = "opdev") -> MagicMock:
    k8s = MagicMock()
    ns = MagicMock()
    ns.metadata.labels = {"kubevirt-ui.io/project": project} if project else {}
    k8s.core_api.read_namespace = AsyncMock(return_value=ns)
    # Creating a disk asks the namespace's quota for room first; no quota
    # here means nothing constrains these tests.
    k8s.core_api.list_namespaced_resource_quota = AsyncMock(
        return_value=SimpleNamespace(items=[]))
    return k8s


async def _create_image(
    *,
    flag_on: bool,
    image: GoldenImageCreate,
    create_side_effect: Any = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Any]:
    """Call the create endpoint and return (captured create call, response)."""
    from app.api.v1 import templates

    monkeypatch.setenv("OPERATOR_IMAGE_ENABLED", "true" if flag_on else "")

    captured: dict[str, Any] = {}

    async def _create(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        if create_side_effect is not None:
            raise create_side_effect
        body = dict(kwargs["body"])
        body["metadata"] = dict(body["metadata"])
        # The API server turns generateName into a name, exactly as it does for
        # the DataVolume path today.
        body["metadata"]["name"] = body["metadata"].get("generateName", "") + "x7k2p"
        body["metadata"]["creationTimestamp"] = "2026-08-20T00:00:00Z"
        return body

    api = MagicMock()
    api.create_namespaced_custom_object = AsyncMock(side_effect=_create)

    request = MagicMock()
    request.app.state.k8s_client = _k8s_with_namespace()

    with patch.object(templates.client, "CustomObjectsApi", return_value=api):
        response = await templates.create_golden_image(
            image=image, request=request, user=MagicMock(), namespace="opdev-dev",
        )
    return captured, response


def _image() -> GoldenImageCreate:
    return GoldenImageCreate(
        display_name="Ubuntu 24.04 UI",
        source_url="https://cloud-images.ubuntu.com/noble/current/noble.img",
        size="10Gi",
        os_type="linux",
        scope="environment",
    )


@pytest.mark.asyncio
class TestWhichObjectGetsWritten:
    async def test_flag_off_still_writes_the_datavolume(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _ = await _create_image(
            flag_on=False, image=_image(), monkeypatch=monkeypatch,
        )
        assert captured["plural"] == "datavolumes"
        assert captured["group"] == "cdi.kubevirt.io"

    async def test_flag_on_writes_a_managed_image_instead(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _ = await _create_image(
            flag_on=True, image=_image(), monkeypatch=monkeypatch,
        )
        assert captured["plural"] == "managedimages"
        assert captured["group"] == "platform.kubevirt-ui.io"
        # Nothing else may be written on this path: the operator owns the disk.
        assert captured["body"]["kind"] == "ManagedImage"

    async def test_the_naming_rule_does_not_change_with_the_flag(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The generated name has the same shape on both sides of the flag.

        The disk is named after the resource, so if the resource were named
        differently every disk name in the cluster would change the day the flag
        flipped — and every UI that read a name back would be looking at a
        stranger.
        """
        off, _ = await _create_image(
            flag_on=False, image=_image(), monkeypatch=monkeypatch,
        )
        on, _ = await _create_image(
            flag_on=True, image=_image(), monkeypatch=monkeypatch,
        )
        assert on["body"]["metadata"]["generateName"] == \
            off["body"]["metadata"]["generateName"] == "ubuntu-24-04-ui-"

    async def test_the_response_looks_the_same_to_the_caller(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, response = await _create_image(
            flag_on=True, image=_image(), monkeypatch=monkeypatch,
        )
        assert response.name == "ubuntu-24-04-ui-x7k2p"
        assert response.namespace == "opdev-dev"
        assert response.display_name == "Ubuntu 24.04 UI"
        assert response.size == "10Gi"
        assert response.status == "Pending"

    async def test_the_request_survives_the_translation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        img = _image()
        img.storage_class = "ceph-block"
        img.description = "base image"
        img.os_version = "24.04"
        img.persistent = True
        img.disk_type = "data"
        captured, _ = await _create_image(
            flag_on=True, image=img, monkeypatch=monkeypatch,
        )
        spec = captured["body"]["spec"]
        assert spec["source"] == {
            "http": {"url": "https://cloud-images.ubuntu.com/noble/current/noble.img"},
        }
        assert spec["size"] == "10Gi"
        assert spec["storageClass"] == "ceph-block"
        assert spec["description"] == "base image"
        assert spec["osType"] == "linux"
        assert spec["osVersion"] == "24.04"
        assert spec["persistent"] is True
        assert spec["diskType"] == "data"

    async def test_an_unset_storage_class_is_not_invented(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No class asked for means no class pinned — the cluster default wins.

        Writing a concrete class here would pin the disk to whichever class
        happened to be default the day it was created, which is not what "use
        the default" means.
        """
        captured, _ = await _create_image(
            flag_on=True, image=_image(), monkeypatch=monkeypatch,
        )
        assert "storageClass" not in captured["body"]["spec"]

    async def test_a_missing_crd_says_so(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag on, CRD absent: the answer must name the cause.

        A generic failure here reads as "the import broke" and sends whoever
        gets it looking at CDI, the URL and the storage class — none of which
        are the problem.
        """
        with pytest.raises(HTTPException) as caught:
            await _create_image(
                flag_on=True,
                image=_image(),
                create_side_effect=ApiException(status=404, reason="Not Found"),
                monkeypatch=monkeypatch,
            )
        assert caught.value.status_code == 503
        assert "ManagedImage CRD" in caught.value.detail


def _delete_harness(
    *,
    dv_labels: dict[str, str] | None,
    managed_image: dict[str, Any] | None = None,
    managed_image_missing: bool = False,
) -> tuple[MagicMock, list[dict[str, Any]]]:
    """Build a CustomObjectsApi mock for the delete path."""
    deletes: list[dict[str, Any]] = []

    async def _get(**kwargs: Any) -> dict[str, Any]:
        if kwargs["plural"] == "datavolumes":
            if dv_labels is None:
                raise ApiException(status=404, reason="Not Found")
            return {"metadata": {"name": kwargs["name"], "labels": dv_labels}}
        if kwargs["plural"] == "managedimages":
            if managed_image_missing:
                raise ApiException(status=404, reason="Not Found")
            return managed_image or {"metadata": {"name": kwargs["name"]}, "status": {}}
        raise AssertionError(f"unexpected get of {kwargs['plural']}")

    async def _delete(**kwargs: Any) -> dict[str, Any]:
        deletes.append(kwargs)
        return {}

    api = MagicMock()
    api.get_namespaced_custom_object = AsyncMock(side_effect=_get)
    api.delete_namespaced_custom_object = AsyncMock(side_effect=_delete)
    return api, deletes


async def _delete_image(api: MagicMock, name: str = "ubuntu-x7k2p") -> None:
    from app.api.v1 import templates

    request = MagicMock()
    request.app.state.k8s_client = MagicMock()
    with patch.object(templates.client, "CustomObjectsApi", return_value=api):
        await templates.delete_golden_image(
            name=name, request=request, user=MagicMock(), namespace="opdev-dev",
        )


@pytest.mark.asyncio
class TestDeletionFollowsOwnership:
    async def test_an_unowned_disk_is_deleted_directly(self) -> None:
        api, deletes = _delete_harness(dv_labels={"kubevirt-ui.io/managed": "true"})
        await _delete_image(api)
        assert [d["plural"] for d in deletes] == ["datavolumes"]

    async def test_an_owned_disk_is_released_through_its_resource(self) -> None:
        """Deleting the disk directly would only make the controller rebuild it."""
        api, deletes = _delete_harness(
            dv_labels={
                "platform.kubevirt-ui.io/owner-kind": "ManagedImage",
                "platform.kubevirt-ui.io/owner-name": "ubuntu-x7k2p",
            },
        )
        await _delete_image(api)
        assert [d["plural"] for d in deletes] == ["managedimages"]

    async def test_an_image_in_use_is_refused_by_name(self) -> None:
        api, deletes = _delete_harness(
            dv_labels={
                "platform.kubevirt-ui.io/owner-kind": "ManagedImage",
                "platform.kubevirt-ui.io/owner-name": "ubuntu-x7k2p",
            },
            managed_image={
                "metadata": {"name": "ubuntu-x7k2p"},
                "status": {"usedBy": ["opdev-dev/web-01", "opdev-dev/web-02"]},
            },
        )
        with pytest.raises(HTTPException) as caught:
            await _delete_image(api)
        assert caught.value.status_code == 409
        assert "web-01" in caught.value.detail
        assert "web-02" in caught.value.detail
        # And nothing was deleted on the way to refusing.
        assert deletes == []

    async def test_a_disk_whose_owner_is_gone_is_deleted_directly(self) -> None:
        """A dangling ownership label must not strand the disk forever."""
        api, deletes = _delete_harness(
            dv_labels={
                "platform.kubevirt-ui.io/owner-kind": "ManagedImage",
                "platform.kubevirt-ui.io/owner-name": "ubuntu-x7k2p",
            },
            managed_image_missing=True,
        )
        await _delete_image(api)
        assert [d["plural"] for d in deletes] == ["datavolumes"]
