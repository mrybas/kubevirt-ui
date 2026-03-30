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


def k8s_error_to_http(e: ApiException, action: str = "operation") -> HTTPException:
    """Convert K8s ApiException to safe HTTPException without leaking internals."""
    status_map = {
        400: (400, "Bad request"),
        401: (401, "Unauthorized"),
        403: (403, "Access denied"),
        404: (404, "Resource not found"),
        409: (409, "Resource conflict"),
        422: (422, "Invalid resource configuration"),
    }
    status, detail = status_map.get(e.status, (500, f"Internal error during {action}"))
    logger.warning(f"K8s API error during {action}: {e}")
    return HTTPException(status_code=status, detail=detail)
