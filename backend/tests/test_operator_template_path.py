"""Templates as their own objects instead of a shared blob.

Every template used to be a JSON value under a user-chosen key in one
cluster-wide ConfigMap, rewritten whole on every change. That store had three
properties worth losing: a name collision answered 409 naming a template the
user might not be allowed to see, two concurrent writes lost one of each other
because the map was replaced without a version check, and the reference to an
image was the *generated* name of a DataVolume — a name that does not exist
until the image has been created and read back, which is precisely why a
template could not be written from a manifest.

What is guarded here is the migration behaviour rather than the storage: both
stores are readable at once, and a template that already exists as an object is
edited and deleted as one regardless of the flag, because the flag says where
new templates go, not where the old ones live.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.exceptions import ApiException

from app.models.template import (
    TemplateCompute,
    TemplateDisk,
    VMTemplateCreate,
)


def _cr(name: str, namespace: str, image: str, image_ns: str | None = None) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": namespace, "creationTimestamp": None},
        "spec": {
            "displayName": f"{name} display",
            "imageRef": {"name": image},
            "compute": {"cores": 2, "sockets": 1, "threads": 1, "memory": "4Gi"},
            "rootDisk": {"size": "20Gi"},
            "osType": "linux",
            "category": "linux",
        },
        "status": {"imageNamespace": image_ns or namespace},
    }


def _harness(crs: list[dict[str, Any]], legacy: dict[str, str] | None = None):
    from app.api.v1 import templates

    created: dict[str, Any] = {}
    deleted: list[dict[str, Any]] = []

    async def _list_cluster(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("plural") != "managedvmtemplates":
            return {"items": []}
        return {"items": crs}

    async def _create(**kwargs: Any) -> dict[str, Any]:
        created.update(kwargs)
        return _cr(
            kwargs["body"]["metadata"]["name"],
            kwargs["namespace"],
            kwargs["body"]["spec"]["imageRef"]["name"],
        )

    async def _delete(**kwargs: Any) -> dict[str, Any]:
        deleted.append(kwargs)
        return {}

    api = MagicMock()
    api.list_cluster_custom_object = AsyncMock(side_effect=_list_cluster)
    api.create_namespaced_custom_object = AsyncMock(side_effect=_create)
    api.delete_namespaced_custom_object = AsyncMock(side_effect=_delete)
    # The legacy create path checks the image exists as a DataVolume first.
    api.get_namespaced_custom_object = AsyncMock(return_value={"metadata": {"name": "ubuntu-2404"}})

    k8s = MagicMock()
    cm = MagicMock()
    cm.data = legacy or {}
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.replace_namespaced_config_map = AsyncMock()
    k8s.core_api.read_namespace = AsyncMock()
    k8s.core_api.create_namespaced_config_map = AsyncMock()

    request = MagicMock()
    request.app.state.k8s_client = k8s

    return templates, api, k8s, request, created, deleted


@pytest.mark.asyncio
class TestReadingCoversBothStores:
    async def test_resources_and_legacy_entries_appear_together(self) -> None:
        import json

        legacy = {"old-one": json.dumps({
            "display_name": "Old", "golden_image_name": "img-x", "golden_image_namespace": "ns-a",
        })}
        templates, api, _, request, _, _ = _harness([_cr("new-one", "opdev-dev", "ubuntu")], legacy)
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            got = await templates.list_templates(request=request, user=MagicMock())
        names = sorted(t.name for t in got.items)
        assert names == ["new-one", "old-one"]

    async def test_a_resource_shadows_a_legacy_entry_of_the_same_name(self) -> None:
        """The migration copies one to the other; showing both is showing double."""
        import json

        legacy = {"ubuntu-base": json.dumps({
            "display_name": "Legacy copy", "golden_image_name": "img-x",
            "golden_image_namespace": "ns-a",
        })}
        templates, api, _, request, _, _ = _harness(
            [_cr("ubuntu-base", "opdev-dev", "ubuntu")], legacy,
        )
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            got = await templates.list_templates(request=request, user=MagicMock())
        assert len(got.items) == 1
        assert got.items[0].display_name == "ubuntu-base display"

    async def test_the_image_namespace_survives_the_conversion(self) -> None:
        """The wizard filters templates by it, so getting it wrong hides them."""
        templates, api, _, request, _, _ = _harness(
            [_cr("shared", "opdev-dev", "ubuntu", image_ns="opdev-shared")],
        )
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            got = await templates.list_templates(request=request, user=MagicMock())
        assert got.items[0].golden_image_namespace == "opdev-shared"


@pytest.mark.asyncio
class TestWritingFollowsTheFlagAndDeletingFollowsTheObject:
    async def test_flag_on_writes_a_resource_next_to_its_image(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPERATOR_TEMPLATE_ENABLED", "true")
        templates, api, _, request, created, _ = _harness([])
        req = VMTemplateCreate(
            name="ubuntu-base", display_name="Ubuntu base",
            golden_image_name="ubuntu-2404", golden_image_namespace="opdev-dev",
            compute=TemplateCompute(cpu_cores=2, memory="4Gi"),
            disk=TemplateDisk(size="20Gi"),
        )
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            await templates.create_template(template=req, request=request, user=MagicMock())
        assert created["plural"] == "managedvmtemplates"
        assert created["namespace"] == "opdev-dev"
        assert created["body"]["spec"]["imageRef"] == {"name": "ubuntu-2404"}

    async def test_flag_off_still_writes_the_configmap(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPERATOR_TEMPLATE_ENABLED", "")
        templates, api, k8s, request, created, _ = _harness([])
        req = VMTemplateCreate(
            name="ubuntu-base", display_name="Ubuntu base",
            golden_image_name="ubuntu-2404", golden_image_namespace="opdev-dev",
        )
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            await templates.create_template(template=req, request=request, user=MagicMock())
        assert created == {}
        assert k8s.core_api.replace_namespaced_config_map.await_count == 1

    async def test_deleting_a_resource_deletes_the_resource_whatever_the_flag_says(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPERATOR_TEMPLATE_ENABLED", "")
        templates, api, k8s, request, _, deleted = _harness(
            [_cr("ubuntu-base", "opdev-dev", "ubuntu")],
        )
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            await templates.delete_template(
                name="ubuntu-base", request=request, user=MagicMock(),
            )
        assert deleted and deleted[0]["plural"] == "managedvmtemplates"
        assert k8s.core_api.replace_namespaced_config_map.await_count == 0

    async def test_an_ambiguous_name_is_reported_rather_than_guessed(self) -> None:
        """Names are unique per namespace, so a bare name can mean two things.

        The old store's answer to a collision was a 409 naming a template the
        user could not see; picking one at random is not an improvement.
        """
        templates, api, _, request, _, _ = _harness([
            _cr("ubuntu-base", "opdev-dev", "ubuntu"),
            _cr("ubuntu-base", "other-dev", "ubuntu"),
        ])
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            with pytest.raises(HTTPException) as caught:
                await templates.get_template(
                    name="ubuntu-base", request=request, user=MagicMock(),
                )
        assert caught.value.status_code == 409
        assert "opdev-dev" in caught.value.detail
        assert "other-dev" in caught.value.detail

    async def test_a_missing_crd_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPERATOR_TEMPLATE_ENABLED", "true")
        templates, api, _, request, _, _ = _harness([])
        api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"),
        )
        req = VMTemplateCreate(
            name="ubuntu-base", display_name="Ubuntu base",
            golden_image_name="ubuntu-2404", golden_image_namespace="opdev-dev",
        )
        with patch.object(templates.client, "CustomObjectsApi", return_value=api):
            with pytest.raises(HTTPException) as caught:
                await templates.create_template(
                    template=req, request=request, user=MagicMock(),
                )
        assert caught.value.status_code == 503
        assert "ManagedVMTemplate CRD" in caught.value.detail
