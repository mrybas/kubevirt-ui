"""Read routes answer only for namespaces and folders the caller can reach.

The write paths were scoped, several reads were not: any logged-in user, with
no group and no binding, could list every tenant's images, ask which VMs use
an image in any namespace, read every template, and read any folder's
members, environments and quota by name. All of it with the UI's own
ServiceAccount, which can read every enabled namespace.

Foreign namespaces and folders answer 404, like the rest of the API.
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import folders, images, storage, templates
from app.core.auth import User

MINE = "tenant-mine"
THEIRS = "tenant-theirs"


def _user() -> User:
    return User(id="u", email="u@example.test", username="u", groups=["team-mine"])


def _k8s() -> MagicMock:
    k8s = MagicMock()
    k8s.list_namespaces = AsyncMock(return_value=[
        {"name": n, "status": "Active", "labels": {"kubevirt-ui.io/enabled": "true"}}
        for n in (MINE, THEIRS)
    ])
    return k8s


def _request(k8s: MagicMock) -> MagicMock:
    request = MagicMock()
    request.app.state.k8s_client = k8s
    return request


def _mine_only():
    """The caller has a binding in MINE and nowhere else."""
    return patch(
        "kubernetes_asyncio.client.RbacAuthorizationV1Api",
        return_value=SimpleNamespace(list_namespaced_role_binding=AsyncMock(
            side_effect=lambda namespace, **_: SimpleNamespace(items=[
                SimpleNamespace(subjects=[SimpleNamespace(kind="Group", name="team-mine")])
            ] if namespace == MINE else []),
        )),
    )


@pytest.mark.asyncio
class TestImages:
    async def test_the_list_reads_only_your_namespaces(self) -> None:
        k8s = _k8s()
        k8s.core_api.read_namespace = AsyncMock(return_value=SimpleNamespace(
            metadata=SimpleNamespace(labels={}),
        ))
        read: list[str] = []

        async def _list(**kwargs: Any) -> dict[str, Any]:
            read.append(kwargs["namespace"])
            return {"items": []}

        api = MagicMock()
        api.list_namespaced_custom_object = AsyncMock(side_effect=_list)
        with _mine_only(), patch.object(images.client, "CustomObjectsApi", return_value=api):
            await images.list_golden_images(request=_request(k8s), user=_user())
        assert read and set(read) == {MINE}

    async def test_a_foreign_namespace_is_refused(self) -> None:
        with _mine_only(), pytest.raises(HTTPException) as e:
            await images.list_golden_images(
                request=_request(_k8s()), user=_user(), namespace=THEIRS,
            )
        assert e.value.status_code == 404

    async def test_usage_of_a_foreign_image_is_refused(self) -> None:
        k8s = _k8s()
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(return_value={})
        with _mine_only(), pytest.raises(HTTPException) as e:
            await storage.get_image_usage(
                request=_request(k8s), namespace=THEIRS, name="debian", user=_user(),
            )
        assert e.value.status_code == 404
        k8s.custom_api.get_namespaced_custom_object.assert_not_awaited()

    async def test_usage_of_your_own_image_gets_past_the_guard(self) -> None:
        k8s = _k8s()
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(return_value={})
        with _mine_only(), patch.object(storage.client, "CustomObjectsApi"):
            try:
                await storage.get_image_usage(
                    request=_request(k8s), namespace=MINE, name="debian", user=_user(),
                )
            except Exception:
                pass  # past the guard, failing on the mock cluster
        k8s.custom_api.get_namespaced_custom_object.assert_awaited()


def _template_k8s() -> MagicMock:
    k8s = _k8s()
    cm = MagicMock()
    cm.data = {
        name: json.dumps({"display_name": name, "golden_image_name": "img",
                          "golden_image_namespace": ns})
        for name, ns in (("mine", MINE), ("theirs", THEIRS), ("shared", "golden-images"))
    }
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    return k8s


def _no_template_crs():
    api = MagicMock()
    api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
    return patch.object(templates.client, "CustomObjectsApi", return_value=api)


@pytest.mark.asyncio
class TestTemplates:
    async def test_the_list_hides_another_tenants_templates(self) -> None:
        with _mine_only(), _no_template_crs():
            got = await templates.list_templates(request=_request(_template_k8s()), user=_user())
        assert sorted(t.name for t in got.items) == ["mine", "shared"]

    async def test_a_foreign_template_is_refused_by_name(self) -> None:
        with _mine_only(), _no_template_crs(), pytest.raises(HTTPException) as e:
            await templates.get_template(
                name="theirs", request=_request(_template_k8s()), user=_user(),
            )
        assert e.value.status_code == 404

    @pytest.mark.parametrize("name", ["mine", "shared"])
    async def test_your_own_and_shared_templates_are_served(self, name: str) -> None:
        with _mine_only(), _no_template_crs():
            got = await templates.get_template(
                name=name, request=_request(_template_k8s()), user=_user(),
            )
        assert got.name == name


def _folder_k8s() -> MagicMock:
    k8s = _k8s()
    cm = MagicMock()
    cm.data = {"mine": json.dumps({"display_name": "Mine"}),
               "theirs": json.dumps({"display_name": "Theirs"})}
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.list_namespace = AsyncMock(return_value=SimpleNamespace(items=[
        SimpleNamespace(metadata=SimpleNamespace(name=ns, labels={
            "kubevirt-ui.io/enabled": "true", "kubevirt-ui.io/folder": folder,
            "kubevirt-ui.io/environment": "prod",
        }))
        for ns, folder in ((MINE, "mine"), (THEIRS, "theirs"))
    ]))
    return k8s


FOLDER_READS = [
    folders.get_folder,
    folders.get_folder_quota_headroom,
    folders.list_folder_access,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", FOLDER_READS, ids=lambda h: h.__name__)
class TestFolders:
    async def test_a_foreign_folder_is_refused(self, handler: Any) -> None:
        with _mine_only(), pytest.raises(HTTPException) as e:
            await handler(request=_request(_folder_k8s()), name="theirs", user=_user())
        assert e.value.status_code == 404

    async def test_your_own_folder_gets_past_the_guard(self, handler: Any) -> None:
        with _mine_only():
            try:
                await handler(request=_request(_folder_k8s()), name="mine", user=_user())
            except HTTPException as e:
                assert e.status_code != 404, "the guard refused the caller's own folder"
            except Exception:
                pass  # past the guard, failing on the mock cluster
