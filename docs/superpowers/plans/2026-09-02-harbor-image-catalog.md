# Harbor Image Catalogue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Harbor-backed image path to kubevirt-ui — browse a Harbor catalogue with the user's own token, materialise an artifact into a disk, and publish a running VM's disk back to Harbor.

**Architecture:** A new `harbor_client.py` holds no service credential and takes the caller's bearer token per call, so Harbor authorises browsing per user. The existing image endpoints move out of the 1802-line `templates.py` into their own module first, as a pure move. `GET /images` then merges cluster DataVolumes with Harbor artifacts on registry URL, degrading to cluster-only when Harbor is unreachable.

**Tech Stack:** FastAPI, `kubernetes_asyncio`, `httpx` 0.28.1, pydantic; React with `@tanstack/react-query`; pytest 8.3.4 with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-09-01-harbor-image-catalog-design.md`

## Deliberate deviation from the spec

The spec names a new `app/models/image.py`. This plan instead extends
`VMImage` in `app/models/template.py`, because that model already exists, is
already returned by `GET /images` as `GoldenImageListResponse`, and is already
consumed by the frontend. A parallel model would mean two shapes for one
concept and a translation layer between them. The spec's intent — catalogue
rows carry provenance — is met by two new fields with defaults.

## Global Constraints

- Branch: `feat/harbor-image-catalog`. Never commit to `main`.
- Python: async tests need **no** `@pytest.mark.asyncio` — `asyncio_mode = "auto"` is set in `pyproject.toml`.
- HTTP mocking: **`httpx.MockTransport`**. `respx` is not a dependency and must not be added.
- `harbor_client.py` must **never** hold a service credential. Every public method takes `token: str` as its first parameter. A method that reaches Harbor's management API without a caller-supplied token is a defect, not a shortcut.
- Commit style, matching this repo: `type(scope): lowercase sentence that reads like prose`. Examples in `git log`: `fix(tenants): the page was admin-only, and the role allowed to use it is not`.
- Test names state behaviour, matching `backend/tests/`: `test_an_image_you_asked_for_is_visible.py`.
- Existing image endpoints keep their paths and response shapes. This plan adds fields; it removes none.
- `HARBOR_IMAGE_ENABLED` gates every new endpoint. With it unset, behaviour must be byte-identical to today.
- Harbor management-API calls use the user's `id_token`. Registry operations (pull, push) use the tenant robot Secret. Never the reverse — measured: robots get 401 on the management API, at either level.

---

### Task 1: Move the image endpoints out of templates.py

Pure move, no behaviour change, so a later regression bisects to the feature and not to this. `templates.py` is 1802 lines and this plan adds substantially to the image code.

**Files:**
- Create: `backend/app/api/v1/images.py`
- Modify: `backend/app/api/v1/templates.py` (remove `images_router` and the five image endpoints at lines 40, 678, 1221, 1490, 1535, 1676)
- Modify: `backend/app/api/v1/router.py:18` (import site)
- Test: `backend/tests/test_an_image_you_asked_for_is_visible.py` (must pass unchanged)

**Interfaces:**
- Consumes: nothing
- Produces: `backend/app/api/v1/images.py` exporting `images_router: APIRouter` with the same five routes: `GET ""`, `POST ""`, `DELETE "/{name}"`, `PATCH "/{name}"`, `POST "/from-disk"`

- [ ] **Step 1: Confirm the existing image tests pass before touching anything**

Run: `cd backend && python -m pytest tests/test_an_image_you_asked_for_is_visible.py -v`
Expected: PASS. If it fails now, stop and report — this task's whole safety property is that the test result does not change.

- [ ] **Step 2: Create the new module with the image endpoints moved verbatim**

Create `backend/app/api/v1/images.py`. Move `images_router = APIRouter()` (currently `templates.py:40`) and the five endpoint functions into it, unchanged. Copy across only the imports those functions actually use:

```python
"""Image endpoints (CDI DataVolumes, and the Harbor catalogue).

Split out of templates.py, which had grown past 1800 lines. Templates and
images are separate concerns that happened to share a file.
"""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from app.core.auth import User, require_auth
from app.core.naming import DISPLAY_NAME_ANNOTATION, SLUG_LABEL, sanitize_display_name
from app.core.storage_headroom import assert_storage_headroom
from app.models.template import GoldenImage, GoldenImageListResponse

logger = logging.getLogger(__name__)

images_router = APIRouter()

# ... the five endpoint functions, moved unchanged ...
```

Do not rename anything. Do not "improve" the moved code. If an import turns out to be unused after the move, remove it from the new file only.

- [ ] **Step 3: Update the import site**

In `backend/app/api/v1/router.py:18`, split the import:

```python
from app.api.v1.templates import router as templates_router
from app.api.v1.images import images_router
```

Leave `router.py:88` (`protected.include_router(images_router, prefix="/images", tags=["Images"])`) untouched.

- [ ] **Step 4: Verify nothing changed**

Run: `cd backend && python -m pytest tests/ -q`
Expected: the same pass/fail counts as Step 1. Any newly failing test means something was altered during the move, not that a test is wrong.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/images.py backend/app/api/v1/templates.py backend/app/api/v1/router.py
git commit -m "refactor(images): the endpoints move out of templates.py, unchanged"
```

---

### Task 2: The Harbor client

**Files:**
- Create: `backend/app/core/harbor_client.py`
- Test: `backend/tests/test_the_harbor_client_never_uses_a_shared_identity.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `HARBOR_URL: str` — module constant from env, default `""`
  - `class HarborClient` with:
    - `async def list_projects(self, token: str) -> list[dict[str, Any]]`
    - `async def list_repositories(self, token: str, project: str) -> list[dict[str, Any]]`
    - `async def list_artifacts(self, token: str, project: str, repository: str) -> list[dict[str, Any]]`
  - `class HarborUnavailable(Exception)` — raised on transport failure or 5xx
  - `class HarborUnauthorized(Exception)` — raised on 401/403

Two exception types, not one: an expired token and a down registry need different user actions, and the endpoint in Task 4 reports them differently.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_the_harbor_client_never_uses_a_shared_identity.py`:

