import re

from kubernetes_asyncio.client.exceptions import ApiException
from fastapi import HTTPException
import logging

logger = logging.getLogger(__name__)


_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def validate_k8s_name(value: str, field: str = "name") -> str:
    """Validate value matches Kubernetes resource name rules.

    Raises HTTPException(422) if invalid. This prevents shell injection
    when names are interpolated into commands or YAML.
    # Attack vector blocked: value='foo; rm -rf /' or 'foo\nmalicious: yaml'
    """
    if not value or len(value) > 253 or not _K8S_NAME_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field}: must match regex ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ (got {value!r})",
        )
    return value


# --- OCI / Harbor coordinates ------------------------------------------------
#
# A Kubernetes object name and an image coordinate are different alphabets, and
# validating one with the other's rule is wrong in both directions.
# `validate_k8s_name` forbids dots, underscores and uppercase, so it rejects
# `v1.0.0`, `24.04` and `ubuntu_22` — every one of them an ordinary tag — with a
# 422 that blames the caller for a legal value. It is also *not* the right
# safety rule here: what these strings need is to be safe to interpolate into a
# registry reference and into a Harbor API path, which means no `/` where one is
# not allowed, no `..`, no `:`, no whitespace, no scheme.
#
# Shapes below follow the OCI distribution spec:
#   tag             [A-Za-z0-9_][A-Za-z0-9._-]{0,127}
#   path component  lowercase alphanumerics separated by one `.`, one `_`,
#                   a `__`, or a run of `-`
#   repository      one or more path components joined by `/`
# Harbor project names are a single path component.
_OCI_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_OCI_PATH_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
_OCI_REPOSITORY_RE = re.compile(
    rf"^{_OCI_PATH_COMPONENT}(?:/{_OCI_PATH_COMPONENT})*$"
)
_HARBOR_PROJECT_RE = re.compile(rf"^{_OCI_PATH_COMPONENT}$")


def validate_oci_tag(value: str, field: str = "tag") -> str:
    """Validate an OCI image tag.

    Raises HTTPException(422) if invalid.
    """
    if not value or not _OCI_TAG_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid {field}: an image tag must match "
                r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ "
                f"(got {value!r})"
            ),
        )
    return value


def validate_oci_repository(value: str, field: str = "repository") -> str:
    """Validate an OCI repository path (may be multi-segment: ``team/sub``).

    Raises HTTPException(422) if invalid.
    """
    if not value or len(value) > 255 or not _OCI_REPOSITORY_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid {field}: a repository is lowercase path components "
                f"joined by '/' (got {value!r})"
            ),
        )
    return value


def validate_harbor_project(value: str, field: str = "project") -> str:
    """Validate a Harbor project name (a single lowercase path component).

    Raises HTTPException(422) if invalid.
    """
    if not value or len(value) > 255 or not _HARBOR_PROJECT_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid {field}: a Harbor project is a single lowercase "
                f"path component with no '/' (got {value!r})"
            ),
        )
    return value


def k8s_error_to_http(e: ApiException, action: str = "operation") -> HTTPException:
    """Convert K8s ApiException to safe HTTPException without leaking internals."""
    status_map = {
        400: (400, "Bad request"),
        401: (401, "Unauthorized"),
        403: (403, "Access denied"),
        404: (404, "Resource not found"),
        409: (409, "Resource conflict"),
        422: (422, "Invalid resource configuration"),
        # The API server asking to be asked again. Flattened into a 500 it
        # reads as "this is broken" — seen in UAT run 4, where listing disk
        # snapshots came back 500 and the very next request succeeded. A
        # client has no reason to retry a 500 and every reason to retry this.
        429: (429, "The cluster is rate-limiting this request — try again"),
        # Likewise for a control plane that is briefly unavailable: retriable,
        # and nothing about it is internal to us.
        503: (503, "The cluster API is unavailable — try again"),
    }
    status, detail = status_map.get(e.status, (500, f"Internal error during {action}"))
    logger.warning(f"K8s API error during {action}: {e}")
    return HTTPException(status_code=status, detail=detail)
