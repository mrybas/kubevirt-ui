# Migration log

One entry per migrated slice. Each records what was compared against the
existing UI path, what matched, and every difference that was left in place on
purpose. A difference nobody wrote down is a difference someone will later read
as a bug.

Stand: dev cluster `kubevirt-lab`, UI https://kubevirt-ui.lab.beardlabs.cc/,
polygon namespace `opdev-dev` (folder `opdev`, env `dev`), operator in
`kubevirt-ui-operator-dev` deployed with kustomize, outside the chart and Flux.

---

## M0 — operator skeleton, dev CI, polygon (done)

Built `dev-8377fab`, deployed, verified the running pod carried that exact tag
before measuring anything.

Accepted:

- healthz/readyz answer 200; metrics served on :8080.
- Domain isolation is real, not a plan: with `--domains=network` the
  ManagedImage controller does not start and the process takes the lease
  `network.operator.kubevirt-ui.io`, while the `vm` lease keeps its own holder.
  Both profiles coexisted, neither disturbed the other.
- A typo (`--domains=netwrok`) crashes on startup naming the four known domains,
  and the healthy pod stays up through the failed rollout. A manager that
  silently watches nothing is indistinguishable from a reconciled cluster, so
  this fails loudly by design.
- Full rollback verified: `kustomize build … | kubectl delete -f -` leaves zero
  namespace, zero CRDs, zero ClusterRoles, zero ClusterRoleBindings.
- Polygon created through the UI, not by hand: namespace `opdev-dev` carries
  `kubevirt-ui.io/folder=opdev`, `environment=dev`, `enabled=true`,
  `managed=true`, with a ResourceQuota (8 CPU / 16Gi / 300Gi) and a LimitRange —
  the env-quota shape the product produces (requests+limits+LimitRange).

## M1 — ManagedImage (done)

Built `dev-042825b`, deployed, pod image verified.

Live: `ManagedImage/ubuntu-2404-crd` in `opdev-dev` went Pending → Importing →
Ready in about two minutes, published `status.dataVolumeName`,
`status.dataSourceName`, `progress: 100.0%`, and a `Ready` condition with
`reason: Imported`. The same image was then imported through the old UI as
"Ubuntu 24.04 UI" into the same namespace.

**Both DataVolumes compared field by field. Identical:** every
`kubevirt-ui.io/*` label the product filters on (`managed`, `disk-type`,
`persistent`, `scope`, `os-type`), the slug rule (both derive the slug from the
display name), the `display-name` annotation, `spec.source.http.url`,
`spec.storage.volumeMode: Block`, and the requested size.

**Differences, all deliberate:**

