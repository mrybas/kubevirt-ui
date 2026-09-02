"""A disk imported from Harbor is the same image as its catalogue entry.

Showing both would tell the user they have two Ubuntu images when they have
one, and leave them guessing which to boot.
"""

from app.api.v1.images_catalog import catalog_images, catalog_ref_from_source_url, merge
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


class _MultiSegmentHarbor:
    """A repository whose name is itself multi-segment: "team/subimage"."""

    async def verify_identity(self, token):
        return None

    async def list_projects(self, token):
        return [{"name": "vm-images-public"}]

    async def list_repositories(self, token, project):
        return [{"name": "vm-images-public/team/subimage"}]

    async def list_artifacts(self, token, project, repository):
        return [{"size": 2147483648, "tags": [{"name": "20260901"}]}]


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
    rows = await catalog_images(_VerifiedButEmpty(), "tok")
    assert rows == []


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
    catalog = await catalog_images(_MultiSegmentHarbor(), "tok")

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