```python
"""The Harbor client must carry the caller's identity, never its own.

Browsing is authorised per user by Harbor. If the client could hold a service
credential, every caller would see everything and kubevirt-ui would have to
filter — a second, weaker copy of a decision Harbor already makes correctly.
"""

import httpx
import pytest

from app.core.harbor_client import (
    HarborClient,
    HarborUnauthorized,
    HarborUnavailable,
)


def _client(handler) -> HarborClient:
    c = HarborClient(base_url="https://harbor.example")
    c._transport = httpx.MockTransport(handler)
    return c


async def test_the_caller_token_is_what_reaches_harbor():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[{"name": "vm-images-public"}])

    await _client(handler).list_projects("user-token-abc")

    assert seen["auth"] == "Bearer user-token-abc"


async def test_a_rejected_token_is_not_reported_as_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})

    with pytest.raises(HarborUnauthorized):
        await _client(handler).list_projects("expired-token")


async def test_an_unreachable_harbor_is_not_reported_as_a_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(HarborUnavailable):
        await _client(handler).list_projects("fine-token")


async def test_repositories_are_read_from_the_project_scoped_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"name": "vm-images-public/ubuntu-2204"}])

    await _client(handler).list_repositories("t", "vm-images-public")

    assert seen["path"] == "/api/v2.0/projects/vm-images-public/repositories"


async def test_artifacts_are_read_per_repository_not_per_project():
    """The project-wide artifact listing returns 401 for scoped identities.

    Browsing goes project -> repositories -> artifacts-of-one-repository, so
    the client must not reach for /projects/{p}/artifacts.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"tags": [{"name": "20260901"}]}])

    await _client(handler).list_artifacts("t", "vm-images-public", "ubuntu-2204")

    assert seen["path"] == (
        "/api/v2.0/projects/vm-images-public/repositories/ubuntu-2204/artifacts"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_the_harbor_client_never_uses_a_shared_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.harbor_client'`

- [ ] **Step 3: Write the client**

Create `backend/app/core/harbor_client.py`:

```python
"""Harbor management API client.

Unlike app/core/lldap_client.py, this client holds NO service credential.
Every method takes the caller's bearer token as its first argument, so there
is no code path that can reach Harbor's management API as a shared identity.

Measured on Harbor 2.15.2: a dex-issued id_token works as an API bearer and
Harbor applies that user's roles. Robot accounts do NOT work here at all, at
either level — they are for the registry API (pull and push) only.
"""

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

HARBOR_URL = os.getenv("HARBOR_URL", "").rstrip("/")
HARBOR_TIMEOUT_SECONDS = float(os.getenv("HARBOR_TIMEOUT_SECONDS", "10"))


class HarborUnavailable(Exception):
    """Harbor could not be reached, or answered 5xx."""


class HarborUnauthorized(Exception):
    """Harbor rejected the caller's token."""


class HarborClient:
    """Read-only client for Harbor's management API."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or HARBOR_URL).rstrip("/")
        self._transport: httpx.AsyncBaseTransport | None = None

    async def _get(self, token: str, path: str) -> list[dict[str, Any]]:
        """GET a list from Harbor.

        The two designed exceptions are exhaustive: every failure path leaves
        this method as HarborUnauthorized or HarborUnavailable, never as a raw
        httpx or json error. A caller that catches only the documented two must
        not be able to receive a third type -- that surfaces as an unhandled
        500 rather than a clean message.
        """
        url = f"{self._base_url}/api/v2.0{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=HARBOR_TIMEOUT_SECONDS
            ) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError as exc:
            raise HarborUnavailable(str(exc)) from exc

        if resp.status_code in (401, 403):
            raise HarborUnauthorized(f"Harbor rejected the token for {path}")
        if resp.status_code >= 400:
            # Everything else that is not success -- 404, 400, 409, 422 -- is a
            # registry we cannot get an answer from. Letting raise_for_status()
            # escape here would emit httpx.HTTPStatusError, a third type.
            raise HarborUnavailable(f"Harbor returned {resp.status_code} for {path}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise HarborUnavailable(f"Harbor returned a non-JSON body for {path}") from exc
        return body if isinstance(body, list) else []

    async def list_projects(self, token: str) -> list[dict[str, Any]]:
        return await self._get(token, "/projects?page_size=100")

    async def list_repositories(self, token: str, project: str) -> list[dict[str, Any]]:
        # quote(safe="") because Harbor repository names are routinely
        # multi-segment ("team/subimage"). An unencoded slash does not error --
        # it addresses a DIFFERENT resource, which is the dangerous failure.
        p = quote(project, safe="")
        return await self._get(token, f"/projects/{p}/repositories?page_size=100")

    async def list_artifacts(
        self, token: str, project: str, repository: str
    ) -> list[dict[str, Any]]:
        p, r = quote(project, safe=""), quote(repository, safe="")
        return await self._get(
            token, f"/projects/{p}/repositories/{r}/artifacts?page_size=100"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_the_harbor_client_never_uses_a_shared_identity.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/harbor_client.py backend/tests/test_the_harbor_client_never_uses_a_shared_identity.py
git commit -m "feat(harbor): a client that carries the caller's token and never its own"
```

---

### Task 3: Catalogue fields on the image model, and the feature flag

**Files:**
- Modify: `backend/app/models/template.py:128-150` (`VMImage`), `:203-207` (`VMImageListResponse`)
- Modify: `backend/app/core/operator.py` (add one function beside the existing `*_path_enabled`)
- Modify: `backend/app/api/v1/router.py:45-46` (`GET /features`)
- Test: `backend/tests/test_the_catalog_flag_keeps_harbor_out_of_sight.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `VMImage.origin: str` — `"cluster"` or `"catalog"`, default `"cluster"`
  - `VMImage.catalog_ref: str | None` — `"<project>/<repository>:<tag>"`, default `None`
  - `VMImageListResponse.catalog_available: bool` — default `True`
  - `app.core.operator.harbor_image_path_enabled() -> bool`
  - `GET /features` gains `enableHarborImages: bool`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_the_catalog_flag_keeps_harbor_out_of_sight.py`:

