"""Resource naming utilities.

Generates K8s-safe names from human-friendly display names.

Primary pattern: `metadata.generateName: {slug}-` — the K8s API server appends a
5-character suffix server-side, guaranteeing uniqueness via etcd CAS. No collision
retry needed in our code.

Display names are stored in annotation `kubevirt-ui.io/display-name`.
For search/filter, the sanitized slug is stored in label `kubevirt-ui.io/slug`.

Resources created by Terraform/CLI without our annotations show metadata.name as fallback.
"""

import re
import uuid

# Annotation/label keys
DISPLAY_NAME_ANNOTATION = "kubevirt-ui.io/display-name"
SLUG_LABEL = "kubevirt-ui.io/slug"
# Unique-per-VM label whose value == the VM's metadata.name. The slug
# label is NOT unique (display-name collisions); the synthetic name is.
# Stamped post-create (the name comes from generateName, server-assigned)
# so Velero can target an individual VM via orLabelSelectors for the
# folder→env→VM backup wizard. The kubevirt-velero-plugin then follows
# the VM to its DataVolumes/PVCs.
VM_NAME_LABEL = "kubevirt-ui.io/vm-name"

# K8s naming constraints
DNS_1123_MAX = 63
K8S_GENERATENAME_SUFFIX_LEN = 5  # K8s API server appends 5 chars to generateName
# Slug max = 63 - 5 (k8s suffix) - 1 (dash separator) = 57
SLUG_MAX = DNS_1123_MAX - K8S_GENERATENAME_SUFFIX_LEN - 1


def sanitize_display_name(display_name: str, max_length: int = SLUG_MAX) -> str:
    """Convert a human-friendly name to a K8s-safe slug.

    Rules:
    - ASCII-only, lowercase
    - Non-alphanumeric characters collapsed to a single hyphen
    - Leading/trailing hyphens stripped
    - Truncated to ``max_length`` characters
    - Empty/all-symbol input falls back to "unnamed"

    Examples:
        "Ubuntu 24.04 Server" -> "ubuntu-24-04-server"
        "My   Weird---Name!!!" -> "my-weird-name"
        "" -> "unnamed"
        "!!!" -> "unnamed"
        "123abc" -> "123abc"  (digit-leading OK for DNS-1123 subdomain)
    """
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:max_length]
    slug = slug.rstrip("-")
    return slug or "unnamed"


# Backwards-compatible alias for existing callers.
slugify = sanitize_display_name


def with_synthetic_metadata(
    body: dict,
    display_name: str,
    namespace: str | None = None,
) -> dict:
    """Inject generateName, display-name annotation, and slug label into a K8s resource body.

    Mutates and returns ``body``. After POST, the actual name is in ``response.metadata.name``.

    Usage:
        body = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "spec": {...}}
        with_synthetic_metadata(body, "Web Server (prod)", namespace="default")
        response = await api.create_namespaced_custom_object(..., body=body)
        actual_name = response["metadata"]["name"]   # e.g. "web-server-prod-q4r8z"
    """
    slug = sanitize_display_name(display_name)
    md = body.setdefault("metadata", {})
    md["generateName"] = f"{slug}-"
    # generateName and name are conceptually exclusive — drop any pre-set name
    md.pop("name", None)
    if namespace:
        md["namespace"] = namespace

    annotations = md.setdefault("annotations", {})
    annotations[DISPLAY_NAME_ANNOTATION] = display_name

    labels = md.setdefault("labels", {})
    labels[SLUG_LABEL] = slug

    return body


def generate_k8s_name(display_name: str) -> str:
    """DEPRECATED — kept for backwards compatibility while callers migrate.

    Prefer ``with_synthetic_metadata()`` which leverages K8s generateName for
    collision-free server-side uniqueness.

    This function generates a name locally using a uuid4 hex suffix:
    "Ubuntu 24.04 Server" -> "ubuntu-24-04-server-a7f3e2"
    """
    slug = sanitize_display_name(display_name)
    suffix = uuid.uuid4().hex[:6]
    return f"{slug}-{suffix}"


def get_display_name(metadata: dict, fallback_to_name: bool = True) -> str | None:
    """Extract display name from K8s resource metadata.

    Reads from annotation; falls back to ``metadata.name`` if requested.
    Works for both UI-created and Terraform/CLI-created resources.
    """
    annotations = metadata.get("annotations") or {}
    display_name = annotations.get(DISPLAY_NAME_ANNOTATION)
    if display_name:
        return display_name
    if fallback_to_name:
        return metadata.get("name")
    return None
