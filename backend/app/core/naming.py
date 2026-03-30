"""Resource naming utilities.

Generates K8s-safe names from human-friendly display names.
Pattern: {slug}-{uuid6}  (e.g. "ubuntu-24-04-server-a7f3e2")

Display names are stored in annotation `kubevirt-ui.io/display-name`.
Resources created by Terraform/CLI without our annotations will show
metadata.name as fallback — no special handling needed.
"""

import re
import uuid

# Annotation key for display name
DISPLAY_NAME_ANNOTATION = "kubevirt-ui.io/display-name"


def slugify(display_name: str, max_length: int = 50) -> str:
    """Convert a human-friendly name to a K8s-safe slug.

    "Ubuntu 24.04 Server" → "ubuntu-24-04-server"
    "My   Weird---Name!!!" → "my-weird-name"
    """
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:max_length]
    # Ensure it doesn't end with a dash after truncation
    slug = slug.rstrip("-")
    return slug or "resource"


def generate_k8s_name(display_name: str) -> str:
    """Generate a unique K8s-safe name from a display name.

    "Ubuntu 24.04 Server" → "ubuntu-24-04-server-a7f3e2"

    Uses 6 hex chars from uuid4 — 16.7M combinations, collision-free
    for practical purposes within a single namespace.
    """
    slug = slugify(display_name)
    suffix = uuid.uuid4().hex[:6]
    name = f"{slug}-{suffix}"
    # K8s names max 253 chars for most resources, 63 for labels
    # slug is already capped at 50 + 7 for suffix = 57 max
    return name


def get_display_name(metadata: dict, fallback_to_name: bool = True) -> str | None:
    """Extract display name from K8s resource metadata.

    Reads from annotation, falls back to metadata.name if requested.
    Works for both our UI-created resources and Terraform/CLI-created ones.
    """
    annotations = metadata.get("annotations") or {}
    display_name = annotations.get(DISPLAY_NAME_ANNOTATION)
    if display_name:
        return display_name
    if fallback_to_name:
        return metadata.get("name")
    return None
