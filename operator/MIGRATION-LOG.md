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
