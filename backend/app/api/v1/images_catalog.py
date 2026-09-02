"""Merging the Harbor catalogue into the cluster's image list.

Kept out of the endpoint module so the merge rule can be tested without
FastAPI, a Kubernetes client, or a Harbor.
"""

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlparse

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


async def catalog_images(harbor: Any, token: str) -> list[VMImage]:
    """Every artifact the caller may see, as catalog-origin rows.

    Raises HarborUnavailable or HarborUnauthorized. The caller decides how to
    degrade; this function does not swallow the difference, because "Harbor is
    down" and "your session expired" need different user actions.

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

    projects = [
        name for name in (p.get("name") for p in await harbor.list_projects(token)) if name
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

    artifact_lists = await asyncio.gather(*(artifacts_of(p) for p in projects))

    rows: list[VMImage] = []
    for pname, artifacts in zip(projects, artifact_lists, strict=True):
        for artifact in artifacts:
            rname = _repository_name(artifact.get("repository_name") or "", pname)
            if not rname:
                continue
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