```python
"""With HARBOR_IMAGE_ENABLED unset, nothing about Harbor may be observable."""

import app.core.operator as operator


def test_the_harbor_path_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("HARBOR_IMAGE_ENABLED", raising=False)
    assert operator.harbor_image_path_enabled() is False


def test_the_harbor_path_turns_on_with_the_same_truthy_words_as_its_siblings(monkeypatch):
    for word in ("true", "1", "yes"):
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", word)
        assert operator.harbor_image_path_enabled() is True, word


def test_an_image_defaults_to_being_a_cluster_image():
    from app.models.template import VMImage

    img = VMImage(name="ubuntu", namespace="default", status="Ready")

    assert img.origin == "cluster"
    assert img.catalog_ref is None


def test_a_list_claims_the_catalog_is_present_unless_told_otherwise():
    from app.models.template import VMImageListResponse

    assert VMImageListResponse(items=[], total=0).catalog_available is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_the_catalog_flag_keeps_harbor_out_of_sight.py -v`
Expected: FAIL — `AttributeError: module 'app.core.operator' has no attribute 'harbor_image_path_enabled'`

- [ ] **Step 3: Add the flag**

Append to `backend/app/core/operator.py`, following the shape of the existing functions:

```python
def harbor_image_path_enabled() -> bool:
    """True when images may also come from a Harbor catalogue.

    Off: the image list is exactly the cluster's DataVolumes, as before.
    On: the list additionally carries Harbor artifacts the requesting user is
    allowed to see, and the publish and materialise endpoints are mounted.
    """
    return _enabled("HARBOR_IMAGE_ENABLED")
```

- [ ] **Step 4: Add the model fields**

In `backend/app/models/template.py`, add to `VMImage` after `persistent`:

```python
    # Where this row came from. "cluster" is a DataVolume that exists; "catalog"
    # is a Harbor artifact that has not been materialised yet.
    origin: str = "cluster"
    # "<project>/<repository>:<tag>" when the row has a Harbor counterpart.
    # Present on catalog rows and on cluster rows imported from Harbor, which is
    # what lets the two be merged into one row.
    catalog_ref: str | None = None
```

And to `VMImageListResponse`:

```python
    # False when the catalogue could not be read. The cluster rows are still
    # correct and complete; only the catalogue half is missing. The list must
    # never fail outright because Harbor is down.
    catalog_available: bool = True
```

- [ ] **Step 5: Surface the flag to the frontend**

In `backend/app/api/v1/router.py`, extend the `/features` handler to include `enableHarborImages`, importing `harbor_image_path_enabled` from `app.core.operator`:

```python
@router.get("/features", tags=["Features"])
async def get_features():
    return {
        "enableTenants": tenant_path_enabled(),
        "enableHarborImages": harbor_image_path_enabled(),
    }
```

Keep any other keys the handler already returns; add, do not replace.

- [ ] **Step 6: Run the tests**

Run: `cd backend && python -m pytest tests/test_the_catalog_flag_keeps_harbor_out_of_sight.py tests/test_an_image_you_asked_for_is_visible.py -v`
Expected: all pass — the new fields have defaults, so existing image tests are unaffected.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/template.py backend/app/core/operator.py backend/app/api/v1/router.py backend/tests/test_the_catalog_flag_keeps_harbor_out_of_sight.py
git commit -m "feat(images): a row can say it came from a catalogue, behind a flag"
```

---

### Task 4: Merge the catalogue into the image list

**Files:**
- Modify: `backend/app/api/v1/images.py` (the `GET ""` handler from Task 1)
- Create: `backend/app/api/v1/images_catalog.py` (merge logic, kept separate so it is unit-testable without FastAPI)
- Modify: `backend/tests/conftest.py` (add the fake Harbor fixture)
- Test: `backend/tests/test_a_catalog_image_and_its_disk_are_one_row.py`, `backend/tests/test_harbor_being_down_does_not_hide_local_images.py`

**Interfaces:**
- Consumes: `HarborClient`, `HarborUnavailable`, `HarborUnauthorized` (Task 2); `VMImage.origin`, `VMImage.catalog_ref`, `VMImageListResponse.catalog_available` (Task 3)
- Produces:
  - `app.api.v1.images_catalog.catalog_ref_from_source_url(source_url: str | None) -> str | None`
  - `app.api.v1.images_catalog.merge(cluster: list[VMImage], catalog: list[VMImage]) -> list[VMImage]`
  - `app.api.v1.images_catalog.catalog_images(harbor: HarborClient, token: str) -> list[VMImage]`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_a_catalog_image_and_its_disk_are_one_row.py`:

```python
"""A disk imported from Harbor is the same image as its catalogue entry.

Showing both would tell the user they have two Ubuntu images when they have
one, and leave them guessing which to boot.
"""

from app.api.v1.images_catalog import catalog_ref_from_source_url, merge
from app.models.template import VMImage


def _cluster(name, ref_url=None):
    return VMImage(name=name, namespace="default", status="Ready", source_url=ref_url)


def _catalog(ref):
    return VMImage(
        name=ref.split("/")[-1].split(":")[0],
        namespace="",
        status="Catalog",
        origin="catalog",
        catalog_ref=ref,
    )


def test_a_registry_url_yields_the_catalog_coordinate():
    assert (
        catalog_ref_from_source_url(
            "docker://harbor.example/vm-images-public/ubuntu-2204:20260901"
        )
        == "vm-images-public/ubuntu-2204:20260901"
    )


def test_a_non_registry_url_has_no_catalog_coordinate():
    assert catalog_ref_from_source_url("https://cloud-images.example/x.img") is None
    assert catalog_ref_from_source_url(None) is None


def test_the_disk_wins_when_both_sides_describe_it():
    ref = "vm-images-public/ubuntu-2204:20260901"
    disk = _cluster("ubuntu-2204", f"docker://harbor.example/{ref}")

    rows = merge([disk], [_catalog(ref)])

    assert len(rows) == 1
    assert rows[0].origin == "cluster"
    assert rows[0].status == "Ready"
    assert rows[0].catalog_ref == ref


def test_an_unmaterialised_catalog_entry_still_appears():
    rows = merge([], [_catalog("vm-images-public/rocky-9:20260901")])

    assert [r.origin for r in rows] == ["catalog"]


def test_a_local_disk_with_no_catalog_counterpart_still_appears():
    rows = merge([_cluster("scratch")], [])

    assert [r.name for r in rows] == ["scratch"]
```

