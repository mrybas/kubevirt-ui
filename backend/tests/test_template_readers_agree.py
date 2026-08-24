"""Every path that reads a template reads both stores.

With OPERATOR_TEMPLATE_ENABLED on, a template is written as a
ManagedVMTemplate. The list merged both stores, `GET /templates/{name}` read
both, and `POST /vms/from-template` read the ConfigMap alone — so the template
appeared in the list, the wizard offered it, and creating a machine from it
answered

    404 Template op-ubuntu-small not found

at the last click of the flow. The pairing of OPERATOR_TEMPLATE_ENABLED with
OPERATOR_VM_ENABLED was unusable and looked fine until then.

The chart's own values promise "reading always covers both stores, so a template
written either way stays usable". It did not, and this is what holds the promise.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.templates import resolve_template

LEGACY = {
    "display_name": "Legacy Ubuntu",
    "golden_image_name": "ubuntu-2404",
    "compute": {"cpu_cores": 2, "memory": "4Gi"},
    "disk": {"size": "20Gi"},
}


def _k8s(configmap: dict | None, cr: dict | None):
    k8s = MagicMock()
    if configmap is None:
        from kubernetes_asyncio.client.rest import ApiException

        k8s.core_api.read_namespaced_config_map = AsyncMock(
            side_effect=ApiException(status=404),
        )
    else:
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            return_value=SimpleNamespace(data={
                name: json.dumps(body) for name, body in configmap.items()
            }),
        )
    k8s._api_client = MagicMock()
    return k8s


@pytest.fixture
def crs(monkeypatch):
    """Whatever ManagedVMTemplates exist, keyed by name."""
    store: dict[str, dict] = {}
    import app.api.v1.templates as mod

    async def find(_api, name):
        return store.get(name)

    monkeypatch.setattr(mod, "_find_template_cr", find)
    monkeypatch.setattr(mod.client, "CustomObjectsApi", lambda _c: MagicMock())
    return store


@pytest.mark.asyncio
async def test_a_template_in_the_old_store_is_found(crs) -> None:
    got = await resolve_template(_k8s({"legacy": LEGACY}, None), "legacy")
    assert got is not None
    assert got["name"] == "legacy"
    assert got["compute"]["cpu_cores"] == 2


@pytest.mark.asyncio
async def test_a_template_written_as_a_resource_is_found(crs) -> None:
    """The case that 404'd: the operator holds it, the ConfigMap does not."""
    crs["op-ubuntu-small"] = {
        "metadata": {"name": "op-ubuntu-small"},
        "spec": {
            "displayName": "Op Ubuntu Small",
            "imageRef": {"name": "ubuntu-2404", "namespace": "golden-images"},
            "compute": {"cpuCores": 2, "memory": "4Gi"},
            "rootDisk": {"size": "20Gi"},
        },
    }
    got = await resolve_template(_k8s(None, None), "op-ubuntu-small")
    assert got is not None, "the resource store was not consulted"
    assert got["name"] == "op-ubuntu-small"
    # In the shape the create path reads, not the shape the store keeps.
    assert got["golden_image_name"] == "ubuntu-2404"
    assert "compute" in got and "disk" in got


@pytest.mark.asyncio
async def test_a_resource_shadows_a_legacy_entry_of_the_same_name(crs) -> None:
    crs["both"] = {
        "metadata": {"name": "both"},
        "spec": {"displayName": "From the resource",
                 "imageRef": {"name": "ubuntu-2404"}},
    }
    got = await resolve_template(_k8s({"both": LEGACY}, None), "both")
    assert got["display_name"] == "From the resource"


@pytest.mark.asyncio
async def test_a_name_in_neither_store_is_absent_not_an_error(crs) -> None:
    assert await resolve_template(_k8s({"other": LEGACY}, None), "missing") is None
    assert await resolve_template(_k8s(None, None), "missing") is None


