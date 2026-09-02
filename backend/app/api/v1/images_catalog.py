"""Merging the Harbor catalogue into the cluster's image list.

Kept out of the endpoint module so the merge rule can be tested without
FastAPI, a Kubernetes client, or a Harbor.
"""

import logging
from typing import Any
from urllib.parse import urlparse

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

    rows: list[VMImage] = []
    for project in await harbor.list_projects(token):
        pname = project.get("name")
        if not pname:
            continue
        for repo in await harbor.list_repositories(token, pname):
            # Harbor returns repository names project-qualified, and a
            # repository name may itself be multi-segment (e.g.
            # "vm-images-public/team/subimage"). maxsplit=1 takes only the
            # project off the front, leaving "team/subimage" intact — get
            # this wrong and the ref built below no longer matches what
            # catalog_ref_from_source_url parses back out of a disk's
            # source_url, and the two rows never join.
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