| # | Difference | Why it stays |
|---|---|---|
| 1 | DV name: UI `ubuntu-24-04-ui-49c5f` (slug + generateName) vs CRD `ubuntu-2404-crd` (the resource's own name) | This is the point of the CRD. A one-to-one child inherits its parent's name, so nothing has to be read back before it can be referenced. The generated name remains available in `status.dataVolumeName`. |
| 2 | CRD disks carry `platform.kubevirt-ui.io/owner-{uid,name,kind}` | Children are found by owner label, never by parsing names. |
| 3 | CRD path also publishes a CDI `DataSource` | So KubeVirt-native consumers and a future DataImportCron can reference the image without knowing this operator exists. |
| 4 | `storageClassName`: UI writes `ceph-block` explicitly, CRD omits it | Same disk today, because `ceph-block` *is* the cluster default. Omitting is the more honest declaration: the UI's "Storage Class: Default (ceph-block)" sends the concrete class, so an image the user asked to put on "the default" is pinned to whichever class was default the day it was created. Recorded as a UI defect below rather than copied into the new path. |
| 5 | `os-version` present only on the CRD disk | Input difference, not behaviour: the UI import dialog has no OS-version field. |

**Also observed, and it is the load-bearing result:** the CRD-created image shows
up in the old UI's image list unchanged — same namespace, size, Ready state,
scope column. Nothing in the UI was touched to make that work; the operator
writes the vocabulary the product already reads.

### Defects found in the existing UI while comparing

- **UI-1 (cosmetic-but-sticky):** the Import Image dialog defaults Storage Class
  to `Default (ceph-block)` and submits the concrete class name. Choosing
  "default" should mean "do not pin a class". Not fixed yet: fixing it changes
  behaviour for every existing import path, so it belongs to the FastAPI
  fix track, not to a migration step that is supposed to prove equivalence.

### Backend rewired behind `OPERATOR_IMAGE_ENABLED`

The backend now writes a ManagedImage instead of a DataVolume when the flag is
on, keeping `generateName: <slug>-` on our own resource — so the disk still ends
up named `<slug>-xxxxx` and every caller that reads a name back is unaffected.
Declared in `helm/kubevirt-ui/values.yaml`, and the env-inventory test was
widened to cover the `OPERATOR_` prefix so the next flag cannot be added without
being declared.

Deletion follows **ownership, not the flag**. A disk the operator owns is
released by deleting its ManagedImage; deleting the disk directly would only
make the controller rebuild it. Ownership is stamped on the object, so this
stays correct after the flag goes off. An image still in use answers 409 naming
the holders, rather than accepting a delete the operator will then refuse
asynchronously — from a browser that looks like a button that did nothing.

Live, against the dev cluster with the local UI on :3333:

| Check | Result |
|---|---|
| Same import through the browser form, flag on | Created `ManagedImage/ubuntu-24-04-form-5d4ms`; operator built the disk and the DataSource |
| **Disk written by the old path vs by the operator** | **Identical**: every `kubevirt-ui.io/*` label, the display-name annotation, `spec.source`, size, `volumeMode: Block` — and `storageClassName: ceph-block`, because the form sends it and the flag path passes it through. Only the name (and its slug) differ. |
| Delete of an operator-owned image | ManagedImage, DataVolume and DataSource all removed; the disk did **not** come back |
| Delete of an unowned (pre-migration) disk | Deleted directly, as before |
| Flag off → create | Plain DataVolume again, no ManagedImage. Rollback proven, not assumed. |

Still owed on this slice: the in-use refusal is proven in envtest but not yet
live — it needs a real VM cloning from the image, which arrives with M2.

### Defects fixed in passing

- **Frontend Dockerfile** installed `@rollup/rollup-linux-x64-musl`
  unconditionally, so the dev image could not be built on an arm64 workstation
  at all. Now it installs the binary for the architecture being built.

---

## M2 research — the CDI clone gate in the VM path (measured, settles an open question)

The plan carried this as an assumption to be measured before writing the VM
controller: a cross-namespace clone needs `create datavolumes` with subresource
`source` in the *source* namespace, and it was unclear which subject is checked
when the DataVolume is materialised by KubeVirt from `dataVolumeTemplates`
rather than created by us. Guessed wrong is expensive here, because a lab
running as cluster-admin hides the entire class.

Measured on the stand (KubeVirt v1.9.0, CDI v1.66.0):

1. **virt-api does not reject the VM.** A subject with no rights in the source
   namespace created the VirtualMachine successfully — admission is clean.
2. **The DataVolume is then never created**, and the only trace is a Warning
   event on the VM: `UnauthorizedDataVolumeCreate — User
   system:serviceaccount:opdev-tgt:default has insufficient permissions in
   clone source namespace opdev-dev`.
3. **So the checked subject is the `default` ServiceAccount of the VM's own
   namespace** — not the VM's creator, and not virt-controller (which does hold
   the permission cluster-wide: `can-i create datavolumes --subresource=source`
   answers yes for `kubevirt-controller`, no for every namespace `default`).
4. **Same-namespace clones are not gated at all.** A VM in `opdev-dev` cloning
   `opdev-dev/ubuntu-2404-crd` reached `Succeeded` in 26 seconds with no
   permission granted and no warning event.

Consequences, all now facts rather than guesses:

- The common case (image and VM in one namespace) needs nothing.
- Cross-namespace `imageRef` needs a Role in the **source** namespace bound to
  `ServiceAccount/<vm-namespace>/default`. That is exactly the shape the tenant
  path already uses — `talos-golden-cloner` in `kubevirt-ui-system`, bound to
  `ServiceAccount/tenant-uat-t1/default`. The plan expected a *different*
  subject here; measured, it is the same one, so the existing pattern is
  reusable rather than re-derived.
