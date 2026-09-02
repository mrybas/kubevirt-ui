"""A private Harbor project cannot be pulled anonymously.

CDI resolves secretRef in the DataVolume's OWN namespace, so the Secret has to
be in the target namespace — which is where the harbor-robots chart puts it.

The credential is NEVER named by the caller. `source_registry_secret` and
`source_registry_ca_configmap` used to be request fields, which is how a
request naming `docker://attacker.tld/x:1` could also name the tenant's robot
Secret and have CDI authenticate to the attacker with it. Both are derived
from the resolved registry host now, so every test here configures HARBOR_URL
and the convention names instead of passing them in the body.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.images import build_registry_source
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


@pytest.fixture(autouse=True)
def _a_configured_harbor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_URL", "https://harbor.example")
    monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")


async def test_a_missing_robot_secret_is_named_in_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal happens before any DataVolume/ManagedImage is created.

    A DataVolume created with an unresolvable secretRef fails later, inside
    CDI, as an import error that never mentions the Secret — the user would
    learn nothing useful from that. This has to happen first, and it has to
    name the Secret.

    Driven through `catalog_ref`, because that is the path where the
    credential is the point: a catalogue image is materialised from the
    tenant's Harbor and pulling it anonymously is a failure dressed as a
    success. A caller-supplied `source_registry` that happens to name the same
    host degrades to an anonymous pull instead — see
    `test_a_public_harbor_project_with_no_robot_secret_still_pulls`.
    """
    from app.api.v1 import images

    monkeypatch.delenv("OPERATOR_IMAGE_ENABLED", raising=False)
    # The convention name for this deployment. It is what the URL's host —
    # harbor.example, the configured Harbor — resolves to; nothing in the
    # request names it.
    monkeypatch.setenv("HARBOR_ROBOT_SECRET", "absent-secret")

    k8s = _k8s_with_namespace()
    k8s.core_api.read_namespaced_secret = AsyncMock(
        side_effect=ApiException(status=404)
    )
    k8s.core_api.read_namespaced_config_map = AsyncMock(
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
        catalog_ref="vm-images-tenant-a/ubuntu:1",
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


# ---------------------------------------------------------------------------
# What actually survives being written to the cluster
# ---------------------------------------------------------------------------
#
# The test that used to sit here asserted `secretRef` and `certConfigMap` on
# the ManagedImage body the handler passed to a MagicMock. A MagicMock has no
# schema, so it accepted a body the real API server did not: ManagedImage's CRD
# is a STRUCTURAL schema with no x-kubernetes-preserve-unknown-fields, and
# `RegistrySource` carried only `url` — so the API server PRUNED both fields on
# write. Every catalogue materialise with OPERATOR_IMAGE_ENABLED on passed the
# 422 pre-flight (the Secret does exist), returned 201, and then pulled
# ANONYMOUSLY, failing inside CDI with an error that never mentions a
# credential — verbatim the failure `_harbor_credentials_for` exists to prevent.
#
# A test that asserts a field production discards is worse than no test: it is
# the reason this shipped. So the assertion is made against the real CRD now,
# by pruning the body exactly as the API server would.

_CRD = Path("kubevirt-ui") / "templates" / "operator-crds.yaml"
# In the test container only ./backend is mounted at /app; the chart comes in
# separately at /helm (see docker-compose.yml). Outside it, walk up.
_CRD_CANDIDATES = [Path("/helm") / _CRD, Path(__file__).resolve().parents[2] / "helm" / _CRD]
CRD_FILE = next((c for c in _CRD_CANDIDATES if c.is_file()), None)


def _managed_image_spec_schema() -> dict:
    """ManagedImage's `spec` schema, out of the chart's own CRD.

    The chart's copy rather than `operator/config/crd/bases`, deliberately: it
    is what actually gets applied to a cluster, and it is generated from the
    Go markers by `go run ./cmd/chartsync`, so reading it also catches the two
    drifting apart. The file is a Helm template, but only its first and last
    lines are directives — everything between is plain YAML.
    """
    text = "\n".join(
        line for line in CRD_FILE.read_text().splitlines()
        if not line.lstrip().startswith("{{")
    )
    for doc in yaml.safe_load_all(text):
        if doc and doc.get("spec", {}).get("names", {}).get("kind") == "ManagedImage":
            version = doc["spec"]["versions"][0]
            return version["schema"]["openAPIV3Schema"]["properties"]["spec"]
    raise AssertionError("no ManagedImage CRD in the chart")


def _prune(value, schema: dict):
    """Drop what a structural schema drops, the way the API server does.

    An object whose schema declares `properties` and does not set
    `x-kubernetes-preserve-unknown-fields` keeps only the declared keys.
    Everything else is discarded silently — no error, no warning, no trace in
    the stored object.
    """
    if schema.get("x-kubernetes-preserve-unknown-fields"):
        return value
    if isinstance(value, dict):
        properties = schema.get("properties")
        if properties is None:
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                return {k: _prune(v, extra) for k, v in value.items()}
            return value
        return {
            k: _prune(v, properties[k]) for k, v in value.items() if k in properties
        }
    if isinstance(value, list):
        return [_prune(item, schema.get("items", {})) for item in value]
    return value


@pytest.mark.skipif(
    CRD_FILE is None,
    reason=(
        "Helm chart not reachable; mount it at /helm to run the CRD pruning "
        f"test (looked in: {', '.join(str(c) for c in _CRD_CANDIDATES)})"
    ),
)
async def test_the_managed_image_writer_carries_the_secret_and_ca_past_the_crd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential must survive the CRD, not merely reach the client call.

    Both writers have to carry it — `create_golden_image` builds `source` once
    and the DataVolume path and `_create_managed_image` consume the same dict —
    but on the operator path the CR is an extra hop through a schema, and that
    hop is where it was being lost.
    """
    from app.api.v1 import images

    monkeypatch.setenv("OPERATOR_IMAGE_ENABLED", "true")
    monkeypatch.setenv("HARBOR_ROBOT_SECRET", "harbor-robot-tenant-a")
    monkeypatch.setenv("HARBOR_CA_CONFIGMAP", "harbor-ca")

    k8s = _k8s_with_namespace()
    k8s.core_api.read_namespaced_secret = AsyncMock(return_value=MagicMock())
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=MagicMock())

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
        catalog_ref="vm-images-tenant-a/ubuntu:1",
        size="10Gi",
    )

    with patch.object(images.client, "CustomObjectsApi", return_value=api):
        await images.create_golden_image(
            image=image, request=request, user=MagicMock(), namespace="opdev-dev",
        )

    # Proves the operator path, not the DataVolume path, is what got exercised.
    assert captured["plural"] == "managedimages"
    assert captured["body"]["kind"] == "ManagedImage"

    stored = _prune(captured["body"]["spec"], _managed_image_spec_schema())
    registry = stored["source"]["registry"]

    assert registry["url"].startswith("docker://")
    assert registry["secretRef"] == "harbor-robot-tenant-a", (
        "secretRef was pruned by the CRD: the ManagedImage would be stored "
        "without it and the operator would render an anonymous pull"
    )
    assert registry["certConfigMap"] == "harbor-ca"


@pytest.mark.skipif(CRD_FILE is None, reason="Helm chart not reachable")
def test_the_crd_declares_every_field_the_registry_source_carries() -> None:
    """The direct form of the same contract, so a failure names the cause.

    The test above fails with "secretRef was pruned"; this one says which
    field the schema is missing, which is the thing to go and add.
    """
    schema = _managed_image_spec_schema()
    registry = schema["properties"]["source"]["properties"]["registry"]

    for field in ("url", "secretRef", "certConfigMap"):
        assert field in registry["properties"], (
            f"RegistrySource has no {field!r} in the CRD, so the API server "
            "prunes it on write and it never reaches the operator. Add it to "
            "operator/api/v1alpha1/managedimage_types.go, re-run "
            "`make manifests` and `go run ./cmd/chartsync`, and render it in "
            "operator/internal/cdi/render.go"
        )
