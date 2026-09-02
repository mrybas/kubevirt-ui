"""Merging the Harbor catalogue into the cluster's image list.

Kept out of the endpoint module so the merge rule can be tested without
FastAPI, a Kubernetes client, or a Harbor.
"""

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlparse

from app.core.harbor_client import harbor_registry_host
from app.models.template import VMImage

logger = logging.getLogger(__name__)


def _fetch_concurrency() -> int:
    """How many Harbor list requests may be in flight at once.

    Bounded rather than unlimited: a Harbor with hundreds of repositories
    would otherwise open hundreds of sockets at once and be rate-limited or
    simply refuse, turning a slow page into a broken one. Read per call so a
    deployment can lower it without a rebuild.
    """
    try:
        return max(1, int(os.getenv("HARBOR_FETCH_CONCURRENCY", "8")))
    except ValueError:
        return 8


def catalog_ref_from_source_url(
    source_url: str | None, *, registry_host: str | None = None
) -> str | None:
    """Return "<project>/<repository>:<tag>" for a Harbor docker:// URL, else None.

    This is the merge key. A DataVolume imported from Harbor and the artifact
    it came from must produce the same string, or the two halves of the list
    will not join and the user will see one image twice.

    THE HOST IS PART OF THE IDENTITY, even though it is not part of the key.
    Reading the path and discarding the authority made
    `docker://quay.io/vm-images/ubuntu:22.04` and the Harbor artifact
    `vm-images/ubuntu:22.04` the same key — so an unrelated disk pulled from a
    public registry swallowed the catalogue row of a Harbor image that merely
    shares a path, and the Harbor image silently disappeared from the list.
    A URL whose host is not the configured Harbor is not a catalogue
    coordinate at all, so it gets None.

    `registry_host` defaults to `harbor_registry_host()`. When that is empty
    the deployment has no Harbor configured — there is no catalogue to join
    against and no collision to cause — so the host check is skipped rather
    than turning every ref into None.
    """
    if not source_url or not source_url.startswith("docker://"):
        return None
    parsed = urlparse(source_url)
    host = harbor_registry_host() if registry_host is None else registry_host
    if host and parsed.netloc != host:
        return None
    path = parsed.path.lstrip("/")
    return path or None


_SIZE_UNITS = ("Ki", "Mi", "Gi", "Ti", "Pi")


def format_artifact_size(size: Any) -> str | None:
    """A Harbor artifact's byte count as the same kind of string a disk shows.

    Harbor reports `size` in bytes; every other row in this list carries a
    Kubernetes quantity ("20Gi"). Rendered side by side, `1181116006` next to
    `20Gi` reads as either a much bigger disk or a broken field — the one
    thing it does not read as is a size. Returns None for anything that is
    not a usable positive integer, which the row then renders as "-".
    """
    try:
        raw = int(size)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None

    value = float(raw)
    unit = ""
    for candidate in _SIZE_UNITS:
        if value < 1024:
            break
        value /= 1024
        unit = candidate
    if not unit:
        return f"{raw}"
    rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}{unit}"


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


def _repository_name(full: str, project: str) -> str:
    """The repository name without its project prefix, if it carries one.

    Harbor reports repository names project-qualified in some responses
    ("vm-images-public/ubuntu-2204") and bare in others, and a repository name
    may itself be multi-segment ("team/subimage"). Stripping a known project
    prefix — rather than splitting on the first slash — handles both without
    ever eating a segment of a multi-part name. Get this wrong and the ref
    built from it stops matching what `catalog_ref_from_source_url` parses out
    of a disk's `source_url`, and the two halves of the list never join.
    """
    prefix = f"{project}/"
    return full[len(prefix):] if full.startswith(prefix) else full