Create `backend/tests/test_harbor_being_down_does_not_hide_local_images.py`:

```python
"""A registry outage must not stop anyone booting a VM from a disk they have.

This is the reason the catalogue is not the source of truth for the list.
"""

import pytest

from app.api.v1.images_catalog import catalog_images
from app.core.harbor_client import HarborUnauthorized, HarborUnavailable


class _Down:
    async def list_projects(self, token):
        raise HarborUnavailable("no route to host")


class _Rejecting:
    async def list_projects(self, token):
        raise HarborUnauthorized("token expired")


async def test_an_unreachable_harbor_raises_rather_than_returning_nothing():
    """The caller must be able to tell 'no images' from 'could not ask'."""
    with pytest.raises(HarborUnavailable):
        await catalog_images(_Down(), "tok")


async def test_a_rejected_token_stays_distinguishable_from_an_outage():
    with pytest.raises(HarborUnauthorized):
        await catalog_images(_Rejecting(), "tok")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && python -m pytest tests/test_a_catalog_image_and_its_disk_are_one_row.py tests/test_harbor_being_down_does_not_hide_local_images.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.v1.images_catalog'`

- [ ] **Step 3: Write the merge module**

Create `backend/app/api/v1/images_catalog.py`:

```python
"""Merging the Harbor catalogue into the cluster's image list.

Kept out of the endpoint module so the merge rule can be tested without
FastAPI, a Kubernetes client, or a Harbor.
"""

import logging
from typing import Any
from urllib.parse import urlparse

from app.core.harbor_client import HarborClient
from app.models.template import VMImage

logger = logging.getLogger(__name__)


def catalog_ref_from_source_url(source_url: str | None) -> str | None:
    """Return "<project>/<repository>:<tag>" for a docker:// URL, else None.

    This is the merge key. A DataVolume imported from Harbor and the artifact
    it came from must produce the same string, or the two halves of the list
    will not join and the user will see one image twice.
    """
    if not source_url or not source_url.startswith("docker://"):
        return None
    path = urlparse(source_url).path.lstrip("/")
    return path or None


def merge(cluster: list[VMImage], catalog: list[VMImage]) -> list[VMImage]:
    """Join the two halves on catalog_ref, preferring the cluster row.

    The cluster row wins because it carries real state — Ready, importing,
    progress, size, what is using it. The catalogue row contributes only its
    coordinate, which the cluster row then carries as provenance.
    """
    by_ref: dict[str, VMImage] = {}
    for img in cluster:
        ref = img.catalog_ref or catalog_ref_from_source_url(img.source_url)
        if ref:
            img.catalog_ref = ref
            by_ref[ref] = img

    rows = list(cluster)
    for entry in catalog:
        if entry.catalog_ref and entry.catalog_ref in by_ref:
            continue
        rows.append(entry)
    return rows


async def catalog_images(harbor: Any, token: str) -> list[VMImage]:
    """Every artifact the caller may see, as catalog-origin rows.

    Raises HarborUnavailable or HarborUnauthorized. The caller decides how to
    degrade; this function does not swallow the difference, because "Harbor is
    down" and "your session expired" need different user actions.
    """
    rows: list[VMImage] = []
    for project in await harbor.list_projects(token):
        pname = project.get("name")
        if not pname:
            continue
        for repo in await harbor.list_repositories(token, pname):
            # Harbor returns repository names project-qualified.
            full = repo.get("name", "")
            rname = full.split("/", 1)[1] if "/" in full else full
            if not rname:
                continue
            for artifact in await harbor.list_artifacts(token, pname, rname):
                for tag in artifact.get("tags") or []:
                    tname = tag.get("name")
                    if not tname:
                        continue
                    ref = f"{pname}/{rname}:{tname}"
                    rows.append(
                        VMImage(
                            name=f"{rname}:{tname}",
                            namespace="",
                            status="Catalog",
                            origin="catalog",
                            catalog_ref=ref,
                            size=str(artifact.get("size") or "") or None,
                        )
                    )
    return rows
```

- [ ] **Step 4: Run the unit tests**

Run: `cd backend && python -m pytest tests/test_a_catalog_image_and_its_disk_are_one_row.py tests/test_harbor_being_down_does_not_hide_local_images.py -v`
Expected: 7 passed

- [ ] **Step 5: Add the fake Harbor fixture**

Append to `backend/tests/conftest.py`, matching the shape of `mock_k8s_client`:

```python
@pytest.fixture
def mock_harbor_client() -> MagicMock:
    """A Harbor that answers with one project, one repository, one tag.

    Note this fake accepts ANY bearer token. That is fine for merge and
    degradation tests and useless for proving token forwarding — a mock will
    happily confirm an identity scheme that does not work. The e2e test in
    Task 9 covers that claim against a real Harbor.
    """
    mock = MagicMock()
    mock.list_projects = AsyncMock(return_value=[{"name": "vm-images-public"}])
    mock.list_repositories = AsyncMock(
        return_value=[{"name": "vm-images-public/ubuntu-2204"}]
    )
    mock.list_artifacts = AsyncMock(
        return_value=[{"size": 2147483648, "tags": [{"name": "20260901"}]}]
    )
    return mock
```

- [ ] **Step 6: Wire the merge into the list endpoint**