@pytest.mark.asyncio
async def test_the_create_path_uses_the_same_resolver(monkeypatch) -> None:
    """The point of the fix: one owner for "what is this template", because the
    answer was being worked out in three places and one of them was wrong."""
    import app.api.v1.templates as templates
    import app.api.v1.vms as vms

    asked: list[str] = []

    async def resolver(_k8s, name):
        asked.append(name)
        return None

    monkeypatch.setattr(templates, "resolve_template", resolver)

    from fastapi import HTTPException

    request = MagicMock()
    request.app.state.k8s_client = MagicMock()
    with pytest.raises(HTTPException) as e:
        await vms.create_vm_from_template(
            request, "poc-transit-dev",
            vms.VMFromTemplateRequest(display_name="op-vm1", template_name="op-ubuntu-small"),
            user=MagicMock(),
        )

    assert asked == ["op-ubuntu-small"], "it consulted something else"
    assert e.value.status_code == 404


# Every endpoint that names a template, and the store it must consult. Listed
# rather than discovered: one of these was missed once and the symptom was a
# 404 for something the list had just shown.
ENDPOINTS = ["list_templates", "get_template", "update_template", "delete_template"]


def test_every_template_endpoint_consults_the_resource_store() -> None:
    """Create is the exception: it decides where to write, by the flag.

    The rest answer about a template that already exists, and where it exists is
    not theirs to assume. `update_template` assumed, and a CR-backed template
    404'd on edit exactly as it did on create-from-template.
    """
    import inspect

    import app.api.v1.templates as templates

    for name in ENDPOINTS:
        source = inspect.getsource(getattr(templates, name))
        assert "_find_template_cr" in source or "_list_template_crs" in source, (
            f"{name} does not look in the resource store, so a template written "
            f"with OPERATOR_TEMPLATE_ENABLED on is invisible to it"
        )

    from app.api.v1.vms import create_vm_from_template

    assert "resolve_template" in inspect.getsource(create_vm_from_template)


@pytest.mark.asyncio
async def test_editing_a_resource_backed_template_patches_the_resource(crs) -> None:
    """It used to read the ConfigMap, not find it, and answer 404."""
    import app.api.v1.templates as templates

    crs["op-ubuntu-small"] = {
        "metadata": {"name": "op-ubuntu-small", "namespace": "golden-images"},
        "spec": {"displayName": "Before", "imageRef": {"name": "ubuntu-2404"}},
    }

    patched: dict = {}

    async def patch(**kwargs):
        patched.update(kwargs)
        return {
            "metadata": {"name": "op-ubuntu-small", "namespace": "golden-images"},
            "spec": kwargs["body"]["spec"],
        }

    api = MagicMock()
    api.patch_namespaced_custom_object = AsyncMock(side_effect=patch)
    monkey = MagicMock(return_value=api)

    k8s = _k8s(None, None)
    k8s.core_api.replace_namespaced_config_map = AsyncMock()
    request = MagicMock()
    request.app.state.k8s_client = k8s

    import pytest as _pytest

    from app.models.template import (
        TemplateCompute, TemplateConsole, TemplateDisk, VMTemplateCreate,
    )

    original = templates.client.CustomObjectsApi
    templates.client.CustomObjectsApi = monkey
    try:
        got = await templates.update_template(
            "op-ubuntu-small",
            VMTemplateCreate(
                name="op-ubuntu-small", display_name="After",
                golden_image_name="ubuntu-2404",
                golden_image_namespace="golden-images",
                compute=TemplateCompute(), disk=TemplateDisk(),
                console=TemplateConsole(),
            ),
            request, user=MagicMock(),
        )
    finally:
        templates.client.CustomObjectsApi = original

    assert patched["plural"] == "managedvmtemplates"
    assert patched["namespace"] == "golden-images"
    assert patched["body"]["spec"]["displayName"] == "After"
    assert got.display_name == "After"
    k8s.core_api.replace_namespaced_config_map.assert_not_awaited()
    _ = _pytest
