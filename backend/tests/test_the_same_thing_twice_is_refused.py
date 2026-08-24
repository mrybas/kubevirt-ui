"""Creating something that already exists is refused, in the same words.

UAT run 4, D-2: importing the same image twice — same display name, same URL,
same project — closed the dialog without a word and left two objects that the
picker shows identically:

    UAT Ubuntu 22.04   uat-ubuntu-dkvpb   10Gi   InUse   1 VM
    UAT Ubuntu 22.04   uat-ubuntu-x8czz   10Gi   Ready   -

and the second one pulled the same gigabyte into Ceph again.

It is a regression with a cause worth naming: while images carried the name a
person typed, Kubernetes refused the second one for free. Moving to
`generateName` fixed real collisions and removed that refusal, and nothing was
put in its place.

Tenants and templates both answer this case already. This file holds all three
to one behaviour, because the interesting failure is not any one of them being
wrong — it is a product that refuses in one place and shrugs in another.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.templates import _refuse_a_duplicate_image


def _dv(name: str, display: str) -> dict:
    return {"metadata": {
        "name": name,
        "annotations": {"kubevirt-ui.io/display-name": display},
    }}


def _managed_image(name: str, display: str) -> dict:
    return {"metadata": {"name": name}, "spec": {"displayName": display}}


def _api(datavolumes: list[dict], managedimages: list[dict] | None = None):
    api = MagicMock()

    async def listing(**kwargs):
        if kwargs.get("plural") == "datavolumes":
            return {"items": datavolumes}
        return {"items": managedimages or []}

    api.list_namespaced_custom_object = AsyncMock(side_effect=listing)
    return api


async def _check(api, display: str = "UAT Ubuntu 22.04"):
    k8s = MagicMock()
    with patch("app.api.v1.templates.client.CustomObjectsApi", return_value=api):
        await _refuse_a_duplicate_image(k8s, "poc-transit-dev", display)


@pytest.mark.asyncio
class TestImportingTheSameImageTwice:
    async def test_the_second_one_is_refused(self) -> None:
        with pytest.raises(HTTPException) as e:
            await _check(_api([_dv("uat-ubuntu-dkvpb", "UAT Ubuntu 22.04")]))
        assert e.value.status_code == 409
        # Which one, and where — the synthetic name is the only way to find it.
        assert "uat-ubuntu-dkvpb" in e.value.detail
        assert "poc-transit-dev" in e.value.detail

    async def test_a_different_name_is_not_a_duplicate(self) -> None:
        await _check(_api([_dv("uat-ubuntu-dkvpb", "UAT Ubuntu 24.04")]))

    async def test_one_described_but_not_yet_built_counts(self) -> None:
        """The window the operator path spends between request and disk is
        exactly when somebody presses the button again."""
        with pytest.raises(HTTPException):
            await _check(_api([], [_managed_image("uat-ubuntu-x8czz", "UAT Ubuntu 22.04")]))

    async def test_an_empty_name_is_not_matched_against_everything(self) -> None:
        await _check(_api([_dv("something", "")]), display="")

    async def test_a_cluster_without_the_operator_crd_still_checks_disks(self) -> None:
        api = MagicMock()

        async def listing(**kwargs):
            if kwargs.get("plural") == "managedimages":
                raise ApiException(status=404)
            return {"items": [_dv("uat-ubuntu-dkvpb", "UAT Ubuntu 22.04")]}

        api.list_namespaced_custom_object = AsyncMock(side_effect=listing)
        with pytest.raises(HTTPException):
            await _check(api)


def test_all_three_creates_refuse_what_already_exists() -> None:
    """One product, one answer. Images were the only one that shrugged."""
    import inspect

    from app.api.v1.tenants_crud import create_tenant
    from app.api.v1.templates import create_golden_image, create_template

    for fn in (create_tenant, create_template):
        assert "already exists" in inspect.getsource(fn), fn.__name__
    image = inspect.getsource(create_golden_image)
    assert "_refuse_a_duplicate_image" in image
    # Before anything is written, or it is not a refusal.
    assert image.index("_refuse_a_duplicate_image") < image.index(
        "create_namespaced_custom_object")
