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

@pytest.fixture(autouse=True)
def a_visible_catalogue_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Materialise now asks Harbor, as the caller, whether they may see the ref.

    Patched out here, deliberately and only here: these tests are about what
    the handler WRITES, and satisfying the check for real would mean a fake
    Harbor in every one of them. The rule itself is proven in
    `test_a_catalogue_ref_you_cannot_see_is_not_yours_to_pull.py`, which is
    also the file to change if it ever moves — a check patched away in two
    places is a check that can be deleted without a test noticing.
    """
    from app.api.v1 import images

    async def _visible(harbor, token, catalog_ref):
        return None

    monkeypatch.setattr(images, "assert_catalogue_ref_visible", _visible)


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
    # The catalogue path is gated by the same flag the list and publish paths
    # use. It used to be the one Harbor path with no gate at all.
    monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
    monkeypatch.delenv("OPERATOR_IMAGE_ENABLED", raising=False)
    monkeypatch.delenv("HARBOR_ROBOT_SECRET", raising=False)
    monkeypatch.delenv("HARBOR_CA_CONFIGMAP", raising=False)


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

    async def test_a_transport_failure_reading_the_ca_does_not_fail_the_materialise(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The CA read is a best-effort look at an OPTIONAL object.

        `except ApiException` alone let a reset connection or a DNS blip
        escape as a 500 that failed the whole materialise — over a ConfigMap
        that may legitimately not exist at all. The docstring already promised
        this behaviour; the code only implemented half of it.
        """
        k8s = _k8s()
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            side_effect=TimeoutError("connection reset")
        )

        captured, _ = await _create(
            k8s,
            GoldenImageCreate(display_name="U", catalog_ref="p/u:1", size="10Gi"),
        )

        registry = captured["body"]["spec"]["source"]["registry"]
        assert "certConfigMap" not in registry
        # The pull itself is still fully formed — this is a degradation, not
        # a failure.
        assert registry["url"].startswith("docker://")
        assert registry["secretRef"] == "harbor-robot"

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

    async def test_a_caller_supplied_registry_at_harbors_own_host_is_authenticated(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The credential follows the HOST, so Harbor's own host still gets it.

        This is the other half of the rule the security tests below pin: the
        decision is made from the resolved host and from nothing else, so a
        full URL naming the configured Harbor is treated exactly like a
        catalogue selection would be.
        """
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="U",
                source_registry="docker://harbor.example:443/p/u:1",
                size="10Gi",
            ),
        )

        assert captured["body"]["spec"]["source"]["registry"]["secretRef"] == (
            "harbor-robot"
        )

    async def test_a_public_harbor_project_with_no_robot_secret_still_pulls(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An absent Secret refuses a CATALOGUE pull and degrades a raw one.

        A request that names its own `source_registry` pulled anonymously
        before this feature existed, and a deployment with a public Harbor
        project and no robot Secret is not misconfigured — so it keeps
        working. The catalogue path is the one where the credential is the
        point, and there the same absence is a 422 (see
        test_a_missing_robot_secret_is_refused_loudly_not_pulled_anonymously).
        """
        captured, _ = await _create(
            _k8s(secret_exists=False),
            GoldenImageCreate(
                display_name="U",
                source_registry="docker://harbor.example:443/p/u:1",
                size="10Gi",
            ),
        )

        assert captured["body"]["spec"]["source"]["registry"] == {
            "url": "docker://harbor.example:443/p/u:1"
        }


class TestTheCredentialCannotBeAimedAtAnotherRegistry:
    """BLOCKER: the tenant's Harbor robot password, sent wherever asked.

    `source_registry_secret` used to be a request field, and the catalogue
    branch read `if image.catalog_ref and not registry_url` — so supplying
    `source_registry` skipped host derivation entirely while the secret
    flowed through untouched. A single POST with

        {"source_registry": "docker://attacker.tld/x:1",
         "source_registry_secret": "harbor-robot"}

    made CDI authenticate to the attacker's registry with the tenant's robot
    credential. At the commit this branch started from, a registry source was
    built with no secretRef at all, so the same request got an anonymous pull:
    the credential was added by this feature and had to be taken back off the
    request.

    The fix is not a URL allow-list. The credential is derived from the
    RESOLVED HOST and the fields are gone.
    """

    async def test_a_non_harbor_registry_gets_no_secret_ref(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exact attack, through the handler: no credential comes out."""
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="U",
                source_registry="docker://attacker.tld/x:1",
                size="10Gi",
            ),
        )

        registry = captured["body"]["spec"]["source"]["registry"]
        assert registry == {"url": "docker://attacker.tld/x:1"}
        assert "secretRef" not in registry
        assert "certConfigMap" not in registry

    async def test_the_credential_fields_no_longer_exist_on_the_request(self) -> None:
        """Removed, not validated. A field that is not there cannot be abused.

        Pydantic ignores unknown keys, so a client still sending the old
        fields is not broken — the values are simply dropped on the floor,
        which is the whole point.
        """
        assert "source_registry_secret" not in GoldenImageCreate.model_fields
        assert "source_registry_ca_configmap" not in GoldenImageCreate.model_fields

        image = GoldenImageCreate(
            display_name="U",
            source_registry="docker://attacker.tld/x:1",
            source_registry_secret="harbor-robot",
            source_registry_ca_configmap="harbor-ca",
            size="10Gi",
        )
        assert not hasattr(image, "source_registry_secret")
        assert not hasattr(image, "source_registry_ca_configmap")

    @pytest.mark.parametrize(
        "url",
        [
            # Userinfo: the authority's host is attacker.tld, and reading the
            # host off the left of the "@" is the classic way to be fooled.
            "docker://harbor.example:443@attacker.tld/x:1",
            # A different port on the right host is a different registry.
            "docker://harbor.example:8443/x:1",
            # A prefix that is not the host.
            "docker://harbor.example.attacker.tld/x:1",
            # Not a docker:// URL at all.
            "https://harbor.example:443/x:1",
        ],
    )
    async def test_a_url_that_only_looks_like_harbor_gets_no_secret_ref(
        self, url: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(display_name="U", source_registry=url, size="10Gi"),
        )

        assert "secretRef" not in captured["body"]["spec"]["source"]["registry"]

    async def test_a_catalog_ref_and_a_source_registry_together_are_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two sources for one disk. Preferring one silently is how the
        bypass worked: `if image.catalog_ref and not registry_url` meant a
        caller could send both and have the catalogue half ignored."""
        with pytest.raises(HTTPException) as exc_info:
            await _create(
                _k8s(),
                GoldenImageCreate(
                    display_name="U",
                    catalog_ref="p/u:1",
                    source_registry="docker://attacker.tld/x:1",
                    size="10Gi",
                ),
            )

        assert exc_info.value.status_code == 422


class TestTheCatalogueHalfIsGatedLikeEverythingElseHarbor:
    """BLOCKER: the flag gated 2 of 3 Harbor paths.

    `harbor_image_path_enabled()` appeared at the list handler and at publish,
    and nowhere in materialise — so "with HARBOR_IMAGE_ENABLED unset the
    behaviour is unchanged" was false, and the credential-handling code above
    ran on every deployment whether it had asked for Harbor or not.
    """

    async def test_a_catalogue_materialise_with_the_flag_off_is_501(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("HARBOR_IMAGE_ENABLED", raising=False)

        with pytest.raises(HTTPException) as exc_info:
            await _create(
                _k8s(),
                GoldenImageCreate(display_name="U", catalog_ref="p/u:1", size="10Gi"),
            )

        assert exc_info.value.status_code == 501

    async def test_the_flag_off_leaves_ordinary_sources_alone(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The gate is on the catalogue path, not on the endpoint."""
        monkeypatch.delenv("HARBOR_IMAGE_ENABLED", raising=False)

        captured, _ = await _create(
            _k8s(),
            GoldenImageCreate(
                display_name="U",
                source_url="https://cloud-images.example/x.img",
                size="10Gi",
            ),
        )

        assert captured["body"]["spec"]["source"] == {
            "http": {"url": "https://cloud-images.example/x.img"}
        }


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