In `backend/app/api/v1/images.py`, at the end of the `GET ""` handler, before it returns: keep the existing cluster-image construction exactly as-is, then add the catalogue half.

```python
    # --- catalogue half -------------------------------------------------
    # Off by default: with the flag unset this block does nothing and the
    # response is byte-identical to before.
    catalog_available = True
    if harbor_image_path_enabled():
        harbor = request.app.state.harbor_client
        try:
            catalog = await catalog_images(harbor, user.raw_token or "")
            images = merge(images, catalog)
        except HarborUnauthorized:
            logger.info("harbor rejected the caller's token; listing cluster images only")
            catalog_available = False
        except HarborUnavailable as exc:
            logger.warning("harbor unreachable (%s); listing cluster images only", exc)
            catalog_available = False

    return GoldenImageListResponse(
        items=images, total=len(images), catalog_available=catalog_available
    )
```

Add the imports at the top of `images.py`:

```python
from app.api.v1.images_catalog import catalog_images, merge
from app.core.harbor_client import HarborUnauthorized, HarborUnavailable
from app.core.operator import harbor_image_path_enabled
```

And in `backend/app/main.py`, beside the other `app.state` clients, add:

```python
from app.core.harbor_client import HarborClient

app.state.harbor_client = HarborClient()
```

- [ ] **Step 7: Run the whole backend suite**

Run: `cd backend && python -m pytest tests/ -q`
Expected: all pass. With the flag unset the new branch never executes, so existing image tests must be untouched.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/v1/images.py backend/app/api/v1/images_catalog.py backend/app/main.py backend/tests/conftest.py backend/tests/test_a_catalog_image_and_its_disk_are_one_row.py backend/tests/test_harbor_being_down_does_not_hide_local_images.py
git commit -m "feat(images): one list, with the catalogue folded in when it answers"
```

---

### Task 5: Materialise a catalogue image into a disk

The create path builds `{"registry": {"url": ...}}` today with no credentials, so private projects cannot be pulled.

**Files:**
- Modify: `backend/app/api/v1/images.py` (the `POST ""` handler, registry branch)
- Modify: `backend/app/models/template.py` (the image-create request model)
- Test: `backend/tests/test_a_catalog_image_becomes_a_disk_with_credentials.py`

**Interfaces:**
- Consumes: Task 3's model fields
- Produces: the create request accepts `source_registry_secret: str | None` and `source_registry_ca_configmap: str | None`; the rendered DataVolume carries `spec.source.registry.secretRef` and `.certConfigMap` when they are set

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_a_catalog_image_becomes_a_disk_with_credentials.py`:

```python
"""A private Harbor project cannot be pulled anonymously.

CDI resolves secretRef in the DataVolume's OWN namespace, so the Secret has to
be in the target namespace — which is where the harbor-robots chart puts it.
"""

from app.api.v1.images import build_registry_source


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_a_catalog_image_becomes_a_disk_with_credentials.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_registry_source'`

- [ ] **Step 3: Extract and extend the registry source builder**

In `backend/app/api/v1/images.py`, add above the endpoints:

```python
def build_registry_source(
    url: str, secret_ref: str | None, cert_config_map: str | None
) -> dict[str, Any]:
    """Render spec.source.registry for a DataVolume.

    secret_ref names a Secret in the DataVolume's own namespace holding the
    tenant's Harbor robot credential; CDI will not look in any other namespace.
    Pulling is a registry operation, which is the one place robot accounts do
    work — the user's own token is for browsing and is useless here.
    """
    registry: dict[str, Any] = {"url": url}
    if secret_ref:
        registry["secretRef"] = secret_ref
    if cert_config_map:
        registry["certConfigMap"] = cert_config_map
    return {"registry": registry}
```

Replace the existing `elif image.source_registry:` branch body with a call to it:

```python
        elif image.source_registry:
            source = build_registry_source(
                image.source_registry,
                image.source_registry_secret,
                image.source_registry_ca_configmap,
            )
            source_url_display = image.source_registry
```

- [ ] **Step 4: Add the request fields**

In `backend/app/models/template.py`, on the image-create request model that carries `source_registry`, add:

```python
    # Secret in the TARGET namespace holding the Harbor robot credential.
    # CDI resolves secretRef in the DataVolume's own namespace and nowhere else.
    source_registry_secret: str | None = None
    # ConfigMap holding the registry's CA, while Harbor uses a private one.
    source_registry_ca_configmap: str | None = None
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && python -m pytest tests/test_a_catalog_image_becomes_a_disk_with_credentials.py tests/ -q`
Expected: all pass

- [ ] **Step 6: Refuse a pull whose credential is missing, before creating anything**

The spec requires a 422 naming the missing Secret. A DataVolume created with a
`secretRef` that does not resolve fails later, inside CDI, as an import error
that does not mention the Secret — so the user learns nothing useful.

Add to the `POST ""` handler, before the DataVolume is created:

```python
        if image.source_registry_secret:
            try:
                await k8s_client.core_api.read_namespaced_secret(
                    name=image.source_registry_secret, namespace=target_namespace
                )
            except ApiException as exc:
                if exc.status == 404:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Secret '{image.source_registry_secret}' not found in "
                            f"namespace '{target_namespace}'. CDI resolves secretRef "
                            "in the DataVolume's own namespace; the harbor-robots "
                            "chart provisions it there."
                        ),
                    ) from exc
                raise
```

Add the matching test to `test_a_catalog_image_becomes_a_disk_with_credentials.py`:

```python
async def test_a_missing_robot_secret_is_named_in_the_refusal(
    mock_k8s_client, monkeypatch
):
    from kubernetes_asyncio.client.rest import ApiException

    mock_k8s_client.core_api.read_namespaced_secret = AsyncMock(
        side_effect=ApiException(status=404)
    )
    # ... post an image with source_registry_secret="absent-secret" ...
    # assert response.status_code == 422
    # assert "absent-secret" in response.json()["detail"]
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/images.py backend/app/models/template.py backend/tests/test_a_catalog_image_becomes_a_disk_with_credentials.py
git commit -m "feat(images): a pull can carry the tenant's robot credential and a CA"
```

