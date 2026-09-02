"""A disk imported from Harbor is the same image as its catalogue entry.

Showing both would tell the user they have two Ubuntu images when they have
one, and leave them guessing which to boot.
"""

import pytest

from app.api.v1.images_catalog import (
    catalog_images,
    catalog_ref_from_source_url,
    format_artifact_size,
    merge,
)
from app.core.harbor_client import HarborUnavailable
from app.models.template import VMImage


@pytest.fixture(autouse=True)
def _no_harbor_unless_a_test_says_so(monkeypatch):
    """HARBOR_URL is read at call time by the merge key, so pin it here.

    Without this, whether `catalog_ref_from_source_url` applies its host check
    depends on the environment the suite happens to run in, and these tests
    would pass or fail for reasons that have nothing to do with them. The
    tests that are ABOUT the host set it explicitly.
    """
    monkeypatch.delenv("HARBOR_URL", raising=False)


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


class _MultiSegmentHarbor:
    """A repository whose name is itself multi-segment: "team/subimage"."""

    def __init__(self, qualified: bool = True):
        # Harbor reports repository names project-qualified in some responses
        # and bare in others; both shapes must produce the same ref.
        self.qualified = qualified
        self.per_repository_calls = 0

    async def verify_identity(self, token):
        return None

    async def list_projects(self, token):
        return [{"name": "vm-images-public"}]

    async def list_project_artifacts(self, token, project):
        name = (
            "vm-images-public/team/subimage" if self.qualified else "team/subimage"
        )
        return [
            {
                "repository_name": name,
                "size": 2147483648,
                "tags": [{"name": "20260901"}],
            }
        ]

    async def list_repositories(self, token, project):
        self.per_repository_calls += 1
        raise AssertionError(
            "enumeration must not walk repositories — the project-wide "
            "artifact listing already carries repository_name"
        )

    async def list_artifacts(self, token, project, repository):
        self.per_repository_calls += 1
        raise AssertionError(
            "enumeration must not read artifacts per repository"
        )


class _VerifiedButEmpty:
    """A real identity whose catalogue genuinely has nothing in it.

    This is the case a wrong identity must not be confused with:
    verify_identity succeeds (this is a real user), list_projects legitimately
    returns nothing. catalog_images must return an empty list here, not raise
    — an empty catalogue is not a failure.
    """

    async def verify_identity(self, token):
        return None

    async def list_projects(self, token):
        return []


async def test_a_verified_identity_with_nothing_to_see_returns_an_empty_list_not_an_error():
    """Same empty result as a rejected identity at the list level — the two
    are told apart by whether catalog_images raises, not by what it returns.
    The endpoint is what turns "did not raise" into catalog_available: true
    and "raised HarborUnauthorized" into catalog_available: false; see
    test_the_image_endpoint_wires_the_catalogue_and_the_callers_token.py.
    """
    rows, complete = await catalog_images(_VerifiedButEmpty(), "tok")
    assert rows == []
    # Nothing failed, so the empty catalogue is a COMPLETE empty catalogue —
    # which is what lets the endpoint report catalog_available: true for it.
    assert complete is True


class _OrderSensitiveHarbor:
    """Raises if project enumeration is ever reached before identity is verified.

    Pins the ordering itself, independent of any particular exception type —
    a refactor that reordered the two calls would trip this even if it kept
    verify_identity's exception behaviour otherwise intact.
    """

    def __init__(self):
        self.verified = False

    async def verify_identity(self, token):
        self.verified = True

    async def list_projects(self, token):
        if not self.verified:
            raise AssertionError("list_projects ran before verify_identity")
        return []


async def test_identity_is_verified_before_any_project_is_listed():
    harbor = _OrderSensitiveHarbor()
    await catalog_images(harbor, "tok")
    assert harbor.verified is True


