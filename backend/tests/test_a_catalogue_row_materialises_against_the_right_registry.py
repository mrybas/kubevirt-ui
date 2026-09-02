"""C1 + C2: what the browser sends, and what the backend makes of it.

The browser sends the host-less `catalog_ref` it was given
("project/repo:tag") and the target namespace. Everything else — the registry
host, the `docker://` scheme, the robot credential, the CA — is added here.

Two failures this guards, both of which shipped green:

  1. `catalog_ref` sent as `source_registry` reaches CDI without a host, so
     the pull resolves against Docker Hub; and the stored `source_url` then
     has no `docker://` prefix, so `catalog_ref_from_source_url` returns None
     and the finished disk never merges back with its catalogue row. The
     unified list — the point of the feature — shows one image as two rows
     permanently.

  2. No credential is attached at all, so every materialise is an anonymous
     pull: it works against a public project and fails against every private
     one, which is most of them.

Both were invisible because the frontend test asserted the mutation was
CALLED, never with what. So every assertion here is about the request body
that actually reaches the cluster.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.images_catalog import catalog_ref_from_source_url
from app.models.template import GoldenImageCreate


def _k8s(*, secret_exists: bool = True, ca_exists: bool = False) -> MagicMock:
    k8s = MagicMock()
    ns = MagicMock()
    ns.metadata.labels = {}
    k8s.core_api.read_namespace = AsyncMock(return_value=ns)
    k8s.core_api.list_namespaced_resource_quota = AsyncMock(
        return_value=SimpleNamespace(items=[])
    )
    k8s.core_api.read_namespaced_secret = AsyncMock(
        return_value=MagicMock()
        if secret_exists
        else None,
        side_effect=None if secret_exists else ApiException(status=404),
    )
    k8s.core_api.read_namespaced_config_map = AsyncMock(
        return_value=MagicMock() if ca_exists else None,
        side_effect=None if ca_exists else ApiException(status=404),
    )
    return k8s


async def _create(k8s: MagicMock, image: GoldenImageCreate, namespace="tenant-a"):
    """Call the real create handler, returning (captured create kwargs, result)."""
    from app.api.v1 import images

    captured: dict = {}

    async def _create_obj(**kwargs):
        captured.update(kwargs)
        body = dict(kwargs["body"])
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"].get("generateName", "") + "x7k2p"
        body["metadata"]["creationTimestamp"] = "2026-09-02T00:00:00Z"
        return body

    api = MagicMock()
    api.list_namespaced_custom_object = AsyncMock(return_value={"items": []})
    api.create_namespaced_custom_object = AsyncMock(side_effect=_create_obj)

    request = MagicMock()
    request.app.state.k8s_client = k8s

    with patch.object(images.client, "CustomObjectsApi", return_value=api):
        result = await images.create_golden_image(
            image=image, request=request, user=MagicMock(), namespace=namespace,
        )
    return captured, result


@pytest.fixture(autouse=True)
def _a_configured_harbor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_URL", "https://harbor.example:443")
    monkeypatch.delenv("OPERATOR_IMAGE_ENABLED", raising=False)


class TestTheRegistryUrlTheBackendBuilds:
    async def test_a_catalog_ref_becomes_a_docker_url_against_harbors_own_host(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="Ubuntu 22.04",
                catalog_ref="vm-images-tenant-a/ubuntu-2204:20260901",
                size="10Gi",
            ),
        )

        url = captured["body"]["spec"]["source"]["registry"]["url"]
        assert url == (
            "docker://harbor.example:443/vm-images-tenant-a/ubuntu-2204:20260901"
        )

    async def test_the_stored_source_url_is_what_the_merge_key_parses_back(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The round trip that keeps one image from becoming two rows.

        `merge()` re-joins a disk with its catalogue row by running
        `catalog_ref_from_source_url` over the disk's stored source_url. That
        returns None for anything without a `docker://` prefix — which is
        precisely what a host-less `catalog_ref` sent as `source_registry`
        stored. This asserts the value actually survives the round trip.
        """
        ref = "vm-images-tenant-a/ubuntu-2204:20260901"
        _, created = await _create(
            _k8s(),
            GoldenImageCreate(display_name="U", catalog_ref=ref, size="10Gi"),
        )

        assert catalog_ref_from_source_url(created.source_url) == ref

    async def test_no_harbor_url_is_refused_rather_than_pulling_from_docker_hub(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_URL", "")

        with pytest.raises(HTTPException) as exc_info:
            await _create(
                _k8s(),
                GoldenImageCreate(
                    display_name="U", catalog_ref="p/u:1", size="10Gi"
                ),
            )

        assert exc_info.value.status_code == 503


class TestTheCredentialTheBackendAttaches:
    async def test_the_robot_secret_is_attached_by_convention(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The browser never names a credential; the backend knows the name."""
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="U", catalog_ref="p/u:1", size="10Gi"
            ),
        )

        assert captured["body"]["spec"]["source"]["registry"]["secretRef"] == (
            "harbor-robot"
        )

    async def test_the_convention_follows_the_deployments_own_name(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_ROBOT_SECRET", "tenant-pull-creds")

        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(display_name="U", catalog_ref="p/u:1", size="10Gi"),
        )

        assert captured["body"]["spec"]["source"]["registry"]["secretRef"] == (
            "tenant-pull-creds"
        )

    async def test_a_missing_robot_secret_is_refused_loudly_not_pulled_anonymously(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Task 5's 422, now reached by the convention path too.

        The alternative — quietly dropping the secretRef — is the defect this
        replaces: an anonymous pull that works against a public project and
        dies inside CDI against a private one, with an error that never
        mentions a credential.
        """
        with pytest.raises(HTTPException) as exc_info:
            await _create(
                _k8s(secret_exists=False),
                GoldenImageCreate(
                    display_name="U", catalog_ref="p/u:1", size="10Gi"
                ),
            )

        assert exc_info.value.status_code == 422
        assert "harbor-robot" in exc_info.value.detail
        assert "tenant-a" in exc_info.value.detail

    async def test_the_ca_config_map_is_attached_when_it_is_really_there(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _ = await _create(
            _k8s(ca_exists=True),
            GoldenImageCreate(display_name="U", catalog_ref="p/u:1", size="10Gi"),
        )

        assert captured["body"]["spec"]["source"]["registry"]["certConfigMap"] == (
            "harbor-ca"
        )

    async def test_an_absent_ca_config_map_is_omitted_rather_than_named(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CDI refuses an import outright when certConfigMap names nothing.

        A Harbor behind a publicly trusted certificate has no such ConfigMap,
        so naming one unconditionally would break exactly the simplest
        deployment.
        """
        captured, _ = await _create(
            _k8s(ca_exists=False),
            GoldenImageCreate(display_name="U", catalog_ref="p/u:1", size="10Gi"),
        )

        assert "certConfigMap" not in captured["body"]["spec"]["source"]["registry"]


class TestWhatTheCallerMayStillOverride:
    async def test_an_explicit_source_registry_still_wins(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Terraform/CLI callers that already send a full URL are untouched."""
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="U",
                source_registry="docker://other.example/p/u:1",
                size="10Gi",
            ),
        )

        assert captured["body"]["spec"]["source"]["registry"] == {
            "url": "docker://other.example/p/u:1"
        }

    async def test_an_explicit_secret_wins_over_the_convention(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="U",
                catalog_ref="p/u:1",
                source_registry_secret="a-specific-secret",
                size="10Gi",
            ),
        )

        assert captured["body"]["spec"]["source"]["registry"]["secretRef"] == (
            "a-specific-secret"
        )


class TestTheRefIsNotAFreeTextField:
    @pytest.mark.parametrize(
        "bad",
        [
            "docker://harbor.example/p/u:1",   # a scheme of its own
            "../../etc/passwd:1",              # traversal
            "p/u:1?page=2",                    # query smuggling
            "no-slash:1",                      # not project/repository
            "p/u",                             # no tag
            "p/u:tag with space",
        ],
    )
    def test_a_ref_that_is_not_project_repository_tag_is_refused(self, bad: str) -> None:
        """It is interpolated into a URL, so the shape is validated, not trusted."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            GoldenImageCreate(display_name="U", catalog_ref=bad, size="10Gi")

    def test_a_multi_segment_repository_is_still_accepted(self) -> None:
        """Harbor repository names are frequently "team/subimage"."""
        image = GoldenImageCreate(
            display_name="U",
            catalog_ref="vm-images-public/team/subimage:20260901",
            size="10Gi",
        )

        assert image.catalog_ref == "vm-images-public/team/subimage:20260901"
