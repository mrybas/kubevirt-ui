"""The namespace an image endpoint acts on has to belong to the caller.

Every one of these took the namespace from a query parameter and read it with
the UI's own ServiceAccount, which can see all of them. Nothing checked it, so
the only thing between a caller and another team's project was knowing its
name: create a disk there, rename theirs, delete theirs, or copy theirs into
your own namespace.

It predates the Harbor catalogue and the catalogue is what made it matter: the
create path now attaches THAT namespace's robot credential to the pull, so an
unchecked namespace spends another tenant's credential and another tenant's
quota — and leaves the result where they can see it.

404 rather than 403 throughout, matching `storage.py` and the VM path: whether
the namespace exists is not this caller's business either.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import images
from app.models.template import CreateImageFromDiskRequest, GoldenImageCreate, GoldenImageUpdate


def _request(allowed: list[str]) -> MagicMock:
    """A request whose caller may reach exactly `allowed`."""
    k8s = MagicMock()
    k8s.list_namespaces = AsyncMock(
        return_value=[
            {"name": n, "status": "Active", "labels": {"kubevirt-ui.io/enabled": "true"}}
            for n in allowed
        ]
    )
    request = MagicMock()
    request.app.state.k8s_client = k8s
    return request


def _admin() -> MagicMock:
    user = MagicMock()
    user.groups = ["kubevirt-ui-admins"]
    user.username = "someone"
    user.email = "someone@example.test"
    return user


async def _call(handler: str, namespace: str, allowed: list[str]) -> Any:
    """Invoke one image handler for `namespace`, as a caller who may reach `allowed`.

    Nothing beyond the guard is mocked on purpose: if the refusal ever stops
    happening, the call runs on into a MagicMock cluster and fails loudly
    rather than passing quietly.
    """
    request = _request(allowed)
    user = _admin()
    if handler == "create":
        return await images.create_golden_image(
            image=GoldenImageCreate(display_name="x", size="10Gi", source_type="blank"),
            request=request, user=user, namespace=namespace,
        )
    if handler == "delete":
        return await images.delete_golden_image(
            name="theirs", request=request, user=user, namespace=namespace,
        )
    if handler == "patch":
        return await images.update_golden_image(
            name="theirs", update=GoldenImageUpdate(display_name="mine"),
            request=request, user=user, namespace=namespace,
        )
    if handler == "from-disk":
        return await images.create_golden_image_from_disk(
            req=CreateImageFromDiskRequest(
                source_disk_name="root", source_namespace=namespace, display_name="copy",
            ),
            request=request, user=user,
        )
    raise AssertionError(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["create", "delete", "patch", "from-disk"])
class TestAForeignNamespaceIsRefused:
    async def test_it_is_refused(self, handler: str) -> None:
        with pytest.raises(HTTPException) as e:
            await _call(handler, "tenant-someone-else", allowed=["tenant-mine"])
        assert e.value.status_code == 404

    async def test_the_refusal_does_not_confirm_the_namespace_exists(
        self, handler: str,
    ) -> None:
        """Telling "not yours" apart from "no such thing" enumerates namespaces."""
        with pytest.raises(HTTPException) as e:
            await _call(handler, "tenant-someone-else", allowed=["tenant-mine"])
        assert "not found" in e.value.detail.lower()
        for word in ("forbidden", "permission", "allowed", "access"):
            assert word not in e.value.detail.lower()

    async def test_a_namespace_of_your_own_gets_past_the_guard(
        self, handler: str,
    ) -> None:
        """The other half: a guard that refuses everything passes the tests above
        and breaks the product. Past the guard the call reaches a mock cluster
        and fails there — anything but a 404 from the guard itself."""
        try:
            await _call(handler, "tenant-mine", allowed=["tenant-mine"])
        except HTTPException as e:
            assert e.status_code != 404, "the guard refused a namespace the caller owns"
        except Exception:
            pass  # got past the guard and died in the mock cluster, which is the point


@pytest.mark.asyncio
class TestCopyingBetweenNamespaces:
    async def test_both_ends_are_checked(self) -> None:
        """Source and target are two different rights: one is what gets read,
        the other is where it lands and whose quota pays."""
        request = _request(["tenant-mine"])
        with pytest.raises(HTTPException) as e:
            await images.create_golden_image_from_disk(
                req=CreateImageFromDiskRequest(
                    source_disk_name="root",
                    source_namespace="tenant-mine",
                    target_namespace="tenant-someone-else",
                    display_name="copy",
                ),
                request=request, user=_admin(),
            )
        assert e.value.status_code == 404
        assert "tenant-someone-else" in e.value.detail

    async def test_the_source_is_checked_too(self) -> None:
        request = _request(["tenant-mine"])
        with pytest.raises(HTTPException) as e:
            await images.create_golden_image_from_disk(
                req=CreateImageFromDiskRequest(
                    source_disk_name="root",
                    source_namespace="tenant-someone-else",
                    target_namespace="tenant-mine",
                    display_name="copy",
                ),
                request=request, user=_admin(),
            )
        assert e.value.status_code == 404
        assert "tenant-someone-else" in e.value.detail