---

### Task 6: Publish a disk to the catalogue

Snapshot first so a running VM never stops.

**Files:**
- Create: `backend/app/core/image_publish.py`
- Modify: `backend/app/api/v1/images.py` (add `POST "/publish"`)
- Test: `backend/tests/test_a_publish_that_fails_cleans_up_after_itself.py`, `backend/tests/test_a_tag_that_already_exists_is_refused.py`

**Interfaces:**
- Consumes: `HarborClient` (Task 2)
- Produces:
  - `app.core.image_publish.publish_job(namespace: str, pvc: str, ref: str) -> dict[str, Any]` — a **suspended** Job
  - `app.core.image_publish.publish_dependents(namespace: str, pvc: str, job_name: str, job_uid: str) -> list[dict[str, Any]]` — snapshot and temp PVC, each owned by the Job
  - `app.core.image_publish.cleanup_names(job_name: str) -> tuple[str, str]` — `(snapshot_name, temp_pvc_name)`
  - `app.core.image_publish.assert_tag_is_free(harbor, token, project, repository, tag) -> None`

**Why the Job is created first, and suspended:** an `ownerReference` needs the
owner's UID, which does not exist until the owner is created. Creating the
snapshot and PVC first therefore cannot make the Job their owner. So: create the
Job suspended, take its UID, create the two dependents owned by it, then
unsuspend. Kubernetes garbage-collects both whatever the Job's outcome — which a
request handler cannot do, because the request is long finished by the time a
Job fails.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_a_publish_that_fails_cleans_up_after_itself.py`:

```python
"""A failed publish must not leave a snapshot and a PVC behind.

Orphans here are invisible until the storage pool fills, which is the worst
time to discover them.
"""

from app.core.image_publish import cleanup_names, publish_dependents, publish_job


def test_the_job_starts_suspended_so_it_can_own_what_it_waits_for():
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1")

    assert job["spec"]["suspend"] is True


def test_the_snapshot_comes_before_the_pvc_that_is_made_from_it():
    kinds = [
        o["kind"]
        for o in publish_dependents("tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1")
    ]

    assert kinds.index("VolumeSnapshot") < kinds.index("PersistentVolumeClaim")


def test_the_temporary_pvc_is_made_from_the_snapshot_not_the_live_disk():
    dependents = publish_dependents("tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1")
    pvc = next(o for o in dependents if o["kind"] == "PersistentVolumeClaim")

    assert pvc["spec"]["dataSource"]["kind"] == "VolumeSnapshot"


def test_both_dependents_are_owned_by_the_job_so_kubernetes_reaps_them():
    """A request handler cannot clean up after a Job that fails later."""
    dependents = publish_dependents("tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1")

    for obj in dependents:
        owner = obj["metadata"]["ownerReferences"][0]
        assert owner["kind"] == "Job"
        assert owner["uid"] == "uid-1"
        assert owner["controller"] is True


def test_every_created_object_is_named_after_the_job_that_owns_it():
    """Cleanup keys off the Job name, so the names must be derivable from it."""
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1")
    name = job["metadata"]["name"]
    snap, tmp = cleanup_names(name)

    names = {o["metadata"]["name"] for o in publish_dependents("tenant-a", "ubuntu-disk", name, "uid-1")}
    assert snap in names
    assert tmp in names


def test_the_source_disk_is_never_named_as_a_thing_to_delete():
    snap, tmp = cleanup_names("publish-ubuntu-disk-20260902")

    assert "ubuntu-disk" not in (snap, tmp)
```

Create `backend/tests/test_a_tag_that_already_exists_is_refused.py`:

```python
"""CDI imports a registry source once. Re-pushing a tag updates no disk.

A publish that overwrites a tag looks successful and ships nothing, so it is
refused at publish time rather than discovered at boot time.
"""

import pytest

from app.core.image_publish import assert_tag_is_free


class _Harbor:
    def __init__(self, tags):
        self._tags = tags

    async def list_artifacts(self, token, project, repository):
        return [{"tags": [{"name": t} for t in self._tags]}]


async def test_a_free_tag_is_allowed():
    await assert_tag_is_free(_Harbor([]), "tok", "p", "u", "20260902")