async def test_a_multi_segment_repository_name_still_joins_with_its_disk():
    """catalog_images strips only the project off the front of the repo name.

    Harbor repository names are frequently multi-segment
    ("vm-images-public/team/subimage"). If the strip took more than the
    project (e.g. split on the last "/" instead of the first), the resulting
    catalog_ref would not match what catalog_ref_from_source_url parses back
    out of the disk's docker:// source_url, the merge key would not join, and
    the user would see the same image twice — once as a cluster row and once
    as an unmaterialised catalogue row.
    """
    catalog, _ = await catalog_images(_MultiSegmentHarbor(), "tok")

    assert len(catalog) == 1
    ref = catalog[0].catalog_ref
    assert ref == "vm-images-public/team/subimage:20260901"

    # The ref must round-trip through the merge key exactly.
    disk = _cluster("subimage", f"docker://harbor.example/{ref}")
    assert catalog_ref_from_source_url(disk.source_url) == ref

    rows = merge([disk], catalog)

    assert len(rows) == 1
    assert rows[0].origin == "cluster"
    assert rows[0].catalog_ref == ref


async def test_a_bare_repository_name_produces_the_same_ref_as_a_qualified_one():
    """Whether Harbor qualifies the name with its project or not, the ref that
    comes out has to be identical — it is the merge key, and two spellings of
    it means the disk and its catalogue row never join."""
    catalog, _ = await catalog_images(_MultiSegmentHarbor(qualified=False), "tok")

    assert [row.catalog_ref for row in catalog] == [
        "vm-images-public/team/subimage:20260901"
    ]


async def test_enumeration_never_falls_back_to_the_per_repository_walk():
    """The N+1 this replaced: 1 + P + (P x R) requests per catalogue read.

    `list_repositories`/`list_artifacts` still exist and are still used by the
    publish path's tag check, so nothing stops a refactor from quietly
    reintroducing the walk here — the fake raises if either is touched.
    """
    harbor = _MultiSegmentHarbor()

    await catalog_images(harbor, "tok")

    assert harbor.per_repository_calls == 0


# ---------------------------------------------------------------------------
# The registry host is part of the image's identity
# ---------------------------------------------------------------------------


def test_a_disk_from_another_registry_is_not_a_catalogue_coordinate(
    monkeypatch,
) -> None:
    """`quay.io/vm-images/ubuntu:22.04` is not the Harbor image of that path.

    The key was built by reading `urlparse(url).path` and throwing the
    authority away, so a disk pulled from any public registry collided with
    the Harbor artifact that happens to share a project/repository name. The
    consequence is not a duplicate: `merge()` prefers the cluster row and
    DROPS the catalogue row, so the Harbor image silently vanished from the
    list and could never be imported.
    """
    monkeypatch.setenv("HARBOR_URL", "https://harbor.example")

    assert (
        catalog_ref_from_source_url("docker://quay.io/vm-images/ubuntu:22.04")
        is None
    )
    assert (
        catalog_ref_from_source_url("docker://harbor.example/vm-images/ubuntu:22.04")
        == "vm-images/ubuntu:22.04"
    )


def test_the_harbor_row_survives_a_same_path_disk_from_elsewhere(monkeypatch) -> None:
    monkeypatch.setenv("HARBOR_URL", "https://harbor.example")
    ref = "vm-images/ubuntu:22.04"
    elsewhere = _cluster("ubuntu-from-quay", f"docker://quay.io/{ref}")

    rows = merge([elsewhere], [_catalog(ref)])

    assert len(rows) == 2
    assert sorted(r.origin for r in rows) == ["catalog", "cluster"]
    assert elsewhere.catalog_ref is None


def test_with_no_harbor_configured_the_host_check_is_skipped(monkeypatch) -> None:
    """There is no catalogue to collide with, so nothing is gained by
    refusing every ref — and the pre-existing behaviour is preserved."""
    monkeypatch.delenv("HARBOR_URL", raising=False)

    assert (
        catalog_ref_from_source_url("docker://anything.example/p/u:1") == "p/u:1"
    )


# ---------------------------------------------------------------------------
# A size the user can read
# ---------------------------------------------------------------------------


def test_a_catalogue_row_reports_its_size_the_way_a_disk_does() -> None:
    """Harbor reports bytes; every other row carries a Kubernetes quantity.

    Rendered in the same column, `2147483648` beside `20Gi` is not a size the
    reader can compare with anything.
    """
    assert format_artifact_size(2147483648) == "2Gi"
    assert format_artifact_size(1181116006) == "1.1Gi"
    assert format_artifact_size(524288) == "512Ki"
    assert format_artifact_size(900) == "900"
    # Nothing usable renders as "-" in the row rather than as a wrong number.
    assert format_artifact_size(None) is None
    assert format_artifact_size(0) is None
    assert format_artifact_size("not-a-number") is None