async def catalog_images(harbor: Any, token: str) -> tuple[list[VMImage], bool]:
    """Every artifact the caller may see, as catalog-origin rows.

    Returns `(rows, complete)`. `complete` is False when at least one project
    could not be read but others could: the rows returned are real, and the
    ones missing are unknowable, which is exactly what the list endpoint's
    `catalog_available: false` banner already says. Returning only the rows
    would claim a catalogue that is short some images is the whole catalogue —
    the "convincing empty list" failure in a smaller size.

    Raises HarborUnavailable or HarborUnauthorized when NOTHING could be read.
    The caller decides how to degrade; this function does not swallow the
    difference, because "Harbor is down" and "your session expired" need
    different user actions.

    Identity is checked with `verify_identity()` before anything is
    enumerated. `list_projects()` alone cannot be trusted for this: it
    returns 200 for any bearer, including garbage or none, filtered to
    whatever that identity can see — an anonymous caller and a legitimately
    empty catalogue both come back as zero projects. Without the probe, a
    rejected identity would silently look like an authenticated user with
    nothing to show, which is the opposite of what "catalog_available" is
    supposed to mean. This must stay the first call the function makes; a
    fake that raises if enumeration runs before it pins that order in tests.
    """
    await harbor.verify_identity(token)

    # `isinstance` on both sides for the same reason the artifact loop has it:
    # these are wire rows, and a row that is not the shape this expects must
    # not become an AttributeError that no caller of this function catches.
    projects = [
        p["name"]
        for p in await harbor.list_projects(token)
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]
    ]

    # One request per project, not one per repository. Harbor's project-wide
    # artifact listing carries `repository_name` on every artifact, which is
    # the only thing the repositories-then-artifacts walk was fetching, so
    # `1 + P + (P x R)` requests collapse to `1 + P`. Following every page
    # (which the client now does, because a truncated list makes an occupied
    # tag look free) multiplies whatever this costs, so the shape of the walk
    # is what decides whether that is affordable.
    #
    # Concurrent because it is free to be: one gather over the project list,
    # bounded by a semaphore so a Harbor with hundreds of projects does not
    # open hundreds of sockets at once. gather preserves argument order, so
    # rows come out in the same deterministic order a serial loop produced.
    sem = asyncio.Semaphore(_fetch_concurrency())

    async def artifacts_of(project_name: str) -> list[dict[str, Any]]:
        async with sem:
            return await harbor.list_project_artifacts(token, project_name)

    # return_exceptions, because one project must not take the rest with it.
    # A single repository the caller cannot read, a project mid-deletion, one
    # 500 out of forty — without this, gather re-raises the first of them and
    # the ENTIRE catalogue disappears behind a banner, including every project
    # that answered perfectly well. The failures are counted instead, and
    # reported through `complete`.
    artifact_lists = await asyncio.gather(
        *(artifacts_of(p) for p in projects), return_exceptions=True
    )

    failed = 0
    rows: list[VMImage] = []
    # `strict=True` would be correct here — gather preserves argument order and
    # length — but "correct" is not the same as "safe to raise from": a
    # ValueError out of this loop escapes every `except Harbor*` the caller
    # has and 500s a page whose cluster half was fine. zip's default stops at
    # the shorter sequence, which cannot happen, and does not raise if it
    # somehow does.
    for pname, artifacts in zip(projects, artifact_lists):  # noqa: B905
        if isinstance(artifacts, BaseException):
            failed += 1
            logger.warning(
                "harbor project %r could not be listed (%r); its images are "
                "missing from the catalogue", pname, artifacts,
            )
            continue
        for artifact in artifacts:
            # Rows come off the wire. Anything that is not the shape this
            # expects is skipped rather than allowed to raise an AttributeError
            # that no caller catches.
            if not isinstance(artifact, dict):
                continue
            raw_repo = artifact.get("repository_name")
            if not isinstance(raw_repo, str):
                continue
            rname = _repository_name(raw_repo, pname)
            if not rname:
                continue
            tags = artifact.get("tags")
            if not isinstance(tags, list):
                # None for an untagged artifact is the ordinary case; anything
                # else is a shape this does not know, and iterating it is how
                # a TypeError gets out.
                continue
            for tag in tags:
                tname = tag.get("name") if isinstance(tag, dict) else None
                if not isinstance(tname, str) or not tname:
                    continue
                ref = f"{pname}/{rname}:{tname}"
                rows.append(
                    VMImage(
                        name=f"{rname}:{tname}",
                        namespace="",
                        status="Catalog",
                        origin="catalog",
                        catalog_ref=ref,
                        size=format_artifact_size(artifact.get("size")),
                    )
                )

    if failed and failed == len(projects):
        # Nothing at all could be read. That is not a partial answer, it is
        # the failure the caller's own exception handling exists for — so it
        # is raised rather than reported as an empty-but-complete catalogue.
        first = next(a for a in artifact_lists if isinstance(a, BaseException))
        raise first

    return rows, failed == 0
