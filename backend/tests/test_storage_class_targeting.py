"""Unit tests for where cloned disks and new VM disks land.

Storage here is tiered: templates and images sit on an erasure-coded class
(cheap, 1.5x), VM disks belong on the replicated one (measured 4x faster
sequential, 2.6x random 4K). Inheriting the source's class sent every write a
VM makes to erasure coding — the exact opposite of the intent.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.vms import VMFromTemplateRequest
from app.models.template import CreateImageFromDiskRequest

@pytest.fixture(autouse=True)
def _namespace_access_is_not_what_these_prove(monkeypatch: pytest.MonkeyPatch) -> None:
    """These call the image handlers directly to inspect what they WRITE.

    The handlers refuse a namespace the caller has no binding in, which needs a
    cluster's worth of RBAC to satisfy and has nothing to do with the object
    under inspection here. It is proven once, deliberately, in
    `test_an_image_endpoint_refuses_someone_elses_namespace.py`; making thirty
    more tests re-prove it would only mean thirty places to weaken it from.
    """
    from app.api.v1 import images

    async def _allow(request, user, namespace) -> None:
        return None

    monkeypatch.setattr(images, "require_namespace_access", _allow)



def _source_pvc(storage_class: str = "ceph-block-ec") -> MagicMock:
    pvc = MagicMock()
    pvc.spec.resources.requests = {"storage": "20Gi"}
    pvc.spec.storage_class_name = storage_class
    return pvc


class TestRequestModels:
    """The target class has to be expressible in the first place."""

    def test_vm_from_template_accepts_a_storage_class(self) -> None:
        req = VMFromTemplateRequest(
            display_name="db", template_name="ubuntu", storage_class="ceph-block",
        )
        assert req.storage_class == "ceph-block"

    def test_vm_from_template_storage_class_is_optional(self) -> None:
        req = VMFromTemplateRequest(display_name="db", template_name="ubuntu")
        assert req.storage_class is None

    def test_image_from_disk_accepts_a_storage_class(self) -> None:
        req = CreateImageFromDiskRequest(
            source_disk_name="d", source_namespace="ns",
            display_name="img", storage_class="ceph-block-ec",
        )
        assert req.storage_class == "ceph-block-ec"

    def test_image_from_disk_storage_class_is_optional(self) -> None:
        req = CreateImageFromDiskRequest(
            source_disk_name="d", source_namespace="ns", display_name="img",
        )
        assert req.storage_class is None


@pytest.mark.asyncio
class TestImageFromDiskTargeting:
    """create_golden_image_from_disk must not copy the source's class."""

    async def _run(self, storage_class: str | None) -> dict[str, Any]:
        from app.api.v1 import images

        captured: dict[str, Any] = {}

        async def _create(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            body = kwargs["body"]
            body["metadata"] = dict(body["metadata"])
            body["metadata"]["name"] = "img-abc12"
            return body

        k8s = MagicMock()
        k8s.core_api.read_namespace = AsyncMock()
        k8s.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_source_pvc("ceph-block-ec"),
        )
        api = MagicMock()
        api.create_namespaced_custom_object = AsyncMock(side_effect=_create)

        request = MagicMock()
        request.app.state.k8s_client = k8s

        req = CreateImageFromDiskRequest(
            source_disk_name="vm-root", source_namespace="ns",
            display_name="My Image", storage_class=storage_class,
        )

        with patch.object(images.client, "CustomObjectsApi", return_value=api):
            await images.create_golden_image_from_disk(
                req=req, request=request, user=MagicMock(),
            )

        return captured["body"]["spec"]["storage"]

    async def test_explicit_class_is_used(self) -> None:
        storage = await self._run("ceph-block")
        assert storage["storageClassName"] == "ceph-block"

    async def test_omitted_falls_through_to_cluster_default(self) -> None:
        # No storageClassName at all — CDI then uses the cluster default.
        # The source PVC is on ceph-block-ec and must NOT leak through.
        storage = await self._run(None)
        assert "storageClassName" not in storage