async def test_the_catalogue_row_carries_the_formatted_size() -> None:
    catalog, _ = await catalog_images(_MultiSegmentHarbor(), "tok")

    assert catalog[0].size == "2Gi"


# ---------------------------------------------------------------------------
# One unreadable project must not take the catalogue with it
# ---------------------------------------------------------------------------


class _PartlyReadableHarbor:
    """Three projects; the middle one cannot be listed.

    A repository the caller cannot read, a project mid-deletion, one 500 out
    of forty — `asyncio.gather` without `return_exceptions` re-raises the
    first of them, and the ENTIRE catalogue disappears behind the outage
    banner, including every project that answered perfectly well.
    """

    def __init__(self, failure=None):
        self.failure = failure or HarborUnavailable("project is being deleted")

    async def verify_identity(self, token):
        return None

    async def list_projects(self, token):
        return [{"name": "good-a"}, {"name": "broken"}, {"name": "good-b"}]

    async def list_project_artifacts(self, token, project):
        if project == "broken":
            raise self.failure
        return [
            {
                "repository_name": f"{project}/img",
                "size": 1073741824,
                "tags": [{"name": "1"}],
            }
        ]


async def test_one_unreadable_project_does_not_empty_the_catalogue() -> None:
    rows, complete = await catalog_images(_PartlyReadableHarbor(), "tok")

    assert [r.catalog_ref for r in rows] == ["good-a/img:1", "good-b/img:1"]
    # Reported as incomplete, because it is: the rows returned are real and
    # the ones missing are unknowable, which is exactly what the endpoint's
    # `catalog_available: false` banner already says.
    assert complete is False


async def test_a_project_that_fails_with_anything_at_all_is_skipped() -> None:
    """Not only the two designed exceptions — an AttributeError off an
    unexpected row shape must not empty the catalogue either."""
    rows, complete = await catalog_images(
        _PartlyReadableHarbor(failure=AttributeError("'str' has no 'get'")), "tok"
    )

    assert [r.catalog_ref for r in rows] == ["good-a/img:1", "good-b/img:1"]
    assert complete is False


class _EveryProjectBroken(_PartlyReadableHarbor):
    async def list_projects(self, token):
        return [{"name": "broken"}]


async def test_when_nothing_can_be_read_the_failure_is_still_raised() -> None:
    """A total outage is not a partial answer. Reporting it as an
    empty-but-complete catalogue is the "convincing empty list" failure."""
    with pytest.raises(HarborUnavailable):
        await catalog_images(_EveryProjectBroken(), "tok")


# ---------------------------------------------------------------------------
# Rows come off the wire, so their shape is not a given
# ---------------------------------------------------------------------------


class _HarborReturningJunk:
    """Every field the enumerator reads, in a shape it does not expect.

    `zip(..., strict=True)` and `artifact.get(...)` both raise for these, and
    neither ValueError nor AttributeError is caught by any caller of
    `catalog_images` — so a single malformed row 500'd the whole images page,
    cluster rows included.
    """

    async def verify_identity(self, token):
        return None

    async def list_projects(self, token):
        return ["not-a-dict", {"no_name": 1}, {"name": None}, {"name": "real"}]

    async def list_project_artifacts(self, token, project):
        return [
            "not-a-dict",
            {"repository_name": 17, "tags": [{"name": "1"}]},
            {"repository_name": "real/img", "tags": "not-a-list"},
            {"repository_name": "real/img", "tags": ["not-a-dict", {"name": None}]},
            {"repository_name": "real/img", "size": 1073741824, "tags": [{"name": "ok"}]},
        ]


async def test_a_malformed_row_is_skipped_rather_than_raised() -> None:
    rows, complete = await catalog_images(_HarborReturningJunk(), "tok")

    assert [r.catalog_ref for r in rows] == ["real/img:ok"]
    assert complete is True