async def test_an_occupied_tag_is_refused_by_name():
    with pytest.raises(ValueError) as exc:
        await assert_tag_is_free(_Harbor(["20260902"]), "tok", "p", "u", "20260902")

    assert "20260902" in str(exc.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && python -m pytest tests/test_a_publish_that_fails_cleans_up_after_itself.py tests/test_a_tag_that_already_exists_is_refused.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.image_publish'`

- [ ] **Step 3: Write the publish planner**

Create `backend/app/core/image_publish.py`:

```python
"""Planning a snapshot-then-publish.

The VM keeps running: we snapshot its disk, make a temporary PVC from the
snapshot, and let a Job read that. Cross-namespace and same-namespace clones
are both thin on snapshot-capable storage, so the copy costs little.

The plan is data, not actions, so the ordering and the naming can be tested
without a cluster.
"""

from typing import Any

PUBLISH_IMAGE = "gcr.io/go-containerregistry/crane:debug"


def cleanup_names(job_name: str) -> tuple[str, str]:
    """Names of the two objects a publish leaves behind if it dies.

    Derived from the Job name so cleanup never has to guess, and never has to
    be told the source disk's name — deleting that would destroy the very disk
    the user asked to publish.
    """
    return f"{job_name}-snap", f"{job_name}-tmp"


def publish_job(namespace: str, pvc: str, ref: str) -> dict[str, Any]:
    """The publish Job, created SUSPENDED so it can own its dependents.

    An ownerReference needs the owner's UID, which exists only once the owner
    is created. Creating the snapshot and PVC first would leave them ownerless
    and therefore un-reaped when the Job fails.
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"publish-{pvc}", "namespace": namespace},
        "spec": {
            "suspend": True,
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "publish",
                            "image": PUBLISH_IMAGE,
                            "env": [{"name": "REF", "value": ref}],
                            "volumeMounts": [
                                {"name": "disk", "mountPath": "/disk", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "disk",
                            "persistentVolumeClaim": {
                                "claimName": cleanup_names(f"publish-{pvc}")[1],
                                "readOnly": True,
                            },
                        }
                    ],
                }
            },
        },
    }


def publish_dependents(
    namespace: str, pvc: str, job_name: str, job_uid: str
) -> list[dict[str, Any]]:
    """The snapshot and the temporary PVC, both owned by the Job.

    Ownership is what makes cleanup unconditional: Kubernetes reaps these when
    the Job goes, whether it succeeded, failed, or was deleted by hand.
    """
    snap_name, tmp_name = cleanup_names(job_name)
    owner = [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_name,
            "uid": job_uid,
            "controller": True,
            "blockOwnerDeletion": False,
        }
    ]
    return [
        {
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {
                "name": snap_name,
                "namespace": namespace,
                "ownerReferences": owner,
            },
            "spec": {"source": {"persistentVolumeClaimName": pvc}},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": tmp_name,
                "namespace": namespace,
                "ownerReferences": owner,
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "dataSource": {
                    "name": snap_name,
                    "kind": "VolumeSnapshot",
                    "apiGroup": "snapshot.storage.k8s.io",
                },
                "resources": {"requests": {"storage": "0"}},
            },
        },
    ]


async def assert_tag_is_free(
    harbor: Any, token: str, project: str, repository: str, tag: str
) -> None:
    """Raise ValueError if the tag already exists in the catalogue.

    CDI imports a registry source exactly once, so overwriting a tag produces a
    publish that reports success and changes nothing anybody can boot.
    """
    for artifact in await harbor.list_artifacts(token, project, repository):
        for existing in artifact.get("tags") or []:
            if existing.get("name") == tag:
                raise ValueError(
                    f"tag {tag} already exists in {project}/{repository}; "
                    "publish a new tag rather than replacing one"
                )
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && python -m pytest tests/test_a_publish_that_fails_cleans_up_after_itself.py tests/test_a_tag_that_already_exists_is_refused.py -v`
Expected: 6 passed

- [ ] **Step 5: Add the endpoint**

In `backend/app/api/v1/images.py`:

```python
@images_router.post("/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish_image(
    request: Request,
    req: ImagePublishRequest,
    user: User = Depends(require_auth),
) -> dict[str, str]:
    """Publish a disk to the catalogue without stopping the VM using it."""
    if not harbor_image_path_enabled():
        raise HTTPException(status_code=501, detail="Harbor image path is disabled")

    validate_k8s_name(req.namespace, "namespace")
    validate_k8s_name(req.disk_name, "disk_name")
    validate_k8s_name(req.tag, "tag")

    harbor = request.app.state.harbor_client
    try:
        await assert_tag_is_free(
            harbor, user.raw_token or "", req.project, req.repository, req.tag
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    k8s_client = request.app.state.k8s_client
    ref = f"{req.project}/{req.repository}:{req.tag}"

    # Suspended first, so its UID can own the snapshot and the temporary PVC.
    job = await create_object(k8s_client, publish_job(req.namespace, req.disk_name, ref))
    job_name = job["metadata"]["name"]
    try:
        for obj in publish_dependents(
            req.namespace, req.disk_name, job_name, job["metadata"]["uid"]
        ):
            await create_object(k8s_client, obj)
        await unsuspend_job(k8s_client, req.namespace, job_name)
    except ApiException as exc:
        # Deleting the Job takes its owned dependents with it.
        await delete_job(k8s_client, req.namespace, job_name)
        raise HTTPException(
            status_code=422, detail=f"publish could not start: {exc.reason}"
        ) from exc

    return {"job": job_name, "ref": ref}
```

Add `ImagePublishRequest` to `app/models/template.py` with `namespace`, `disk_name`, `project`, `repository`, `tag` — all `str`.

`create_object`, `unsuspend_job` and `delete_job` are thin helpers over
`client.BatchV1Api` / `CustomObjectsApi` / `core_api`; write them beside the
handler. `create_object` must return the created object, because the Job's UID
is read from it.

The `except` covers only failures while *creating* the objects, and it recovers
by deleting the Job — which takes the snapshot and temporary PVC with it,
because they are owned by it.

Cleanup after the *Job itself* fails is the Job's `ttlSecondsAfterFinished`
plus those same `ownerReferences`: Kubernetes garbage-collects both dependents
whatever the outcome. That is the part a `finally` in the request handler
cannot do, because the request is long gone by the time the Job fails.

- [ ] **Step 6: Run the whole suite**

Run: `cd backend && python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/image_publish.py backend/app/api/v1/images.py backend/tests/test_a_publish_that_fails_cleans_up_after_itself.py backend/tests/test_a_tag_that_already_exists_is_refused.py
git commit -m "feat(images): publish a disk without stopping the machine using it"
```

---

### Task 7: The unified list in the UI

**Files:**
- Modify: `frontend/src/api/images.ts` (create if absent; otherwise extend)
- Modify: `frontend/src/hooks/useTemplates.ts` (the `useImages` hook)
- Modify: `frontend/src/pages/Storage.tsx` (the Images section)
- Test: `frontend/src/__tests__/images-list.test.tsx`

**Interfaces:**
- Consumes: `GET /images` returning `{items, total, catalog_available}` with `origin` and `catalog_ref` per item (Tasks 3, 4)
- Produces: `useImages(namespace)` returns `{ items, catalogAvailable }`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/__tests__/images-list.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ImageRows } from '../components/images/ImageRows';

