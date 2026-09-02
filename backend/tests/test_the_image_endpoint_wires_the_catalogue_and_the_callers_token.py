"""GET /images wires the Harbor catalogue in correctly, or explains why not.

The neighbouring test files prove merge() and catalog_images() individually,
without FastAPI. Neither proves the *wiring*: that the flag actually gates the
call, that catalog_available reaches the HTTP response, and — the design's
central security claim — that the caller's own token is what reaches Harbor,
rather than an empty string, a service credential, or something transposed by
a wrong argument order. A regression in any of those would still pass every
test in this file's neighbours and only show up in a Playwright run against a
real Harbor (Task 9), which is not part of `pytest tests/ -q` and not a cheap
way to catch a wiring mistake.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.auth import User
from app.core.harbor_client import HarborUnauthorized, HarborUnavailable


def _no_cluster_resources():
    """A cluster with no DataVolumes, VirtualMachines, or ManagedImages.

    `list_golden_images` builds one CustomObjectsApi instance and reuses it
    for every `list_namespaced_custom_object` call in the handler
    (datavolumes twice, virtualmachines once, managedimages once via
    `_described_but_unbuilt_images`), so one empty-items mock covers all of
    them — same pattern `test_vms_phase2.py` uses for its own module.
    """
    api = MagicMock()
    api.list_namespaced_custom_object = AsyncMock(return_value={"items": []})
    return patch("app.api.v1.images.client.CustomObjectsApi", return_value=api)


def _cluster_with_one_disk():
    """One Ready DataVolume, no VMs, no ManagedImages.

    Used only by the "Harbor is down" test, so that "cluster rows still
    returned" has something in it to prove.
    """
    dv_items = [{
        "metadata": {
            "name": "ubuntu-existing",
            "namespace": "default",
            "creationTimestamp": "2026-08-01T00:00:00Z",
            "labels": {},
            "annotations": {},
        },
        "spec": {
            "source": {"http": {"url": "https://example.test/x.img"}},
            "storage": {"resources": {"requests": {"storage": "10Gi"}}},
        },
        "status": {"phase": "Succeeded", "conditions": []},
    }]

    async def _list(*args, **kwargs):
        # The vm-disk-owned-DV scan and the VM scan both pass a
        # label_selector or hit a different plural; only the plain,
        # unfiltered datavolumes listing should see this disk.
        if kwargs.get("plural") == "datavolumes" and "label_selector" not in kwargs:
            return {"items": dv_items}
        return {"items": []}

    api = MagicMock()
    api.list_namespaced_custom_object = AsyncMock(side_effect=_list)
    return patch("app.api.v1.images.client.CustomObjectsApi", return_value=api)


class TestTheCatalogueHalf:
    def test_flag_on_returns_catalog_rows_and_says_the_catalog_is_available(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "caller-token-abc"

        with _no_cluster_resources():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        data = response.json()
        assert data["catalog_available"] is True
        assert [item["origin"] for item in data["items"]] == ["catalog"]
        assert data["items"][0]["catalog_ref"] == "vm-images-public/ubuntu-2204:20260901"

    def test_flag_off_is_byte_identical_and_never_calls_harbor(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("HARBOR_IMAGE_ENABLED", raising=False)
        # Even with a token available, the flag being off must mean the
        # catalogue code path is never reached at all.
        fake_user.raw_token = "caller-token-abc"

        with _no_cluster_resources():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["catalog_available"] is True
        mock_harbor_client.list_projects.assert_not_called()

    def test_harbor_unavailable_still_returns_cluster_rows(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "caller-token-abc"
        mock_harbor_client.list_projects = AsyncMock(
            side_effect=HarborUnavailable("no route to host")
        )

        with _cluster_with_one_disk():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        data = response.json()
        assert [item["name"] for item in data["items"]] == ["ubuntu-existing"]
        assert data["catalog_available"] is False

    def test_the_callers_raw_token_is_what_reaches_harbor(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The value the fake receives, not merely that it was called.

        A fake accepting any token (see mock_harbor_client's own docstring)
        would happily confirm an identity scheme that forwards the wrong
        string, or none at all — this is the one assertion that would catch
        that.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "caller-token-abc"

        with _no_cluster_resources():
            client.get("/api/v1/images")

        mock_harbor_client.list_projects.assert_called_once_with("caller-token-abc")

    def test_a_garbage_or_rejected_bearer_gets_no_catalogue_not_an_empty_one(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The headline finding from the real-Harbor e2e run (Task 9).

        GET /api/v2.0/projects returns 200 for ANY bearer, so a wrong
        identity used to come back catalog_available: true with zero rows —
        identical to a valid, legitimately-empty catalogue. verify_identity()
        is what makes these distinguishable: a rejected identity must fail
        before any project is even listed.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "not-a-real-token"
        mock_harbor_client.verify_identity = AsyncMock(
            side_effect=HarborUnauthorized("harbor rejected the caller's identity")
        )

        with _no_cluster_resources():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        data = response.json()
        assert data["catalog_available"] is False
        assert [i for i in data["items"] if i.get("origin") == "catalog"] == []
        # The rejection must be caught before enumeration is ever attempted.
        mock_harbor_client.list_projects.assert_not_called()

    def test_a_valid_identity_with_a_genuinely_empty_catalogue_is_still_available(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other half of the same distinction.

        Same observable shape as an empty catalogue used to be for a wrong
        identity too (zero catalog rows) — the two must now be told apart by
        catalog_available, not conflated back into one case.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "caller-token-abc"
        mock_harbor_client.list_projects = AsyncMock(return_value=[])

        with _no_cluster_resources():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        data = response.json()
        assert data["catalog_available"] is True
        assert [i for i in data["items"] if i.get("origin") == "catalog"] == []

    def test_identity_is_verified_before_projects_are_enumerated(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pins the call order so a later refactor cannot silently reorder it
        back to the pre-fix behaviour (list_projects first, probe second/never).
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "caller-token-abc"

        calls: list[str] = []
        mock_harbor_client.verify_identity = AsyncMock(
            side_effect=lambda token: calls.append("verify_identity")
        )
        mock_harbor_client.list_projects = AsyncMock(
            side_effect=lambda token: calls.append("list_projects") or []
        )

        with _no_cluster_resources():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        assert calls == ["verify_identity", "list_projects"]

    def test_no_token_on_the_request_skips_harbor_entirely(
        self,
        client: TestClient,
        fake_user: User,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AUTH_TYPE=none shape: a User with raw_token left at None.

        fake_user is deliberately left untouched — see the trap warning on
        the fake_user fixture in conftest.py. Sending an empty bearer can
        never succeed, so the handler must not even try, and it must not
        report this the same way as a rejected token.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        assert fake_user.raw_token is None

        with _no_cluster_resources():
            response = client.get("/api/v1/images")

        assert response.status_code == 200
        data = response.json()
        assert data["catalog_available"] is False
        mock_harbor_client.list_projects.assert_not_called()
