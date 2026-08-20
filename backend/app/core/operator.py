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


def template_path_enabled() -> bool:
    """True when VM templates are written as ManagedVMTemplate resources.

    Off: templates are JSON blobs under user-chosen keys in one cluster-wide
    ConfigMap, rewritten whole on every change.
    On: each template is its own object, unique per namespace by the API
    server, and its reference to an image is a name that exists before the
    image does — which is what makes a template writable from a manifest.
    """
    return _enabled("OPERATOR_TEMPLATE_ENABLED")


def announce_path_enabled() -> bool:
    """True when the operator owns the BGP announcements.

    This one is not a routing choice, it is an ownership switch, and it has to
    move in the same change as the operator's policy leaving dry-run. frr-k8s
    merges every FRRConfiguration in its namespace into the node's FRR, so two
    writers of that object are not two opinions — they are two `router bgp`
    blocks fighting over one session.

    The handover is provable before it happens: the operator publishes what it
    would write while this is off, and the two are compared byte for byte.
    """
    return _enabled("OPERATOR_ANNOUNCE_ENABLED")


# The group the operator's custom resources live in.
OPERATOR_GROUP = "platform.kubevirt-ui.io"
OPERATOR_VERSION = "v1alpha1"

# Stamped by the operator on every object it creates, so the backend can tell
# "a disk the operator owns" from "a disk we made before the migration" without
# guessing from names.
OWNER_KIND_LABEL = "platform.kubevirt-ui.io/owner-kind"
OWNER_NAME_LABEL = "platform.kubevirt-ui.io/owner-name"


def operation_name(action: str, vm_name: str) -> str:
    """A stable-ish name for an operation object.

    Operations are immutable records of one act, so each needs its own name.
    The suffix comes from the clock rather than a counter: a counter would need
    a read-modify-write to allocate, and the whole point of these objects is
    that nothing about them depends on a process staying alive.
    """
    import time

    return f"{vm_name}-{action.lower()}-{int(time.time())}"


async def patch_managed_disks(
    custom_api,
    namespace: str,
    owner: str,
    claim: str,
    attach: bool,
    volume_name: str | None = None,
    bus: str | None = None,
) -> None:
    """Add or remove a disk on the machine's declared list.

    Read-modify-write on one field of one object: the list is small, it belongs
    to one owner, and the alternative — a strategic merge on a list of maps —
    silently drops entries the client did not know about.
    """
    vm = await custom_api.get_namespaced_custom_object(
        group=OPERATOR_GROUP, version=OPERATOR_VERSION,
        namespace=namespace, plural="managedvms", name=owner,
    )
    disks = list((vm.get("spec", {}) or {}).get("disks") or [])
    disks = [d for d in disks if d.get("claim") != claim]
    if attach:
        entry: dict = {"claim": claim}
        # The attach dialog lets a person name the volume separately from the
        # claim; carrying only the claim would drop that choice silently.
        if volume_name and volume_name != claim:
            entry["name"] = volume_name
        if bus:
            entry["bus"] = bus
        disks.append(entry)

    await custom_api.patch_namespaced_custom_object(
        group=OPERATOR_GROUP, version=OPERATOR_VERSION,
        namespace=namespace, plural="managedvms", name=owner,
        body={"spec": {"disks": disks}},
        _content_type="application/merge-patch+json",
    )


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