- **The failure mode is silence.** The VM is admitted, exists, looks healthy,
  and simply never provisions. The controller must find the reason where it
  actually lives — a field-selected Warning event — and report
  `ImageAccessDenied`, or every cross-namespace mistake becomes a support
  ticket that starts with "the VM is stuck in Pending".
- **Security consequence worth stating plainly:** because the gate is skipped
  within a namespace and satisfied by a Role we would grant across namespaces,
  RBAC is *not* what stops one team reading another team's disk through a VM.
  The scope/ancestry check in our webhook is. That moves it from a product
  nicety to the actual access control, and it is why the guard on raw
  `kubevirt.io/VirtualMachine` creation is load-bearing rather than tidy.

---

## M2 — ManagedVM (controller done; admission webhook and raw-VM guard pending)

Live on the stand, `opdev-dev`:

**Apply order is not part of the API.** One file with the VM listed *before* the
image it clones: the VM reported `ImageNotReady — ManagedImage
opdev-dev/ubuntu-fresh is Importing; cloning from an unfinished disk produces a
broken VM`, created nothing, and then provisioned itself the moment the import
finished. No re-apply, no restart. The root disk cloned through CDI's CSI fast
path as `opvm-crd-root-1`, and the VM reached `Running: True`.

**The main check of the task — the rendered VirtualMachine against the one the
old UI produces.** Same template, same cores, memory, disk, network, no SSH key,
no password, not started. Diff of the two objects, with the generated names
normalised:

```
-    kubevirt-ui.io/owner: anonymous@local
-  generateName: tpl-vm-ui-
+    platform.kubevirt-ui.io/owner-kind: ManagedVM
+    platform.kubevirt-ui.io/owner-name: opvm-tpl2
+    platform.kubevirt-ui.io/owner-uid: f4d0e69a-…
-          serial: a4e19aae-…   uuid: 727dcab3-…
+          serial: 20eb3036-…   uuid: d06a9319-…
```

That is the whole diff. Identical: `dataVolumeTemplates` (clone source, storage,
labels), volumes, disks, interfaces, networks, the cloud-init document and its
network data, CPU requests and limits, memory, console flags, runStrategy, and
every `kubevirt-ui.io/*` label the folder views filter on. The four differences
are the owner annotation (the UI records a person; a stored object has none
until the UI supplies it), `generateName` versus an explicit name, our ownership
labels, and the firmware identifiers KubeVirt generates per machine.

Both operator-created VMs appear in the unmodified UI with the right display
name, CPU, memory and address.

### Defect found by the comparison, and fixed: two answers to "may I use this network"

The wizard offered no networks at all for `opdev-dev`, while the operator had
happily attached a VM to `uat-net-vm-default`. The wizard was right: that VPC
carries `kubevirt-ui.io/folder=poc-transit`, and the wizard hides VPCs belonging
to other folders. The create path never checked, so the rule existed in the
picker and nowhere else — a VM created by any other route could attach to
another team's network.

Fixed by porting the rule into `internal/scope`, which is now the only place
that answers the question, and calling it from the controller: a subnet out of
scope is refused with both folder names in the message. The same package will
back the admission webhook, so the picker and the enforcement cannot drift
again. Also enforced there, from the same measurement: only the primary NIC may
be a VPC overlay — the create path checked this and the hot-plug path did not.

### Divergences from the old path, deliberate

| # | Old behaviour | New behaviour | Why |
|---|---|---|---|
| 1 | A subnet lookup that failed was treated as a VLAN, with the subnet name used as the attachment name | `NetworkNotFound`, named | The old rule turns a typo into a VM wired to an attachment that does not exist; the only symptom is a guest with no address |
| 2 | The VPC resolver address was derived from the service CIDR (`x.y.z.200`) | Read from the subnet's own DHCP options, falling back to kube-ovn's `vpc-dns-config` | The formula is right on this cluster and a guess on any other |
| 3 | Supplying user-data silently discarded the initial password | The password is always applied | Two branches of one handler; a password that vanishes because an unrelated field was filled in is a defect |
| 4 | The `vm-name` label was patched in after create, failure swallowed | Rendered with the object | Backup selection targets that label, so best-effort meant occasionally unselectable VMs |
| 5 | project/environment labels copied once at create | Reconciled every pass | A VM created before its namespace was labelled stayed invisible to folder views forever |
