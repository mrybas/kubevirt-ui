"""Which write paths go through the operator's CRDs.

The migration is per path and reversible: with a flag off the backend writes
the cluster objects it has always written, with it on it writes our custom
resource and the operator writes the cluster objects. Exactly one of the two is
ever the writer of a given object — the class of bug where two writers share one
list is the reason the operator exists at all, and it would be an odd way to
start to reproduce it here.

Flags are read through these accessors and never bound at import time. A
module-level `FLAG = os.getenv(...)` freezes the value at import, which has
already cost this codebase a rule that existed in the code, passed its tests,
was present in the object, and was absent from the cluster.
"""

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def image_path_enabled() -> bool:
    """True when golden images are written as ManagedImage custom resources.

    Off: the backend creates the CDI DataVolume itself, as it always has.
    On: the backend creates a ManagedImage and the operator creates the disk,
    the DataSource, and the status the UI reads.
    """
    return _enabled("OPERATOR_IMAGE_ENABLED")


def vm_path_enabled() -> bool:
    """True when VMs are written as ManagedVM custom resources.

    Off: the backend renders the KubeVirt VirtualMachine itself.
    On: the backend writes intent and the operator renders the machine, which
    is also what makes the same rules apply to anything else writing the
    resource — a Terraform module, a pipeline, a person with kubectl.
    """
    return _enabled("OPERATOR_VM_ENABLED")


# The group the operator's custom resources live in.
OPERATOR_GROUP = "platform.kubevirt-ui.io"
OPERATOR_VERSION = "v1alpha1"

# Stamped by the operator on every object it creates, so the backend can tell
# "a disk the operator owns" from "a disk we made before the migration" without
# guessing from names.
OWNER_KIND_LABEL = "platform.kubevirt-ui.io/owner-kind"
OWNER_NAME_LABEL = "platform.kubevirt-ui.io/owner-name"


def managed_owner(obj: dict, kind: str) -> str | None:
    """Name of the custom resource that owns this object, if any.

    Ownership is a property of the object, not of a feature flag: an object
    created while the operator owned that path keeps being the operator's after
    the flag goes off, and writing to it directly would be undone on the next
    reconcile.
    """
    labels = ((obj or {}).get("metadata", {}) or {}).get("labels", {}) or {}
    if labels.get(OWNER_KIND_LABEL) != kind:
        return None
    return labels.get(OWNER_NAME_LABEL) or None
