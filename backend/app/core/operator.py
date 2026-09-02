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

    This is an ownership switch, and there is no atomic step for it: this flag
    lives on a Deployment and the operator's dry-run lives on a custom resource.
    Only one order is safe — this flag on first, which takes the number of
    writers from one to none while the existing configuration stays exactly as
    it is, and then the operator out of dry-run. Backwards there are two, and
    frr-k8s merges every FRRConfiguration in its namespace into one FRR.

    The handover is provable before it happens: the operator publishes what it
    would write while this is off, and the two are compared byte for byte.
    """
    return _enabled("OPERATOR_ANNOUNCE_ENABLED")


def network_path_enabled() -> bool:
    """True when VPCs are written as ManagedNetwork custom resources.

    Off: the create endpoint writes the Vpc, the Subnet and the VpcDns itself.
    On: it writes one object and the operator writes those three, plus the
    service-network route on the resolver — which is the reason the switch is
    worth making. That route used to be applied once, best-effort, at create
    time, and only a person calling the recreate endpoint ever applied it again.

    What does *not* move with this flag: the CIDR allocator, namespace
    validation, and VPC peering. Peering has its own —
    `OPERATOR_PEERING_ENABLED` — deliberately: an upgrade must not change the
    meaning of a flag somebody already turned on.

    Isolation ACLs used to be on that list and are not any more. `Subnet.spec.acls`
    moves per subnet rather than per flag: a subnet the composer created carries
    `platform.kubevirt-ui.io/acl-owner: operator`, and the census here leaves
    those lists alone — ownership is read off the object, so it stays true after
    the flag goes off.
    """
    return _enabled("OPERATOR_NETWORK_ENABLED")


def tenant_path_enabled() -> bool:
    """True when creating a tenant writes a ManagedTenant and stops there.

    Off: the endpoint builds the tenant itself — namespace, quota, PKI, golden
    image, CAPI objects, transit, addons — in one request handler, and whatever
    it did not finish is not finished by anybody. That is not a theory: a tenant
    created here lost the race with its own transit EIP getting an address, the
    handler logged "ACLs deferred to the next reconcile", and there is no
    reconcile — `_wire_tenant_to_transit` has exactly one caller and it is
    create. The workers dialled a control plane an ACL was dropping, forever,
    and every condition the product shows stayed green.
    On: the same description goes into one object and the operator builds it,
    every pass, from a watch. A step that loses a race is retried instead of
    logged.

    The three narrower tenant flags stay meaningful and are not implied by this
    one. They decide who writes *parts* of a tenant that already exists — its
    bootstrap CA, its clock, its addons — and a half-migrated cluster is exactly
    the state they exist to make workable. This one decides who builds a new
    one.

    **New tenants only.** The ones that exist are untouched and stay the
    product's: ownership is the ManagedTenant object, so a tenant the operator
    has no object for is still built and repaired here. Turning this off orphans
    nothing, turning it on rewrites nothing, and there is no cutover day.

    What the object cannot say, this path refuses rather than drops. The wizard
    collects more than `ManagedTenantSpec` has fields for — a worker image URL,
    DNS servers, OIDC group names — and a request carrying one of them would
    otherwise be accepted, described without it, and built into a tenant that
    silently differs from what was asked for. See `_undescribable_fields`.
    """
    return _enabled("OPERATOR_TENANT_ENABLED")


def tenant_bootstrap_path_enabled() -> bool:
    """True when the operator repairs worker bootstrap templates.

    Off: `reconcile_loop` calls `ensure_worker_bootstrap_ca` every pass — on a
    timer, in the request-serving process, with no leader election, so two
    replicas do it twice and none does it after a restart.
    On: the operator watches the template, the MachineDeployment and the CA
    secret, and repairs on a write rather than on a clock.

    Both writing the same repair is not dangerous — it is create-if-absent and a
    patch to the same value — but two writers of one thing is what this
    migration is for, and the order is the same as everywhere else: this flag on
    first, then the operator's controller.
    """
    return _enabled("OPERATOR_TENANT_BOOTSTRAP_ENABLED")


def tenant_time_path_enabled() -> bool:
    """True when the operator owns the tenant time source.

    Off: creating a tenant writes the shared chrony Deployment and ConfigMap and
    the per-tenant NTP Service.
    On: none of it, because the ManagedTenant controller renders the same three
    objects — and two renderers of one Deployment is worse than two writers of a
    Service. Any difference between them rolls chrony from whichever side wrote
    last, and chrony is what a joining worker asks for the time: rolling it
    during a join is a node that does not appear.

    Order as always: this flag on first, then the operator's tenant domain.
    """
    return _enabled("OPERATOR_TENANT_TIME_ENABLED")


def tenant_addons_path_enabled() -> bool:
    """True when the operator installs a tenant's addons.

    Off: the create path writes the HelmReleases and the reconcile loop writes
    any that are missing — two renderers of one object, and they do not agree.
    The repair one omits `install.disableWait`, whose absence wedges a CNI
    release in `uninstalling` for ever, and it fires exactly when a release is
    missing, which is the state a fresh tenant is in.
    On: neither writes, and the operator renders both cases from one function.

    Order as always: this flag on first, then the operator's tenant domain.
    """
    return _enabled("OPERATOR_TENANT_ADDONS_ENABLED")


def underlay_path_enabled() -> bool:
    """True when the egress underlay is written as a ManagedUnderlay resource.

    Off: the POST builds the four fabric objects itself and the GET quietly
    heals the gateway node label on its way past.
    On: the backend writes one object describing the site's physical network and
    the operator keeps the fabric, the label and the workaround DaemonSets in
    line with it — on a watch rather than on a page view, which is the whole
    reason this path is being moved. The label was healed only when somebody
    opened the page; on this lab it sat at `false` on all three workers for two
    hours, and the link watcher that selects on it was scheduled nowhere and
    reported success.
    """
    return _enabled("OPERATOR_UNDERLAY_ENABLED")


def peering_path_enabled() -> bool:
    """True when a new VPC peering is written as a ManagedNetworkPeering.

    Off: the endpoint patches both routers itself.
    On: it writes one object and the operator writes both ends — or neither,
    which is the difference that matters. The endpoint holds the list of applied
    ends in a local variable and undoes them in an `except`; a process that
    stops between the two writes leaves a peering configured on one side only,
    which is a black hole with nothing anywhere remembering to undo it. The
    controller records each end in status before attempting it.

    The larger reason is drift rather than crashes. A peering is desired state
    held on two routers, and today nothing reconciles it: an entry removed by
    hand, or lost by kube-ovn, stays lost. A REST call has nowhere to notice.

    **Only new peerings.** The ones that exist stay exactly as they are and stay
    the product's: ownership is the ManagedNetworkPeering object itself — a pair
    belongs to the operator when an object claims it — so turning this off
    orphans nothing and turning it on rewrites nothing. There is no cutover day.

    The operator refuses to take over a pair that is already written unless it
    is told to, because `Vpc.spec.vpcPeerings` is what the isolation pass reads
    to decide what to allow: a second writer there does not break a peering, it
    breaks the isolation of both neighbours.
    """
    return _enabled("OPERATOR_PEERING_ENABLED")


def harbor_image_path_enabled() -> bool:
    """True when images may also come from a Harbor catalogue.

    Off: the image list is exactly the cluster's DataVolumes, as before.
    On: the list additionally carries Harbor artifacts the requesting user is
    allowed to see, and the publish and materialise endpoints are mounted.
    """
    return _enabled("HARBOR_IMAGE_ENABLED")


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