describe('the image list', () => {
  it('marks a catalogue entry as not yet materialised', () => {
    render(
      <ImageRows
        items={[{ name: 'rocky-9:1', origin: 'catalog', status: 'Catalog' }]}
        catalogAvailable
      />
    );
    expect(screen.getByText(/rocky-9:1/)).toBeInTheDocument();
    expect(screen.getByTestId('origin-catalog')).toBeInTheDocument();
  });

  it('warns when the catalogue could not be read, without hiding local disks', () => {
    render(
      <ImageRows
        items={[{ name: 'ubuntu', origin: 'cluster', status: 'Ready' }]}
        catalogAvailable={false}
      />
    );
    expect(screen.getByText(/ubuntu/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/catalog/i);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm test -- images-list`
Expected: FAIL — cannot resolve `../components/images/ImageRows`

- [ ] **Step 3: Implement the row component and hook**

Create `frontend/src/components/images/ImageRows.tsx` rendering one row per item, with `data-testid="origin-catalog"` on catalogue rows and a `role="status"` banner when `catalogAvailable` is false. The banner must not replace the list — the local rows stay visible beneath it.

Update `useImages` in `frontend/src/hooks/useTemplates.ts` to return `catalogAvailable` from the response, keeping the existing `queryKey` shape and the `useGoldenImages` alias exported at line 145.

- [ ] **Step 4: Run the test**

Run: `cd frontend && npm test -- images-list`
Expected: 2 passed

- [ ] **Step 5: Render it in Storage.tsx**

Replace the Images section's row rendering with `<ImageRows>`. Add a "Create disk" action on catalogue rows that calls the create mutation with `source_registry` set from `catalog_ref`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/images/ImageRows.tsx frontend/src/hooks/useTemplates.ts frontend/src/pages/Storage.tsx frontend/src/api/images.ts frontend/src/__tests__/images-list.test.tsx
git commit -m "feat(images): the list shows the catalogue and what is already a disk"
```

---

### Task 8: Catalogue images are selectable in a template

**Files:**
- Modify: `frontend/src/pages/VMTemplates.tsx:615` (the image `<CustomSelect>` options)
- Test: `frontend/src/__tests__/template-image-choice.test.tsx`

**Interfaces:**
- Consumes: `useImages` from Task 7

- [ ] **Step 1: Write the failing test**

Create `frontend/src/__tests__/template-image-choice.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest';
import { imageOptions } from '../pages/imageOptions';

describe('the template image chooser', () => {
  it('offers images that exist and images the catalogue can supply', () => {
    const opts = imageOptions([
      { name: 'ubuntu', origin: 'cluster', display_name: 'Ubuntu', size: '20Gi' },
      { name: 'rocky-9:1', origin: 'catalog', catalog_ref: 'p/rocky-9:1' },
    ]);
    expect(opts.map((o) => o.value)).toContain('ubuntu');
    expect(opts.map((o) => o.value)).toContain('p/rocky-9:1');
  });

  it('says which options still need importing, so the wait is not a surprise', () => {
    const opts = imageOptions([
      { name: 'rocky-9:1', origin: 'catalog', catalog_ref: 'p/rocky-9:1' },
    ]);
    expect(opts[0].label).toMatch(/import/i);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm test -- template-image-choice`
Expected: FAIL — cannot resolve `../pages/imageOptions`

- [ ] **Step 3: Extract the option builder**

Create `frontend/src/pages/imageOptions.ts` exporting `imageOptions(images)`, which returns `{value, label}` for cluster rows (value `name`) and catalogue rows (value `catalog_ref`, label noting the image will be imported first).

- [ ] **Step 4: Run the test**

Run: `cd frontend && npm test -- template-image-choice`
Expected: 2 passed

- [ ] **Step 5: Use it in the page**

At `VMTemplates.tsx:615`, replace the inline `projectImages.map(...)` with `imageOptions(images)`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/imageOptions.ts frontend/src/pages/VMTemplates.tsx frontend/src/__tests__/template-image-choice.test.tsx
git commit -m "feat(templates): a template can start from an image still in the catalogue"
```

---

### Task 9: Prove token forwarding against a real Harbor

Every test so far uses a fake that accepts any bearer. A fake cannot fail this claim, and this claim is the whole security model.

**Files:**
- Modify: `docker-compose.e2e.yml` (add a Harbor service)
- Create: `e2e/harbor-identity.spec.ts`

**Interfaces:**
- Consumes: everything above

- [ ] **Step 1: Add Harbor to the e2e stack**

Add a `harbor` service to `docker-compose.e2e.yml` pinned to `goharbor/harbor-core:v2.15.2`, with a `HARBOR_URL` pointing the backend at it.

- [ ] **Step 2: Write the test that asserts a refusal**

Create `e2e/harbor-identity.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

// The unit tests use a fake Harbor that accepts any bearer token, so they
// would pass even if kubevirt-ui forwarded nothing at all. This asserts the
// negative that only a real Harbor can: a wrong identity is REFUSED.
test('a request carrying the wrong identity is refused, not served', async ({ request }) => {
  const res = await request.get('/api/v1/images', {
    headers: { Authorization: 'Bearer not-a-real-token' },
  });

  // Cluster rows may still be returned; the catalogue half must not be.
  const body = await res.json();
  expect(body.catalog_available).toBe(false);
  expect(body.items.filter((i: any) => i.origin === 'catalog')).toHaveLength(0);
});

test('a valid identity sees the catalogue', async ({ request }) => {
  const res = await request.get('/api/v1/images');
  const body = await res.json();

  expect(body.catalog_available).toBe(true);
});
```

- [ ] **Step 3: Run it**

Run: `docker compose -f docker-compose.e2e.yml up -d && npx playwright test harbor-identity`
Expected: 2 passed

- [ ] **Step 4: Commit**

```bash
git add docker-compose.e2e.yml e2e/harbor-identity.spec.ts
git commit -m "test(harbor): a wrong identity is refused, which only a real registry can prove"
```

---

## Done when

- `cd backend && python -m pytest tests/ -q` passes
- `cd frontend && npm test` passes
- `npx playwright test harbor-identity` passes against the e2e stack
- With `HARBOR_IMAGE_ENABLED` unset, `GET /images` returns exactly what it returns today
- Nothing has been deployed to the lab
