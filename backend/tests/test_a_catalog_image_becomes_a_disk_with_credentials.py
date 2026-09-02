"""A private Harbor project cannot be pulled anonymously.

CDI resolves secretRef in the DataVolume's OWN namespace, so the Secret has to
be in the target namespace — which is where the harbor-robots chart puts it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.images import build_registry_source
from app.models.template import GoldenImageCreate


def test_a_pull_without_credentials_stays_credential_free():
    src = build_registry_source(
        "docker://harbor.example/vm-images-public/ubuntu:1", None, None
    )

    assert src == {"registry": {"url": "docker://harbor.example/vm-images-public/ubuntu:1"}}


def test_a_pull_with_a_robot_secret_carries_it():
    src = build_registry_source(
        "docker://harbor.example/vm-images-tenant-a/ubuntu:1",
        "harbor-robot-tenant-a",
        None,
    )

    assert src["registry"]["secretRef"] == "harbor-robot-tenant-a"


def test_a_private_ca_is_passed_as_a_config_map():
    src = build_registry_source(
        "docker://harbor.example/p/u:1", "sec", "harbor-ca"
    )

    assert src["registry"]["certConfigMap"] == "harbor-ca"
    assert src["registry"]["secretRef"] == "sec"


def _k8s_with_namespace() -> MagicMock:
    """A mock k8s client whose namespace and quota checks pass through clean.

    Mirrors the helper in test_operator_image_path.py — the create endpoint is
    exercised by calling it directly rather than over HTTP, which is how this
    codebase already tests `create_golden_image`.
    """
    k8s = MagicMock()
    ns = MagicMock()
    ns.metadata.labels = {}
    k8s.core_api.read_namespace = AsyncMock(return_value=ns)
    k8s.core_api.list_namespaced_resource_quota = AsyncMock(
        return_value=SimpleNamespace(items=[]))
    return k8s


async def test_a_missing_robot_secret_is_named_in_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal happens before any DataVolume/ManagedImage is created.

    A DataVolume created with an unresolvable secretRef fails later, inside
    CDI, as an import error that never mentions the Secret — the user would
    learn nothing useful from that. This has to happen first, and it has to
    name the Secret.
    """
    from app.api.v1 import images

    monkeypatch.delenv("OPERATOR_IMAGE_ENABLED", raising=False)

    k8s = _k8s_with_namespace()
    k8s.core_api.read_namespaced_secret = AsyncMock(
        side_effect=ApiException(status=404)
    )

    api = MagicMock()
    # Nothing with this display name exists yet — the duplicate check passes.
    api.list_namespaced_custom_object = AsyncMock(return_value={"items": []})
    api.create_namespaced_custom_object = AsyncMock(
        side_effect=AssertionError("must not create anything past the refusal")
    )

    request = MagicMock()
    request.app.state.k8s_client = k8s

    image = GoldenImageCreate(
        display_name="Tenant A Ubuntu",
        source_registry="docker://harbor.example/vm-images-tenant-a/ubuntu:1",
        source_registry_secret="absent-secret",
        size="10Gi",
    )

    with patch.object(images.client, "CustomObjectsApi", return_value=api):
        with pytest.raises(HTTPException) as exc_info:
            await images.create_golden_image(
                image=image, request=request, user=MagicMock(), namespace="opdev-dev",
            )

    assert exc_info.value.status_code == 422
    assert "absent-secret" in exc_info.value.detail
    assert "opdev-dev" in exc_info.value.detail
    api.create_namespaced_custom_object.assert_not_called()


async def test_the_managed_image_writer_carries_the_secret_and_ca_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both writers must carry the credential, not just the DataVolume one.

    Today they do — `create_golden_image` builds `source` once via
    `build_registry_source` before branching on `image_path_enabled()`, and
    both the DataVolume path and `_create_managed_image` consume that same
    dict. But that sharing is an implementation detail, not a guarantee: a
    later refactor could split the two writers' source construction without
    anything failing loudly. A ManagedImage missing `secretRef` still gets
    created — the operator would build a DataVolume from it, and only then,
    inside CDI, would an anonymous pull against a private project fail, with
    an error that never mentions the credential that quietly went missing.
    This test pins the ManagedImage's actual `spec.source.registry` shape so
    that regression fails here instead.
    """
    from app.api.v1 import images

    monkeypatch.setenv("OPERATOR_IMAGE_ENABLED", "true")

    k8s = _k8s_with_namespace()
    # The Secret exists this time — the pre-flight check must pass through
    # so the create actually happens and there is a body to inspect.
    k8s.core_api.read_namespaced_secret = AsyncMock(return_value=MagicMock())

    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        body = dict(kwargs["body"])
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"].get("generateName", "") + "x7k2p"
        body["metadata"]["creationTimestamp"] = "2026-08-20T00:00:00Z"
        return body

    api = MagicMock()
    api.create_namespaced_custom_object = AsyncMock(side_effect=_create)
    api.list_namespaced_custom_object = AsyncMock(return_value={"items": []})

    request = MagicMock()
    request.app.state.k8s_client = k8s

    image = GoldenImageCreate(
        display_name="Tenant A Ubuntu",
        source_registry="docker://harbor.example/vm-images-tenant-a/ubuntu:1",
        source_registry_secret="harbor-robot-tenant-a",
        source_registry_ca_configmap="harbor-ca",
        size="10Gi",
    )

    with patch.object(images.client, "CustomObjectsApi", return_value=api):
        await images.create_golden_image(
            image=image, request=request, user=MagicMock(), namespace="opdev-dev",
        )

    # Proves the operator path, not the DataVolume path, is what got exercised.
    assert captured["plural"] == "managedimages"
    assert captured["body"]["kind"] == "ManagedImage"

    registry = captured["body"]["spec"]["source"]["registry"]
    assert registry["secretRef"] == "harbor-robot-tenant-a"
    assert registry["certConfigMap"] == "harbor-ca"
