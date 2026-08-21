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

### Admission and the guard (live)

Both webhooks served by the operator, certificate issued by cert-manager.

| Check | Result |
|---|---|
| Raw KubeVirt VM, cluster-admin identity not on the allowlist | **Denied** by `guard-virtualmachine.kb.io`, message says to create a ManagedVM instead |
| Raw KubeVirt VM as `o0-capi:capk-manager` (the tenant machinery) | Allowed |
| Raw KubeVirt VM as `kubevirt-ui-system:kubevirt-ui` (the UI backend, until M7) | Allowed |
| Raw KubeVirt VM in a namespace without `kubevirt-ui.io/enabled` | Allowed — the guard is none of its business |
| ManagedVM naming a subnet from another folder | **Refused at admission**: "subnet uat-net-vm-default belongs to folder poc-transit and this namespace is in folder opdev" |
| ManagedVM naming a subnet that does not exist yet | **Accepted** — apply order is not part of the API; the controller waits |
| Resizing a running machine | **Refused**: "cores and memory can only be changed while the machine is stopped" |
| Stop, then resize | Applied, and it **reached the machine**: cpu 4, memory 8Gi, requests and limits updated — with the disk arrays untouched |

Tenant namespaces do carry `kubevirt-ui.io/enabled=true`, so the guard does
police them. That is why the CAPK identity is on the allowlist and has a test of
its own; a guard that forgot it would not tighten policy, it would stop tenants
from replacing an unhealthy worker.

### The bug this nearly shipped with, and why it was nearly invisible

The first deployment of the guard admitted a raw VM from a cluster-admin
identity. It was not a logic error: kustomize wrote the `inject-ca-from`
annotation against the base's namespace, this overlay moves everything to
another one, cert-manager therefore injected nothing, the API server could not
verify the webhook's TLS — and because the guard **fails open on purpose**, it
admitted everything with no error visible anywhere except `tls: bad certificate`
in the operator's own log.

Worth writing down as a rule rather than a fix: a webhook with
`failurePolicy: Ignore` that is not wired up is indistinguishable from one that
is, from every side except the thing it was supposed to stop. Its acceptance
test is therefore not "the object was created" — it is "a request that must be
refused, was". The overlay now patches the annotation explicitly, and the same
class is why the certificate's dnsNames are recomputed here too.

---

## M7 — the UI writes ManagedVM (done)

Behind `OPERATOR_VM_ENABLED`, the create endpoint writes a ManagedVM and the
operator renders the machine.

**The comparison, through the same endpoint with the same template**, differed
by two lines: the `generateName` KubeVirt keeps on the old object, and our
`owner-kind` label. Everything else — the owner annotation, the template label,
the disk, the cloud-init, the compute, the console flags — identical.

Live, through the endpoints the UI buttons call:

| Action | Result |
|---|---|
| Create | `ManagedVM/flag-vm-9xvq2` → operator rendered the machine |
| Start | `spec.running: true` → `runStrategy: Always` |
| Stop | `spec.running: false` → `runStrategy: Halted` |
| Delete | resource and machine both gone; the machine did not come back |

Three things became explicit in the translation, all of them previously
implicit and one of them a defect: the profile's SSH keys (the handler injected
them silently and installed *none* when the profile read failed — a VM with no
way in and nothing saying so), the owner annotation, and the password, which now
goes into a Secret the resource points at rather than into the resource itself.

### Two defects found by running it

**Deleting a VM left the machine running.** The controller deliberately did not
cascade, which is right for a migration rollback and wrong for a person pressing
Delete. The cascade is a finalizer in the operator — not a second delete from
the backend, which races the controller into recreating what was just deleted,
and not an ownerReference, so it stays opt-out: strip the finalizer and machines
outlive their resources, which is what a rollback needs.

**The controller silently adopted any VirtualMachine sharing the name**, which
meant it would later delete a machine it never created. Found by the test
written for the cascade. Adoption is a deliberate annotation now; a collision is
reported with the annotation to set.

### The guard's fail-open posture, made observable

`failurePolicy: Ignore` is deliberate — failing closed would turn an operator
outage into a tenant outage by blocking the machinery that replaces unhealthy
workers — but this exact policy had already hidden a real break. So the wiring is
checked rather than assumed: a watchdog reads the webhook configuration and
reports `kubevirt_ui_operator_guard_wired` — it exists, it routes here, it has a
CA bundle. It checks the wiring rather than inferring health from traffic,
because traffic cannot tell "never consulted" from "nothing to refuse".

Verified both ways on the stand: wired → `1`; pointed at a configuration that
does not exist → `0` **and** a log line naming the missing configuration.

And a lesson from testing the gauge itself: the first negative reading was taken
eight seconds after a restart, before the watchdog had run at all — an unset
Prometheus gauge reads exactly like one set to zero, so it looked like proof and
was not. There is now a `guard_last_check_timestamp_seconds` alongside it, so
"broken" and "nobody has looked yet" are distinguishable.

---

## M3 — ManagedVMTemplate (done)

Templates were JSON values under user-chosen keys in one cluster-wide ConfigMap,
rewritten whole on every change. Now each is an object, named per namespace by
the API server, referencing a ManagedImage by a name that can be written before
either object exists.

The controller creates nothing — a template is data — and reports the one thing
nobody could see before: whether the image it points at is there. Deliberately
*not* whether that image is ready; that is the image's own status, and two
objects answering one question is how they come to disagree.

Reading covers both stores during the migration, with a resource shadowing a
legacy entry of the same name. Writing follows the flag
(`OPERATOR_TEMPLATE_ENABLED`); deleting follows the object, because the flag
says where new templates go, not where the old ones live. A bare name that
exists in two namespaces is reported, not guessed.

### Migration order, learned by breaking it

Running the template migration first turned a working template into a broken
one: `uat-ubuntu-small` migrated cleanly and immediately reported
`ImageFound: False`, because the image it names was still only a DataVolume.
Images have to be adopted first, and `hack/migrate-templates.sh` now refuses a
template whose image is not managed yet, naming the script to run.

`hack/adopt-images.sh` takes ownership of disks that already exist rather than
recreating them. Verified on the stand: two adopted images reported `Ready`
immediately, no import, no new disk, and the migrated template flipped to
`ImageFound: True`.

Its first version had a defect worth recording, because it is the same shape as
several in this codebase's history: it identified golden disks by the *absence*
of our `vm-disk` label — a convention, not a fact — and offered to adopt four
tenant worker root disks as golden images, since the tenant machinery writes no
such label. It now selects on the absence of a `VirtualMachine` ownerReference,
which KubeVirt writes itself and which states what a disk is for. The platform's
own namespace is skipped: the Talos golden there is owned by the tenant path as
a deterministic singleton, and adopting it would give it a second owner.

### What counts as an image: the predicate, and two wrong answers before it

Adoption has to select exactly what the product calls an image — no more, or it
takes over disks nobody asked it to manage; no less, or images stay visible in
the UI and unmanaged.

Two exclusionary attempts, both wrong, both caught by running the plan:

1. *Not carrying our `vm-disk` label.* Offered four tenant worker root disks:
   the tenant machinery creates disks through `dataVolumeTemplates` and writes
   no such label.
2. *Not owned by a VirtualMachine.* Fixed that, and still admitted a hand-made
   claim that had never been through the product at all.

What a thing is cannot be established by what it is not. Measured across every
DataVolume on the stand, there is a positive marker: `kubevirt-ui.io/disk-type`
is written by the image endpoint and by nothing else — absent on every VM root
disk (ours and the tenant machinery's), on the Talos golden, and on a stray
claim; present on every image the UI lists. The ownerReference check stays as a
guard rather than as the definition.

Verified both directions on the stand: the candidate set now equals the set the
product's own lister returns for managed namespaces, and a deliberately created
standalone `DataVolume` carrying `managed=true` but no `disk-type` is not
offered. The Talos golden now falls out on its own merits rather than by the
namespace rule, which stays as a second guard.

---

## M8 (first slice) — lifecycle operations: restore and migrate

Operations are objects with their own state, because the two things they
replace were sequences of sleeps inside an HTTP handler and died with it.

### Research first, and it changed the design twice

Measured against KubeVirt v1.9 and CDI v1.66 on the stand, before writing
anything:

- `VirtualMachineInstanceMigration.spec.addedNodeSelector` exists. It restricts
  where *this migration* may land without touching the machine. So the old
  defect — a nodeSelector welded onto the VM that nothing removes — is not
  fixed by adding a cleanup step; the mechanism that caused it is simply not
  used.
- `VirtualMachineRestore.spec.targetReadinessPolicy: StopTarget` exists. KubeVirt
  stops the target itself, which deletes the entire stop-and-poll dance from our
  side. What is left is the part it does not do: remembering whether the machine
  had been running, and putting it back — the exact part the old code kept in a
  local variable.

### Live, on one machine

| Check | Result |
|---|---|
| Migrate as an operation | worker-3 → worker-1 in 20s; `addedNodeSelector` on the migration object; **VM nodeSelector empty** |
| The same machine through the **old** endpoint | welded to `kubernetes.io/hostname: kubevirt-lab-worker-2`, and nothing removes it |
| Restore as an operation, **operator killed mid-flight** (`--grace-period=0 --force`) | after the restart the operation finished by itself: `Succeeded — restored op-target from op-snap`, `spec.running` put back to `true`, the machine started again |
| The VM controller during an operation | yields; `status.operationInProgress` names it, and is *derived* from unfinished operations rather than written by the other controller, so the status keeps one writer |
| Migrate through the UI endpoint, after rewiring | creates an operation; machine never pinned |

The kill test is the whole point of the type. In the path it replaces, the same
kill left the machine stopped for good, because the only record that it had been
running was a variable in a process that no longer existed.

`hack/unpin-migrated-vms.sh` frees machines the old path pinned — planning by
default, because a nodeSelector is also how a person places a machine
deliberately and nothing here can tell the two apart.

### Still owed on M8

Recreate and disk-snapshot rollback; the declarative disk model
(`spec.disks` plus the one-VM rule in admission); the delete cascade for
schedules and Velero objects; NIC detach leaving `networks[]` litter. And
before clone is rewritten: measure upstream `VirtualMachineClone`, which has its
own state machine and volume-naming policy — porting the current rename-map,
which copies attached PVCs verbatim and so points a clone at the source's disks,
would be rebuilding a defect in a new language.

## M8 (second slice) — disks are declared, and things stop outliving their machine

**Declarative disks.** What is attached lives in `spec.disks`; attach and detach
through the UI patch that list for machines the operator owns. Live on the
stand, on a **running** guest: attach → declared, plugged in, holder label set,
and `volumeStatus` shows `op-data-1=Ready` with no restart; detach → gone from
all three.

The root disk is deliberately not one of these, and that is what makes
declarative disks compatible with restores: a restore replaces the disk behind
the root volume and keeps the *volume's* name. Matching by volume name rather
than by the disk behind it makes a restore invisible to this reconciliation
instead of undone by it.

Two holes closed:

- **A disk attached to two machines** is refused at admission. Live:
  `disk "op-data-1" is already attached to "op-target"; a disk written by two
  machines is corrupted by both`. The old check lived in one handler, so a
  manifest or the hot-plug path on a second machine walked past it.
- **A deleted machine releases its disks.** The attach path reads the holder
  label before it scans, so a disk whose holder no longer existed could never be
  attached to anything again.

A disk plugged in by another route is left alone: the controller detaches only
what it attached, recorded in status.

**Schedules die with their machine.** The link was a name in a label and a name
inside a shell command, so a schedule kept firing kubectl at a deleted VM, and a
new machine with the same name inherited the old one's schedules. It is an
ownerReference now — the cluster's own garbage collector, no controller, no
finalizer, and it still works when nothing of ours is running. Not blocking, so
a machine never waits on its schedules; not set across namespaces, where the
collector would read it as a missing owner and delete the schedule at once.

### The disk race, and why admission was not enough

Admission refuses a disk another machine already declares — but admission is a
preflight. Two requests racing each read a world in which the other has not
landed, both pass, and both attach. Two machines writing one filesystem corrupt
it, and neither finds out.

So the decision is made on the disk itself. The controller **claims** before it
attaches: a compare-and-set on the holder label, carrying the resourceVersion it
read, so the API server rejects the second writer rather than luck deciding. The
claim is pinned to the object, not to its name — a second label records the
machine's UID, because a name is reusable and a claim must not transfer to
whatever is called that next.

Releasing is refused unless it comes from the holder. That was a real bug on the
way here: the release path checked the name only when *taking* a label, not when
clearing one, so a machine that had merely listed a disk could free somebody
else's on its way out — producing exactly the double attachment the claim
exists to prevent.

Three tests, none of which need the webhook (it is deliberately absent from the
suite, which is the situation being tested):

- two machines racing for one disk: exactly one attaches, the other reports
  `DiskHeldByAnotherMachine` naming the holder, and **stays** refused rather
  than taking it a moment later;
- a machine that never held a disk cannot free it by being deleted;
- a release arriving *after* the disk has been claimed by another object leaves
  that claim alone — the case where comparing names would have been wrong.

### And a guard defect found by the clone research

`VirtualMachineClone` is installed here (v1beta1, with `volumeNamePolicy`, new
MAC addresses and SMBios serial), so clone should delegate to it rather than
port the hand-rolled rename-map — which copies attached claims verbatim and so
points a clone at the *source's* disks.

Measuring that turned up something more urgent: the guard **was refusing the
clone controller**. `system:serviceaccount:o0-kubevirt:kubevirt-controller`
creates the target of a clone and was not on the allowlist, so cloning was
broken by a guard added two slices earlier. Fixed, and the lesson recorded in
the allowlist's own comment: a list assembled from memory will always be missing
one, which is why refusals are logged — the next missing identity should arrive
as a log line rather than as a mystery.

Also worth writing down, because it wasted a measurement: the first attempt to
deploy that fix ran `kubectl apply … >/dev/null 2>&1`, so the apply's failure
was invisible and the "still refused" reading looked like a code problem. Prove
the change arrived — and do not silence the command that would tell you.

#### Proving the claim is a compare-and-set, not a hope

"Exactly one machine ends up with the disk" is not the same claim as "the write
is atomic": with one worker per controller the reconciles are serialised, so the
loser usually loses on the *read*, and a test that only checks the outcome would
pass just as well if the write were last-writer-wins.

So the conflict branch is forced. A test client hands one claimant a snapshot
taken before the other's claim landed, which puts a stale resourceVersion on its
write. It must come back as a lost race, and the disk must still name the first
holder with the first holder's UID.

The test was then checked for being vacuous by breaking the production code, and
two attempts were **inconclusive** before one worked — worth recording, because
each looked like a valid mutation:

- clearing `resourceVersion` before the update: rejected outright by the API
  server (`must be specified for an update`), so it never became a
  last-writer-wins write at all;
- switching to `client.Merge`: still carries the resourceVersion in the patch
  body, so the API server still treats it as a precondition;
- a labels-only raw merge patch, which carries no version: this one **made the
  test fail** with "a stale claimant succeeded; the write is not version-checked".

Only the third mutation proves anything. A comment on the update in
`claimDisk` now records why it is an Update and not a patch.

## M8 (third slice) — rolling a disk back without destroying it first

The path this replaces stopped the machine, **deleted** the claim and its
DataVolume, and created the replacement afterwards. A process that died in
between left a machine with no disk and nothing anywhere that knew what it
should have had. Its delete-wait loops also read any API error as "gone"
(`except ApiException: break`), so a transient failure looked like success.

The operation inverts the order, which is the entire design: build the
replacement from the snapshot, point the machine at it, remove the old one last.
Every failure before the swap leaves the machine on the disk it started with;
every failure after it leaves the machine on the new one. Neither leaves it with
nothing. CDI can build a DataVolume straight from a VolumeSnapshot
(`spec.source.snapshot`, checked against the CRD), so no staging copy is needed.

The names of both disks are recorded on the operation before either is touched,
so a pass resuming after a crash knows which swap it was in the middle of rather
than starting again and building a second replacement.

**A machine's own root disk is refused**, with the alternative named: the
machine is built from that disk, and swapping a claim would leave its own
template describing something that no longer exists. A VirtualMachineSnapshot
and a Restore are the tools for that, and the Restore operation already exists.
This is a deliberate narrowing of what the old endpoint accepted — the case it
refuses is the one where the old code was most dangerous.

## M8 (fourth slice) — recreate and clone

**Recreate** is destructive by intent: it wipes a machine back to the image it
was built from. What it must not be is destructive by accident. The machine's
own template is pointed at a fresh disk name first, so KubeVirt builds a new
disk, and only then is the old one removed — where the old path deleted the disk
and relied on KubeVirt noticing and rebuilding it under the *same* name, leaving
a window with no disk at all and a name that had to be reused while its
predecessor might still be terminating.

The fresh name uses the epoch the naming rule was designed around
(`<vm>-root-<n+1>`), and the epoch is recorded on the machine so the next
rebuild picks the next one. This is one of the two places allowed to rewrite a
machine's own volume arrays; it is safe here for the same reason the rule
exists — the VM controller stands aside for the whole operation, so there is
exactly one writer.

**Clone** delegates to KubeVirt's own `VirtualMachineClone`, which is installed
here and handles volume naming, a fresh MAC and a fresh SMBIOS serial. The path
it replaces built the copy by hand with a rename map covering only
`dataVolumeTemplates`, so an *attached* claim was copied verbatim and the clone
referenced the source's own disks — two machines on one filesystem, produced
deliberately by the feature meant to give you a separate one. The copy is then
described and adopted, so it is a managed machine rather than a KubeVirt object
nobody owns; its spec is the source's, minus the attached disks and minus the
first-boot password, both of which belonged to the original.

### And a defect in my own wiring, caught by existing tests

Routing these endpoints added an ownership probe before the handler's `try`, so
a failing probe escaped instead of becoming the handler's error — two clone
authorization tests went red. The fix is not to make the probe quiet: falling
through on a read that failed would send an operator-owned machine down the old,
destructive path on the strength of a question nobody managed to answer. The
probe now reports the failure, which is both what the tests assert and what
production should do.

## M8 (fifth slice) — the network list stops filling with dead entries

Detaching a NIC marks the interface `state: absent`, which is how KubeVirt is
asked to unplug it, and nothing ever removed the matching entry from `networks`.
The litter accumulates, and because a network name must be unique, attaching a
NIC with the same name again is refused — the machine still lists a network for
an interface that has been gone for months.

The sweep removes only what is provably dead, and never adds or reorders
anything: a network with no interface at all, or an interface marked absent
whose unplug has finished. An interface marked absent on a machine that still
reports it is mid-unplug and left alone — taking the entries away then would
withdraw the request before KubeVirt has acted on it. Both cases are tests.

With this the VM track's own defects are closed. What the plan still lists for
later — a fully declarative `spec.networks` with hot-plug through it — is a
feature, not a defect: today's NIC attach remains imperative and now cleans up
after itself.

---

## M11 — BGP announcements (controller done; cutover is one deliberate step)

The logic is ported as it stands, including the three raw lines that each fail
silently when missing and the datapath predicate that decides who may be
advertised. What changes is where it runs: a pass every thirty seconds inside
the UI backend becomes a controller woken by what it depends on. Its
configuration becomes an object, because these values are properties of an
addressing plan and one of them going unset is how the feature once did nothing
on a whole stand without saying so.

### The comparison, on the live cluster

The stand's backend writes `kubevirt-ui-b3` every thirty seconds, so the
controller ran in dry-run: everything collected, nodes chosen, configuration
rendered, **nothing written**.

```
$ diff backend.conf operator.conf
IDENTICAL — the handover would be a no-op
backend:  ["kubevirt-lab-worker-1","kubevirt-lab-worker-2"]
operator: ["kubevirt-lab-worker-1","kubevirt-lab-worker-2"]
```

Four prefixes, each with its own router leg as the next hop, and the same two
workers carrying them.

**The cutover is two phases, in one safe order, and is not taken here** because
it touches live BGP on the stand. There is no atomic step available: the writer
flag lives on a Deployment and dry-run lives on a custom resource, so it is two
API calls and two rollouts.

| phase | change | writers |
|---|---|---|
| 1 | `OPERATOR_ANNOUNCE_ENABLED` on the backend | 1 → **0** |
| 2 | `dryRun: false` on the policy | 0 → 1 |

A window with no writer is harmless: the FRRConfiguration stays exactly as it
is, frr-k8s keeps applying it, and the dataplane does not move. A window with
two is two `router bgp` blocks over one session. Rollback is the same order
backwards, for the same reason.

`hack/announce-cutover.sh` walks it and **refuses the wrong order**. It will not
flip the backend itself — that is a change to a released deployment — and its
`status` prints the byte-for-byte comparison, so the decision is made on
evidence rather than on this document.

#### The procedure was rehearsed, not just written

The whole cycle was run against a stand-in: an FRRConfiguration identical to the
live one, in a namespace frr-k8s does not watch, with a probe Deployment
standing in for the backend. Nothing on the real BGP path was touched.

| step | result |
|---|---|
| writer flag unset | `backend writer: ON` — phase 2 refused |
| flag set to `false` | `ON` — refused |
| flag set, **rollout still in progress** | `ON` — refused. The check reads the *running pods*, not the Deployment: a flag in a spec is an intention, and a pod that has not been replaced is still writing |
| flag set, rollout complete | `off` — phase 2 allowed |
| takeover | `unchanged — the handover moved ownership and nothing else` |
| ownership while owned | the object was tampered with (`router bgp 1`) and the operator put it back |
| ownership after stepping back | the **same** tamper was made again with `dryRun: true`, and it was still `router bgp 1` forty-five seconds later — the operator really had let go |
| backend back on | only after that, and the writer state read `ON` again |
| teardown | the real object never took part, and the dry-run render still matches it byte for byte |

The negative half is the one that matters for a rollback. "The operator stopped
writing" and "nothing happened to be writing" look identical from outside until
something perturbs the object: only a perturbation that is *not* repaired proves
the first. Turning the backend back on before that check is how a rollback
becomes two writers.

Two defects in the script itself came out of running it. The first version read
the flag off the Deployment spec, so it would have let phase 2 start mid-rollout
— the exact two-writer window the ordering exists to prevent. The second had
`local` outside a function, so `phase2 --apply` printed its checks and then
silently did nothing. Neither would have been visible by reading it.

### Why "shadow" was the wrong idea, corrected before it shipped

The first plan was to point the controller at a second FRRConfiguration name and
compare. That would not have been a comparison: frr-k8s merges every
configuration in its namespace into the node's FRR, so the shadow would have
been applied to the dataplane beside the real one — two `router bgp` blocks over
one session, on a live stand.

### Running the network domain separately, and what that turned up

Two defects, both invisible from the outside:

1. **Both deployments shared a selector.** The second one inherited the base's
   `control-plane: controller-manager`, so the two Deployments formally owned
   each other's pods. The symptom was misleading — `kubectl logs deploy/network…`
   printed the *VM* controllers, which looked like the `--domains` argument not
   arriving, when the arguments were right and the selector was wrong.
2. **The second account could not take its lease.** The process started,
   reported `Domains enabled: network`, and then retried
   `cannot get resource leases` forever — running no controllers at all while
   looking healthy.

Verified after fixing, each separately: selectors differ; pod labels match their
own selector; both roll out; one lease each with distinct holders; the webhook
and metrics services still resolve to the **vm** pod only and neither lost its
endpoint; admission still refuses a raw VM; and each profile starts only its own
controllers — vm: Image, Template, VM, Operation; network: AnnouncementPolicy.

The service check was worth making rather than assuming: the selector labels
propagate into Service selectors too, and had they not, half the admission
traffic would have gone to a pod that serves no webhooks — silently, under
`failurePolicy: Ignore`.

## M13b — ManagedUnderlay

The fabric a VPC egress gateway needs (ProviderNetwork, Vlan, NAD, Subnet) plus
the gateway node label and the two workaround DaemonSets, moved from
`vpc_underlay.py` to a controller.

### Why it is a controller

The `ovn.kubernetes.io/external-gw` label was healed by the GET handler. It came
back when somebody opened the page, and not otherwise. On this lab it sat at an
explicit `false` on all three workers with nothing in managedFields claiming it;
the link-watcher DaemonSet selects on it, so it was scheduled nowhere, and
`kubectl rollout status` reported success because zero desired pods are all
ready. Two hours later both provider links were down and had taken the transit
and egress planes with them.

### Adoption on the live stand

Two underlays already existed, applied by hand from the lab's `vpc-bgp/`
manifests: `cptransit`/eth0.300 and `extnet`/eth0.310. They were written down as
CRs — every value read back from the live objects — and adopted.

Full before/after diff of the nine objects: **one** difference, the NAD config
JSON re-serialised (Go sorts keys and omits spaces; Python kept insertion order).
Same four fields, same values. Everything else, including both DaemonSet pod
templates and the link-watcher script, was byte-identical — the Go port and the
Python builder produce the same fabric.

### Ordering, and the blocker that caught me

I turned the operator on while the backend was still a writer. That is the same
two-writer condition as the announcements, and measuring idempotence does not
remove it. Corrected by pausing both CRs immediately, then doing it in order:

  phase 1 — `OPERATOR_UNDERLAY_ENABLED=true` on both backends. Writers 1 -> 0.
  phase 2 — unpause.                                            Writers 0 -> 1.

`hack/underlay-cutover.sh` walks it and refuses phase 2 while any running
backend pod lacks the flag — asked of the pods, not the Deployment spec.
Verified: it refused before phase 1 (exit 2) and passed after.

Both backends, because there are two against this cluster: the in-cluster
Deployment (moved from the released 2026.10.36 to `dev-a3eaa55`) and the local
compose harness. The script only knows about the first; the second is named here
rather than left implicit.

### The A/B, on the live cluster

- paused, label set to `false` on worker-3: **90 seconds, nothing happened.**
  Which is also a faithful reproduction of the original defect — the DaemonSet
  quietly went to 2/3 and no object said so.
- active, label removed entirely from worker-2: **restored in 5 seconds**,
  counted in `status.labelHeals`, DaemonSet never left 3/3. No GET involved.

### Two defects found by running it

1. **A paused underlay read as a healthy one.** GET reported the fabric ready
   and "3 node(s) carry the provider NIC" while one of them was at `false`,
   because a paused controller keeps its last verdict and a frozen status looks
   exactly like a current one. Same shape as the 0/0 DaemonSet. Paused now
   answers not-ready and names the annotation.

2. **50 invisible writes.** `patches_total` showed 50 DaemonSet updates against
   2 for every other kind, while `resourceVersion` and `generation` sat
   perfectly still. Assigning the whole pod template discards the eight fields
   the API server defaults; the rendered template never equals the stored one,
   so an Update goes out every pass, the API server re-applies the defaults, and
   the object comes back byte-identical. Nothing at the object level can see it.
   Fixed by merging only the fields this controller renders. Guarded by a test
   that pokes the CR five times and asserts the counter does not move; reverting
   to the assignment fails it and nothing else.

   Worth keeping as a rule: `resourceVersion` stability is not proof of
   write-on-diff. The counter is.

   Verified on the stand after the fix: six reconciles across two resync
   cycles, `kubevirt_ui_operator_patches_total{controller="managedunderlay"}`
   absent entirely — not one write of any kind.

### Deliberate design notes

- The heal is add-only. A node dropping out of `readyNodes` is far more often
  kube-ovn briefly unable to report than the NIC being gone.
- Only rendered keys are merged into a live spec — kube-ovn writes its own
  defaults into its own objects (`enableLb`, `gatewayType`, `u2oFeatures`, `vpc`
  and more were all present on the live subnets and all survived adoption).
- The two DaemonSets are cluster singletons shared by every underlay, owned
  non-controller by each one that wants them, and never deleted when a flag goes
  false. Verified on the stand: both carry two ownerReferences, neither marked
  controller.
- Deleting a ManagedUnderlay takes its external Subnet and every gateway on it.
  Stated in the type doc, the values comment and the cutover script header. The
  rollback path pauses; it never deletes.

### A third defect, found by the advisor pushing back on the rollout order

The paused-status fix was committed *after* the cluster backend had been rolled
to `dev-a3eaa55`, and the later deploys only moved the operator tag. So the live
backend was still the stale-status build while the branch was fixed. Rolled to
`dev-77bb000`, digest checked on the running pod, and verified behaviourally on
that pod against the real CRs: `external` paused -> ready=False with the frozen
-status message, `cp-transit` unpaused -> ready=True, then the annotation removed
and the same pod flipped to ready=True. Same pod, one annotation, both answers.

Doing that verification turned up something considerably worse.

**The chart granted the backend nothing on `platform.kubevirt-ui.io`** — not for
underlays, and not for the four operator paths written before this one. The
deployed pod answered 403 reading a ManagedUnderlay. Nothing could see it: the
development backend reaches the cluster through an admin kubeconfig, so every
RBAC-gated path passes in the lab by construction.

There was already a contract test for exactly this class (`test_helm_rbac_
contract.py`, written after three shipped features 403'd at once) with a
hand-kept `REQUIRED` list and a "add a row when you add a call" convention.
Nobody added the rows. So the fix is in that file rather than beside it:

- the fifteen operator rows, and
- `test_every_operator_call_site_is_listed`, which reads the
  `*_custom_object(plural=...)` call sites out of the source — literals and
  module constants — and fails when `REQUIRED` falls behind them.

Both ends now fail on their own: deleting the chart rule fails
`test_chart_grants_permission[...managedunderlays:patch]`, deleting the REQUIRED
row fails the new scan, and a scan that matched nothing fails its own guard.

Applied to the stand: the backend SA can now get/create/patch managedunderlays,
and deliberately **not** delete one — given the cascade, the backend has no
business being able to.

### Left open

- `status.labelHeals` is per-underlay, so on a shared node the first underlay to
  reconcile takes the credit and the other records nothing. Accurate as "how
  often did *this* underlay have to fix it", which is what the number is for.
- The in-cluster backend now runs a branch image. Putting the stand back is
  `kubectl set image ... backend=ghcr.io/mrybas/kubevirt-ui/backend:2026.10.36`
  plus `hack/underlay-cutover.sh rollback --apply`, in that order reversed.

## M10a (first slice) — ManagedNetwork: the VPC and its default subnet

One declaration produces the kube-ovn `Vpc` and the `<name>-default` `Subnet`:
labels the tenant wizard filters on, DHCP options workloads resolve through, and
the external-plane attachment with its default route.

### Where the slice deliberately stops

**No ACLs.** `Subnet.spec.acls` has one writer — the isolation reconciler in the
backend — and the composer that takes it over needs an adoption step that proves
render == live first. The `isolated` flag here records the decision (it stamps
the opt-out annotation the reconciler reads) and writes no rules. Guarded by a
test that plants somebody else's ACL and pokes the controller five times.

**Lists are merged, not set.** Peering writes `staticRoutes`, the
egress-gateway attach path appends to `extraExternalSubnets`. Replacing either
would delete another writer's work on the first pass. The limitation this buys:
removing an entry from the CR does not remove it from the VPC. Closes when those
paths move here.

**No ownerReferences**, and this is the opposite of ManagedUnderlay's choice.
A network is usually written down after it already exists and already carries
workloads, so describing one must be reversible in both directions. Recorded in
the type doc, next to the underlay's opposite reasoning.

### Three write-loops caught before they reached a cluster

The acceptance is parity with the two networks the stand already has, using the
live objects as fixtures rather than hand-written expectations. It failed
immediately, three times, all the same class as the DaemonSet one:

1. kube-ovn defaults `bfdId`, `ecmpMode`, `routeTable` inside every static route.
   Nothing in this product sets them, so an exact comparison never matches and
   the list is rewritten every pass. → merge on the fields we own.
2. kube-ovn drops an empty `namespaces` instead of storing it, so rendering `[]`
   unconditionally never matches. → write it only when non-empty.
3. (envtest) `SetAnnotations` with an empty map on an object that had none counts
   as a change, and the API server stores nil again — one extra write per
   reconcile on every isolated network. → touch annotations only when they differ.

### Live cycle

- **Adoption of an existing UI-built network is a byte-identical no-op.**
  `uat-net-vm`: resourceVersion and generation unchanged on both objects, full
  JSON diff empty, conditions Ready/Attached true, `Via 10.199.4.254` read from
  the external Subnet.
- **A CRD-created network equals a UI-created one.** `opnet1` (CRD) against
  `uinet1` (POST /api/v1/vpcs), names and addresses normalised: Vpc identical,
  Subnet identical apart from `acls`, which the other writer owns.

### Two mistakes worth keeping

**I adopted a live network while the backend still owned the create path.** Same
two-writer condition as the underlay, one slice later. Corrected by removing the
CR — safe here precisely because this controller sets no ownerReferences, and
verified as such: `uat-net-vm` still at rv=170864, no deletionTimestamp, VMs
running. Comparisons now run on throwaway networks only.

**The first comparison was against a differently configured product.** It
reported the UI writing a VPC with no external plane at all. The cause was mine:
`-f docker-compose.yml -f docker-compose.opdev.yml` does not load
`docker-compose.override.yml`, so the harness had no `B3_*` variables and
`b3_enabled()` was false. The B3 configuration now lives in the opdev harness,
copied from the in-cluster Deployment — the authority on what this site's
product configuration is.

And the reason that got noticed at all: the normalisation `sed` failed on the
slashes in a CIDR, wrote two empty files, and `diff` called them identical. A
comparison that passes on empty inputs is not a comparison. Byte counts are now
printed alongside every diff in this log's live checks.

### Dataplane proof, not just object proof

`opnet1-default` has a real logical switch in OVN
(`ovn-nbctl ls-list` → `4787eab0… (opnet1-default)`), alongside the UI-built
`uat-net-vm-default`. The CR conditions alone would not have shown this: the
subnet that turned out to have **no** logical switch also reported
`Ready=True, reason=ResetLogicalSwitchAclSuccess`.

### A trap of my own making — the evidence, and what it does not say

Deleting a test network and recreating it under the same name left the second
one with no logical switch, and its Subnet then sat in Terminating until the
finalizer was removed by hand. The UI's honest 409 says "retry in a moment", and
the moment never came.

I first wrote that up as "name reuse" from inference. Pulled the controller logs
instead. What they actually show, in order:

```
08:53:18  ipam.go:347   adding new subnet uinet1-default        ← UID f06b1840…
08:53:19  ipam.go:490   recorded gateway MAC … for subnet uinet1-default
08:53:19  ipam.go:61    allocate 10.200.20.2 … vpc-dns-uinet1-dns-…   (switch exists)
08:55:41  ipam.go:355   delete subnet uinet1-default            ← first delete
08:57:15  pod.go:751    get logical switch uinet1-default …: not found
                        logical switch "uinet1-default"          ← second incarnation
09:05:06  ipam.go:355   delete subnet uinet1-default            ← my finalizer removal
```

and two distinct UIDs for one name:

```
Name:"uinet1-default", UID:"f06b1840-96dd-4ab1-843b-d2225c49ee3e"
Name:"uinet1-default", UID:"30d10794-7834-480e-ad56-1cd752eca45b"
```

So the name was reused across a delete that was still in flight — that part is
direct. `adding new subnet` appears exactly once, for the first UID: the second
incarnation never had a logical switch built for it at all. The internal
mechanism inside kube-ovn is **not** established, and this log does not claim
one.

Two hypotheses were tested and are wrong, which is worth as much as the finding:

* *"The Vpc is holding it."* Deleted the Vpc directly; the Subnet did not budge.
* *"The VPC was never standby."* The logs are full of
  `the vpc 'uinet1' not standby yet, requeuing` — and `opnet1`, the network that
  worked, hit the same error **twelve times** on its way up. It is normal
  transient noise at create, not a cause. An error message that appears in both
  the broken and the working case explains neither.

Rule for the live cycles from here: **never reuse a network name.**

## M10a (second slice) — VPC DNS, and the route that kept going missing

The network gets its own resolver, and — the part worth the work — the
service-network route on that resolver's pod template is applied on a watch
rather than once at create time.

A VpcDns pod's secondary interface gets exactly one route into the cluster
overlay, written by kube-ovn and not configurable, so the cluster resolver's
ClusterIP is unreachable: the packet takes the pod's default route out into the
tenant network and dies. Routing the whole service network is what removed the
need to pin resolver *pod* addresses in the Corefile — and pinned pod addresses
is how VPC DNS once went silent after a CoreDNS restart while the VpcDns object
reported ACTIVE with both pods Running.

The old path could only try once: kube-ovn creates that Deployment *after* the
VpcDns object, so at create time there is nothing to annotate. The code said the
route would go on "at the next reconcile", and the only next reconcile was a
person calling an endpoint.

### Facts read, not configured

| fact | source |
|---|---|
| resolver address | `vpc-dns-config`.`coredns-vip` — the ConfigMap that already configures kube-ovn's own VpcDns controller |
| overlay gateway | `Subnet/ovn-default`.`spec.gateway` |
| service network | kubeadm ConfigMap, else the apiserver's `--service-cluster-ip-range` |

Measured on this stand: no kubeadm ConfigMap (Talos), and
`--service-cluster-ip-range=10.96.0.0/12` on the apiserver pods.

The last one can genuinely be unavailable — a managed control plane exposes
neither — so there are two ways to state it instead: `--service-cidr` on the
operator (a cluster fact belongs on the operator, not repeated per network) and
`spec.serviceCIDR` on one network. With none of the three, DNSReady goes false
naming all of them, rather than leaving DNS quietly unrouted. The lookup uses
the uncached reader: asked once per process, against an alternative of an
informer over every Pod and ConfigMap in kube-system.

### Mutations

Three, all of which fail the tests:

1. accept any existing annotation instead of the right one — the dangerous
   shape, because a route that is present and wrong satisfies every presence
   check while the packets still go nowhere;
2. drop the "managed control plane" half of the refusal;
3. disable the Deployment watch — fails at the first step, because then nothing
   applies the route at all. That one is the proof that the watch is doing the
   work.

### Live

`opnet2` (a new name — see the rule above): DNSReady true in 6 s,
`status.dnsServer 10.96.0.200` resolved from the ConfigMap,
`status.serviceRoute 10.96.0.0/12 via 10.16.0.1`, all three conditions true.
Operator permissions checked on the stand with `auth can-i` as its own service
account: list pods in kube-system, get configmaps, patch deployments, create
vpc-dnses — all yes.

### Housekeeping, from an audit of this session's commits

Every commit so far used `git add -A`. Auditing the file lists found one thing
that should not have shipped: `config/samples/lab-underlays.yaml` was written
with the repo root as the working directory when `operator/config/samples/` was
meant, creating a stray top-level `config/` directory. Moved. Six other files
carried whitespace-only `gofmt` changes swept in after `make test` ran
`go fmt ./...` — checked, harmless, left alone. Paths are named explicitly from
here.

## M10a (third slice) — deletion, opt-in, with the router going last

`spec.deletionPolicy` decides what deleting the object means, and defaults to
`Retain`.

That default is not caution for its own sake: it is the property that already
mattered. With `Retain` there is no finalizer and no ownership, so a network
written down after it already exists and already carries workloads can be
described and un-described freely. When an adoption CR had to be withdrawn from
a live network on this stand, that is what made it a non-event.

`Delete` opts into the cascade, in the order kube-ovn requires — resolver,
subnets, then the router — and the router only once the subnets are *read back*
as gone. Every subnet is finalized against that router; remove it first and they
are stranded permanently, with kube-ovn looping on `not found logical router`
and the finalizer never coming off.

While it waits it says what it waits for and comes back in five seconds. The
endpoint this replaces answered 409 with the same list and the words "retry in a
moment" — and this session watched exactly that happen: nobody retried, the
moment never came, and the subnet had to be freed by hand.

### Mutations

- delete the router without waiting for the subnets (the measured damage) →
  fails;
- take the finalizer unconditionally → fails, because it turns every description
  into an owner.

### Live

| check | result |
|---|---|
| `Retain`, CR deleted | Vpc `rv=665032` before and after — untouched; Subnet still there |
| `Delete`, fresh network `opnet3` | finalizer claimed; after `kubectl delete`: 5 s draining, gone at 10 s |
| stranded objects afterwards | none — no orphan Subnet, no orphan VpcDns |
| adopt-then-cascade on `opnet1` | adopted with `Delete`, removed cleanly in 5 s |

The contrast with the stuck `uinet1` earlier in this log is the point: same
cluster, same kube-ovn, and the difference is that the router outlived its
subnets.

## M10b — the ACL composer

Decision written first, in `docs/acl-composition.md`: deny the tenant supernet
once and carve the exceptions above it, instead of enumerating every other
tenant. The enumeration is `2·(N−1)` rows per subnet and `2·N·(N−1)` across the
cluster — about 320 000 rows and 400 read-modify-writes per create at the scale
this is aimed at. Not address sets: kube-ovn exposes none, and a rule set that
does not enumerate its peers has nothing to hoist.

### The evaluator, and what it found

Replacing five enumerated drops with one aggregate rewrites every line, so
comparing lines proves nothing. The acceptance is an evaluator — highest
priority wins, no match means allowed — comparing the two sets by what they do.

It immediately showed the property does not hold both ways, and should not.
Against the stand's own live list, two probes are **allowed** under the
enumeration and **denied** under the aggregate:

- `10.200.24.9` — a network created since the last isolation pass;
- `10.203.255.1` — an address inside the supernet nobody has been given yet.

That is the hole the enumeration has by construction. So the assertion is
one-sided: nothing denied may become allowed, and anything newly denied must be
inside the tenant aggregate — never the internet.

The evaluator then caught a bug in itself. A mutation putting the peering allow
at the same priority as the drop failed nothing, because the evaluator resolved
the tie by sort order. OVN does not; it picks one, unspecified. Equal-priority
allow-and-drop is now `Conflicted` and an invariant says no rendered set
produces it.

### Three corrections on the way to durable isolation

Each was wrong for a reason worth keeping:

1. **wait for the subnet, then isolate** — waits expire;
2. **check afterwards and undo** — if the process dies there is nobody left to
   check;
3. **let the operator converge** — durable, but the subnet was created open and
   closed a moment later, and kube-ovn realises a subnet the instant it exists.

The rules now ship in the Subnet's create payload.

### Live

| check | result |
|---|---|
| operator-created network | first `ADDED` event: `generation 1, acls 10, owner=operator` — the subnet never existed open |
| the rules | six mgmt `/32`s from the real nodes, two own-subnet allows, one aggregate floor per direction |
| a UI-built list (22 rules, enumerated) | **not adopted**; the condition names the 14 rules the composer cannot reproduce; list unchanged, ownership unclaimed |

`generation` alone was not enough live evidence: a static read showed 2, because
kube-ovn writes its own defaults into the spec a moment after creation. The
watch from the moment of creation is what actually settles it, and envtest —
where nothing else writes the object — keeps the `generation == 1` assertion.

### An open product defect, not mine

Deleting a VPC through the UI leaves its subnet in Terminating on kube-ovn's
finalizer, indefinitely. Reproduced twice, on `uinet1` and on `uinet2` — the
second with a name never used before, which retires the "name reuse" theory from
the M10a notes entirely.

What is measured: the delete is enqueued once, the pod addresses are released,
and after that the controller never mentions the subnet again. No events after
creation. Nothing references it. Deleting the Vpc afterwards does not release it
— tested on both. Freed by hand each time.

The 409 the endpoint returns says "retry in a moment", and the moment does not
come. The operator's cascade did not reproduce it in three teardowns
(`opnet1`, `opnet3`, and `opnet2/4/5`), which is evidence but not an
explanation; the difference between the two paths has not been established and
this log does not claim one.

### Chasing it, and what the experiments actually settled

Four controlled runs, because "the operator path is fine" resting on three
lucky teardowns is not an acceptance:

| experiment | setup | result |
|---|---|---|
| A | created by the UI, torn down by the **operator** | clean, 5 s |
| B | created and torn down by the **UI**, 20 s old | clean |
| C | same, 100 s old, DNS pods **2/2 Running** | clean |
| D | created by the UI, **a ManagedNetwork describing it**, torn down by the UI | **found two bugs of mine** |

A rules out the create path. B and C rule out the age of the network and the
state of its DNS pods — the hypothesis I had been carrying.

D found something better than the thing I was looking for:

1. **The delete endpoint lied.** It handed the teardown to the operator the
   moment a ManagedNetwork existed. A `Retain` object describes a network it
   does not own, so deleting it removed the description and nothing else — the
   endpoint answered "VPC 'xd1' is being removed" and the Vpc and Subnet were
   still there a minute later. It now hands over only to an object that will
   cascade.
2. **And then it must wait.** The operator keeps reconciling until it observes
   the deletion, and its writes are `CreateOrUpdate`: a subnet the legacy path
   deletes can be kept alive, or recreated, by a controller still working from a
   description that no longer exists.
3. **The controller stands down too**, which is the same hazard from the other
   side and does not rely on anybody waiting: an object carrying a
   deletionTimestamp is not written to, and the test proves the subnet keeps
   both its timestamp and its UID.

D re-run against the fixes: `status: deleted`, and all three objects gone.

### The acceptance, repeated rather than observed

| path | runs | clean |
|---|---|---|
| operator cascade | 3 | 3 |
| UI delete | 3 | 3 |

Six end-to-end deletions with no manual finalizer removal and no hand-deleted
Vpc, after the fixes.

The two original wedges remain unexplained as such. Both happened before those
fixes; one had a ManagedNetwork in play, which fix 2 and 3 address, and the
other reused a name, where the two-UIDs evidence stands. Neither is claimed as
the cause of the other.

## M10c — ManagedNetworkPeering

One object, both ends. A peering is half an entry in each of two foreign specs
plus a point-to-point link, so two objects would be two things that have to
agree — the shape that produces peerings written on one side only.

### What the live cycle found that the tests could not

**The policy priority was below the thing it has to beat.** 29000, where the
egress gateway's catch-all reroute sits at 29100. Every unit test passed,
because they all compared the renderer against its own constant. What caught it
was rendering a peering both ways on the stand — one through the UI, one through
the CRD — and diffing the two routers with names, addresses and prefixes
normalised: one line. The product writes 31001. The test now checks the number
against 29100 rather than against itself.

**And then the peering routed nothing.** Both ends written, `Established` true,
the normalised diff identical — and ping failing in both directions. Isolation
still dropped the peer's prefix, and lifting it belongs to whatever owns those
lists. That is the failure the product has already shipped once: peered on both
routers, reporting Active, carrying nothing.

### Four corrections, each from the previous one being not quite enough

1. **Report it.** A second condition that evaluates each side's rule list the
   way OVN does, against an address from the other side's range — not "did
   something write an allow" but "would a packet get through".
2. **Do not create it.** Reporting a routed black hole is better than hiding
   one and still worse than not making one. If a side's rules drop the peer and
   that list is not the composer's, the allow is never coming: refuse, write
   nothing.
3. **Order it.** A composer-owned drop lifts itself, but "a few seconds,
   fail-closed" is still an avoidable interval. It was only unavoidable because
   the composer derived allows from `Vpc.spec.vpcPeerings` — the peering waiting
   for the allow, the allow waiting for the peering entry. The composer now
   reads the *declaration*, so the allow goes in first and the routes follow.
4. **Do not trust the declaration.** A CR is something anybody who can create an
   object can write. Trusting the spec would let one naming two networks open an
   allow between them with no route ever laid — a hole in the isolation with
   nothing going through it. The peering controller publishes `Accepted` after
   checking both endpoints, and the composer honours only that.

### The ordering claim that was false

I wrote that the finalizer gave route-first, allow-last for free. It did not:
the composer skipped any peering carrying a deletionTimestamp, so the allow came
off the moment the object was *marked*, while the finalizer was still pulling
routes off the routers. The same black hole from the other end.

Found by trying to *observe* the ordering on the stand instead of asserting it.
The teardown was faster than the sampling could resolve, and looking at why led
back to the code. A peering being deleted now counts until the object is
actually gone.

### Live, at `dev-78eff7f`

| check | result |
|---|---|
| normalised diff, UI peering vs CRD peering | identical on both routers |
| two ManagedNetworks, isolated | ping fails both ways |
| + a ManagedNetworkPeering | Accepted / Established / Traffic all true in 4 s, **ping works both ways** |
| control, unpeered network | still unreachable |
| the legacy-ACL pair from earlier | now `Accepted=False`, legs rolled back — the routed black hole is refused |
| delete the peering | routes gone, allow gone, ping fails again |

### A third stranded subnet, and this one has a cause

Cleaning up, four test subnets stuck in Terminating — and they were exactly the
four deleted with `kubectl delete vpc` and `kubectl delete subnet` at the same
moment. That is precisely what the drain in both the endpoint and the operator
exists to prevent: the router has to outlive everything finalized against it.
The controller's own cascade deleted `crdnet1`/`crdnet2` cleanly in the same
minute.

Same silent signature as the earlier two: the delete is processed once, the pod
addresses are released, and the controller never mentions the subnet again. No
`not found logical router` in the log this time either. The mechanism inside
kube-ovn is still not established; what is established is one way to provoke it.

## M13a — deliberately deferred, with the reason

The plan puts `ManagedEgressGateway` next. Checked before starting, and it is
the wrong next thing:

- there is no egress gateway on the stand — no `VpcEgressGateway`, no `egw-*`
  VPC, no transit allocator ConfigMap — so there is nothing to compare a port
  against, and this migration's whole method is comparison;
- the reference UAT run does not exercise it. Its egress is routed: "Ізольований
  VPC із маршрутизованим egress випускає назовні". The gateway phase, C10, is
  marked blocked and was not run;
- routed egress is what M10a already builds, through `externalPlane`.

So M13a would port a hub that nothing on this stand uses, cannot be checked
against anything working, and is superseded by a path already migrated. Recorded
rather than skipped quietly; the two repairs it names — the GET-time heals and
the un-forced VEG rollout — stay worth doing if the hub survives.

## M12a — ManagedTenant, the catalogue and the reservation

### Ports whose numbers came from the product

The quota test's expected values were read off the running backend for four
requests, not derived here. A port that agrees with my reading of the formula
and disagrees with the formula would pass a test written the other way. It
matched first try, all three quantities, all four cases.

The refusal strings are byte-identical to `resolve_talos_release`:

```
Talos 1.13.8 does not support Kubernetes v1.30.1 (it takes 1.31-1.36). Compatible pairs: Talos 1.13.8 -> Kubernetes 1.31-1.36.
Talos 9.9.9 is not in this deployment's catalogue. Offered: 1.13.8.
```

### The mutation that took three tries

The version window is compared as numbers, and the obvious test does not prove
it. With the built-in 1.31–1.36 window a string comparison gives the *same*
answers, including for the "1.9" case the Python comment warns about — the upper
bound rejects it either way. Two mutations passed before I found the shape that
distinguishes them: a **wide** window. With 1.9–1.31, "1.28" sorts below "1.9"
as text, so a textual check refuses a version squarely inside. That case is now
in the test, and the mutation fails on three of its six probes.

### Live

| check | result |
|---|---|
| incompatible pair via `kubectl apply` | denied by admission, with the product's sentence verbatim |
| unknown release | denied, naming what is offered |
| compatible pair | created |
| every minor the catalogue offers (1.31–1.36) | accepted |
| the minors either side (1.30, 1.37) | refused |

The wizard's own endpoint could not be queried on the harness — tenants are
disabled there — and it was left that way deliberately: enabling them would
start the tenant reconciler against the two live UAT tenants, which is another
writer this migration does not need. The comparison was made against the module
the endpoint calls.

## M12b (first slice) — the tenant namespace, and one quota instead of two

Phases 7 and 8: the namespace, the LimitRange, the quota.

### The two quotas, measured before being merged

The plan says to merge them. Measuring first changed the design twice.

Both objects in `tenant-uat-t1` carry **no scopes** and report the *same*
`used: requests.storage` — one counter under two ceilings:

```
tenant-storage        requests.storage 100Gi   persistentvolumeclaims 20   used 44023414784
tenant-uat-t1-quota   requests.storage 120Gi   requests.cpu 7              used 44023414784
```

Kubernetes requires every quota to be satisfied, so what is enforced is the
smaller. Demonstrated rather than reasoned: in a scratch namespace with the same
two objects, a 110Gi claim is refused —

```
persistentvolumeclaims "probe" is forbidden: exceeded quota: a-storage,
requested: requests.storage=110Gi, used: 0, limited: 100Gi
```

— and under a single 220Gi quota the same claim is admitted.

Meanwhile the folder ceiling sums every quota it finds. So that tenant reserves
120Gi, may use 100Gi, and is charged 220Gi. The tenant beside it has only one
object, so the double count is not even consistent between them.

One object, summed, makes the charge and the permission agree. Enforcement does
loosen — 100 to 220 — and that is the point rather than an oversight: 220 was
already being charged, and charging for capacity you forbid is the worse of the
two.

**Except in an adopted namespace.** Adding the allowance while another writer's
cap is still there would take the charge to 320 and change nothing about the
limit, because the other object still binds at its own number. So an adopted
tenant gets the machines only — exactly what the product writes today — and a
condition naming the other quota. Nothing is deleted: something else wrote it.

That redundancy is only ever noticed because the controller watches **every**
ResourceQuota in a tenant namespace rather than the ones it owns. `Owns` would
never deliver the interesting one, and the test proved it: with ownership
watching alone, the foreign quota appeared and the summed value stayed.

### Live, against the tenant the UI built

| | result |
|---|---|
| namespace labels | **identical** (496 bytes each, empty diff) |
| LimitRange | **identical** |
| quota cpu / memory | identical — `7` and `9651617792` = `9425408Ki` |
| quota storage | one object at 220Gi where the UI has two at 100 + 120 |

The last row is the whole change, and everything around it matches.

### M13a note

The tenant domain runs as its own deployment (`--domains=tenant`), a third
alongside vm and network, so a crash in one does not stop the others and each
service account carries only its own rights.

## R-1 — the plan's version did not reproduce, and what did is worse

The plan says a folder member can delete tenant-cluster nodes through inherited
RoleBindings. Measured first:

```
delete machines.cluster.x-k8s.io            -> no
delete machinedeployments.cluster.x-k8s.io  -> no
delete clusters.cluster.x-k8s.io            -> no
delete kubevirtmachinetemplates…            -> no
```

`kubevirt-ui-editor` grants nothing on `cluster.x-k8s.io`. So that claim is not
true as written.

What the same probe *did* return:

```
delete virtualmachines.kubevirt.io  -> yes
get secrets                         -> yes
```

And then, with the lowest role there is:

```
kubectl auth can-i get secret/uat-t1-admin-kubeconfig -n tenant-uat-t1 \
  --as=someone --as-group=kv-poc-transit-viewers
yes
```

A folder **viewer** could read the tenant's admin kubeconfig and its cluster CA.
That is cluster-admin on that tenant, handed to anyone with read access to the
folder. A member's role grants `*` on secrets, so they could rewrite them too,
and delete the worker VMs that are the tenant's nodes.

The mechanism, exactly: a tenant namespace carries the folder and environment
labels — deliberately, so the tenant takes part in folder authorisation — and
`reconcile_folder_rbac` therefore binds `kubevirt-ui-{admin,editor,viewer}` into
it. Those roles grant `get,list,watch` and `*` on `secrets` respectively, which
is right for a project namespace and wrong for one holding another cluster's
certificates.

### The fix, and why it costs nothing

Three new roles — `kubevirt-ui-tenant-{admin,editor,viewer}` — bound instead
when the namespace carries `kubevirt-ui.io/tenant`. Read, and no secrets. All
three are the same, deliberately: everything the product does to a tenant runs
as the backend behind `require_tenant_access`, which checks the caller's folder
role, and the kubeconfig download is one of those. Nothing legitimate reads
those secrets as the user, so nothing breaks.

The namespace label is read rather than the `tenant-` prefix matched: the prefix
is a convention, and a convention is what an authorisation decision must not
rest on.

### Measured after

| group | secret/…-admin-kubeconfig | secrets | delete VMs | list machines | list VMs |
|---|---|---|---|---|---|
| viewers | no | no | no | yes | yes |
| members | no | no | no | yes | yes |
| admins | no | no | no | yes | yes |

And an ordinary project namespace is untouched — `poc-transit-dev` still answers
`yes` to a viewer reading secrets, which is what those roles were written for.

Applied to the stand: three ClusterRoles created, six RoleBindings repointed
across the two live tenants.

## M12c (first slice) — N7, the worker template with no Kubernetes CA

Without `cluster.ca` in its machine config a Talos worker boots, runs a kubelet,
and never joins: nothing files its CSR, so the node does not exist as far as the
cluster is concerned while the VM looks perfectly healthy.

The repair is a port — a new template, because the CRD is immutable, and a
MachineDeployment repointed at it. What moved is where it runs. It was a call in
`reconcile_loop` inside the request-serving backend: a timer, no watch, no
leader election. Two replicas did it twice, one replica did it never after a
restart, and a template broken a second after a pass stayed broken until the
next one. Now a write to the template, the MachineDeployment **or the CA secret**
wakes it — the last one matters, because the CA arriving is the event that makes
a repair possible at all.

### Two things it deliberately does not do

It reads `cluster.ca`, never `machine.ca`. The latter is the Talos CA and is
always present; checking it would report every template as healthy. And it will
not write a replacement while the CA is absent: baking a CA-less config into a
second immutable object makes the defect permanent twice over.

### Two vacuous tests, found by mutation

The lookalike-namespace test passed for the wrong reason — there was no CA
secret there, so nothing was written regardless of the label, and removing the
guard changed nothing. It now has everything a repair needs, so only the label
stops it.

Then a second: the guard could have lived only in the watch predicate, which is
wiring another Watch can widen — and here three other watches map by namespace
with no label filter of their own. So there is now a test that calls Reconcile
directly, with no predicate in the way, and asserts nothing is written; it also
calls it on a labelled namespace, so it cannot pass by the invocation doing
nothing at all.

### Cutover, then the deliberate breakage

The backend loop still ran the same repair, so: `OPERATOR_TENANT_BOOTSTRAP_ENABLED`
on the backend first (writers 1 → 0), then the operator's tenant domain (0 → 1).
The running pod was checked by image and by environment before the second half,
not the Deployment spec.

Then the acceptance the plan asks for, in a throwaway namespace rather than on a
live tenant — breaking a real one means rolling its workers:

```
template with no cluster.ca applied
repair after 12s
configRef -> brkprobe-workers-ca
cluster.ca present in the replacement
original intact
```

And the two live tenants were untouched: `uat-t1-workers` and `uat-t2-workers`
still the only templates in their namespaces, both carrying their CA.

Attribution, since both writers make the identical two moves and the order alone
would not settle who made them: the operator logged the repair and left a
`WorkerBootstrapRepaired` event on the namespace, and the backend log has no
mention of `brkprobe`, `worker bootstrap` or `workers-ca` in that window.

### Two defects the live log showed and the tests did not

The same log said **"wrote a replacement and repointed the MachineDeployment"
twice, in the same second**. The original template is immutable, so it stays
broken for the tenant's whole life and every wake finds it broken again — the
announcement was firing on finding the defect rather than on fixing it, which
turns one real repair into a warning nobody can pick out from the noise.

Reporting only actual writes was half of it. The other half was subtler and is
the same rule as everywhere else in this migration: **whether a write changed
anything is the server's answer, not the caller's intent.** The read comes from
a cache, and right after a repair that cache is a version behind — the
comparison says the workers still point at the broken template, the patch says
what they already say, the API server does nothing, and a controller trusting
its own intent announces a repair that never happened. The check is now the
resource version before and after the patch. envtest reproduced the double
announcement exactly, before and after the first fix, which is how the second
one was found at all.

The second defect: a namespace being torn down refuses new content, so the
repair could only fail — and it failed loudly, once per backoff, for as long as
termination took. Eleven `Reconciler error` lines in six seconds on the stand.
It now leaves a dying namespace alone; the workers are going away with it.

Both tests were mutated to check they were not vacuous. The announcement test
had to be rewritten first: the version that broke the MachineDeployment pointer
by hand and watched a private recorder was racing the running manager for who
would fix it, and losing that race is indistinguishable from silence. It counts
the manager's own events now, aggregation included.

## M12c (second slice) — one golden image per release, not per tenant

A Talos worker's root disk is a clone of a shared image, and the sharing is what
makes a second tenant of the same release cheap: it clones a disk that is
already there instead of pulling 20Gi over HTTP again. H2 is that property
stated as an acceptance — second tenant, zero new imports.

### The tenant declares an image; it does not import one

The product's version created the DataVolume itself and then waited, inside the
HTTP request, up to twenty seconds for an import it had just started. The
operator writes a **ManagedImage** instead and lets the image controller do what
it already does. Two things follow from that and both are the point of this
migration: importing disks has one writer rather than two, and "not yet" has
somewhere to live — a `GoldenReady` condition — instead of being a timeout in a
request handler.

It crosses domains on purpose: the tenant domain writes the CR, the vm domain
reconciles it. One writer of the declaration, one writer of the disk.

Nothing about the image is owned by the tenant. An ownerReference would make the
first tenant deleted take the shared disk away from everyone still cloning it.
What protects it instead is already in the image controller: deletion is refused
while any DataVolume clones from the claim, and it removes only what carries its
own ownership stamp.

### The name is the mechanism

`talos-golden-1-13-8` — the catalogue key with its dots flattened. Two tenants
asking for one release ask for one object, so sharing is not a lookup that could
go wrong but the identity of the thing. The test says so directly: same release
must give the same name, different releases must not, or an upgrade would
silently reuse the old disk.

### Two subjects on the clone path

The grant is `datavolumes/source` in the namespace that holds the image, and the
subject is **not** the backend. The worker's root disk is a `dataVolumeTemplate`
on the VirtualMachine, so KubeVirt creates it and CDI evaluates the tenant
namespace's default ServiceAccount — which the cluster once said in as many
words. One Role, one RoleBinding per tenant namespace, so a second tenant adds a
grant rather than rewriting the first one's.

### What the acceptance had to prove

Not just "one image": that the disk behind it was not touched. The test holds
the golden DataVolume's UID and resourceVersion across the second tenant's whole
reconcile, and separately checks the write counter for the tenant controller —
because a no-op update comes back from the API server as identical bytes, so
resourceVersion alone cannot tell a write that changed nothing from no write at
all. Mutating the name to include the tenant turns the test red on the first
assertion.

Two branches were named honestly rather than dressed up: the condition test
proves the status is read from the image rather than from the write that made it
(envtest has no CDI, so a True would mean it is not reading at all), and the
"catalogue has no image" refusal is exercised by calling the function directly,
since a release the catalogue does not have is already refused at `Accepted`.

### Still one writer short

The backend creates the golden itself on its own tenant path. There is no
conflict today because nothing in production creates a ManagedTenant yet — the
operator's golden runs only for CRs written by hand. When the backend starts
creating ManagedTenants (M12d/M12e), the golden call has to come out of the
backend in the same change, not after it.

### Live: H2, and one grant that the lab could not have refused

On the stand, where the golden already existed because the product built it:

```
first tenant   image talos-golden-1-13-8 Ready
               dv uid 4bce6b88… phase Succeeded  (adopted: labels stamped,
                                                  rv 142136 -> 1065491)
               rolebinding talos-golden-cloner-tenant-gld1 created
second tenant  images 1, DataVolumes named talos-golden-1-13-8: 1
               dv uid unchanged, rv unchanged (1065491 -> 1065491)
               image rv unchanged (1065494 -> 1065494)
               rolebinding talos-golden-cloner-tenant-gld2 created
both           GoldenReady=True
```

The adoption is the interesting half: the operator took over a disk the product
imported, without re-importing it and without touching its spec. Only labels
moved, once.

**The grant would have been refused, and no test in this repository could have
said so.** Kubernetes will not let a writer create a Role conferring permissions
it does not hold itself, and handing `datavolumes/source` to each tenant's
ServiceAccount is exactly what this does. envtest runs as admin, so the check
never fires there. Proven by A/B on the stand, with the controller's own
ClusterRole from before and after the fix bound to two throwaway subjects:

```
old role -> Forbidden: attempting to grant RBAC permissions not currently held:
            {APIGroups:["cdi.kubevirt.io"], Resources:["datavolumes/source"],
             Verbs:["create"]}
new role -> role.rbac.authorization.k8s.io/rbac-probe-cloner created
```

A measurement note worth keeping, because it nearly sent me the wrong way:
`kubectl auth can-i create datavolumes/source` answers **yes** under both roles.
It parses `source` as the object's *name*, not as a subresource. The forms that
actually answer the question are `--subresource=source` (no under the old role,
yes under the new one) and attempting the write itself, which is where the
escalation check lives.

Known gap, named rather than fixed here: deleting a ManagedTenant leaves its
clone grant behind. Tenant teardown does not exist yet — the controller returns
on a deletionTimestamp and nothing is reclaimed — so this belongs with that
slice, not this one.

## M12c (third slice) — the tenant's own address

Everything else in this phase is derived from one value: the certificate's IP
SAN, the worker's control-plane endpoint, where the node gets the time. So the
address comes first, and it is published in status where the things built from
it can be seen to agree.

Its own address, not a shared one, because Talos derives trustd's location from
the control-plane endpoint and dials :50001 there. That port cannot be moved, so
two tenants cannot share a listener for it — the whole per-tenant VIP model
follows from a hard-coded port number.

The product asked MetalLB for the address and then waited for the answer inside
the request that created the tenant, up to a minute, and turned a full pool into
a timed-out API call. Here the waiting is the ordinary state of a controller:
pending is a condition with a reason that names the pool and says what to do
about it (append a range — never resize one in use).

### The watch is load-bearing, and that was measured

The address arrives on the Service's *status*, written by MetalLB long after the
Service was asked for. Without a watch on Services the tenant carried "no
address yet" until something unrelated woke it, and with a ten-hour resync that
is indistinguishable from never. The test failed exactly that way before the
watch was added — the address was assigned and status stayed empty for the full
twenty seconds.

### The invariant that has to run before the address exists

kube-ovn allocates router legs and EIPs out of the transit subnet, so any
MetalLB range the subnet does not exclude is an address both allocators can hand
out — found, when it is found, as a duplicate-address outage. The check is
therefore before the Service is created: the damage is done by an address being
handed out, not by asking for one. Mutating the order so the refusal comes after
turns the test red with "it asked for an address anyway".

Silent when it cannot read either object. A diagnostic that stops tenants from
being created because a CRD is missing has stopped being a diagnostic — and the
MetalLB CRD is now in the suite's schemas, so the check is exercised rather than
skipped there.

### Configuration by the same names the backend uses

`TENANTS_CP_METALLB_POOL`, `TENANTS_CP_METALLB_NAMESPACE`,
`TENANTS_CP_TRANSIT_SUBNET` — and the first draft read
`TENANTS_TRANSIT_SUBNET`, which exists nowhere. Under the deployment's env block
that reads as an invariant that quietly never runs, and "no complaint" looks
exactly like "checked and fine". They are fields on the reconciler with an
environment fallback: the value belongs to the deployment, and a test that has
to set a process variable to exercise one controller sets it for every other
controller running beside it.

The tenant Deployment had none of these at all, so the pool would have defaulted
to `traefik` — the ingress pool, not the transit one. Added to the overlay in
the same change.

### Method note

Three mutation runs in this slice reported the wrong result because the test
binary was built from a file the container had not seen yet — the bind mount
lags a write by a second or two. A mutation is now confirmed *inside* the
container before its result is believed.

### Live: the address, and the invariant proved by making it fire

```
tenant adr1  MetalLB gave 10.199.0.102 from cp-transit-pool
             sharing key adr1-cp, AddressAssigned=True(Assigned)
             status.controlPlaneVIP = 10.199.0.102
```

The first read of that last line was **empty while the condition said
Assigned** — the live CRD predated the field, so the API server pruned it. The
condition and the value it describes disagreed, and only the value was wrong.
Applying the CRD fixed it. It is the same class as everything else in this log:
the change had not reached the object being measured.

Then the invariant, made to fire rather than assumed: the operator was pointed
at `ovn-default`, whose excludeIps are `10.16.0.1` and cover nothing in
`10.199.0.0/24`. A new tenant was refused —

```
PoolOverlapsSubnet: MetalLB pool "cp-transit-pool" has ranges outside the
excludeIps of subnet "ovn-default": 10.199.0.100-10.199.0.119. …
services "adr2-cp-lb" not found
```

— refused, with no Service asked for, and the setting put back afterwards. A
check that has never been seen to say no is not a check.

The two live tenants kept their addresses and their NTP Services on them
(10.199.0.100 and .101, each shared between cp-lb and ntp).

## M12c (fourth slice) — the CSR signer's PKI

A Talos worker asks trustd for a certificate instead of presenting a token, so
this chain is the difference between a node that joins and a VM that looks
perfectly healthy while the cluster has never heard of it.

Four objects, and the shapes are the product's: a selfSigned Issuer, a ten-year
Ed25519 CA (rotating it means re-provisioning every node), an Issuer from that
CA, and the signer's own certificate for a year.

**It waits for the address.** The old rule was DNS names only, for a good
reason: an IP SAN could only be added once the address existed, which meant
patching the certificate afterwards — and the signer reads its certificate once
at startup and never watches the file. Per-tenant addresses remove the ordering
problem, so the SAN is issued with the certificate. It is also required: a
worker dials `<vip>:50001` by address, sends no SNI, and a DNS-only certificate
fails the handshake before trustd is asked anything.

### Ready means the certificate answers, not that a file exists

The first version reported Issued when the secret existed, and a reviewer was
right to call it the wrong object to measure. Two ways it lies, both reporting a
working signer while the handshake fails: a tenant that predates per-tenant
addresses already has a `<t>-talos-signer` secret with a DNS-only certificate,
and adding `ipAddresses` to the Certificate leaves the old secret in place until
cert-manager re-issues. A signer started in that window reads its file once and
never sees the replacement.

So `tls.crt` is parsed and the VIP must be among the IP SANs, with both DNS
names present. The tests build real certificates for each case — names only, a
foreign address, one name missing, and finally the right one — so the refusals
are the check working rather than a condition that never turns true. Mutating
the check to accept everything turns the first case red.

### Waiting for what nobody wakes us for

Two things here are not watched: cert-manager writing the signer's secret, and
the image controller finishing an import. Watching either means caching every
Secret and every ManagedImage in the cluster to notice one object apiece, so the
tenant requeues every ten seconds while something is pending and stops as soon
as it is not. The PKI test found this the honest way: it assigned the secret and
waited twenty seconds for a condition that had nothing to wake it.

## M12c (fifth slice) — somewhere to ask for the time

Talos will not start a kubelet against an unsynchronised clock, so a worker in a
VPC with no egress does not join, and the symptom is a VM that boots, stays up,
and never becomes a node. The answer is the tenant's own address again: chrony
behind it, on 123/udp, sharing the address the API server already answers on.

Ported with its scars intact, all of them measured rather than reasoned:

* `local stratum 10`, without which chronyd answers nothing at all until it
  considers itself synchronised — while the pod reports Ready throughout;
* `rm -f` of the pid file before exec, because chronyd is pid 1 here and writes
  "1" into its own pid file on an emptyDir that survives a restart, so the next
  start finds a live process with that pid (itself) and dies forever;
* `-x`, which is what "serves a clock, never sets one" means to chronyd —
  without it, it tries to discipline the node's clock and dies for want of
  CAP_SYS_TIME, and the alternative is granting a pod CAP_SYS_TIME;
* the capability set, arrived at from crash logs rather than from principle;
* `externalTrafficPolicy: Cluster`, because Local black-holes the request from
  any node without a replica — including, during a join, the node making it.

### Ready means somebody answers

The condition is not "the Service exists". It failed once in exactly that shape:
the Service was created in the tenant's namespace, where a Service selects only
pods beside it, so it had an address and no endpoints. Every query timed out,
which looks exactly like a server refusing to answer — and the first round of
diagnosis went to chronyd's configuration instead of to the Service. So
readiness reads EndpointSlices and requires a ready endpoint, and separately
requires the address MetalLB actually assigned to equal the one asked for,
because a sharing-key mismatch leaves the second Service pending forever.

Both gates were mutated out; both tests went red, one reporting `Served` with
"0 chrony endpoint(s)" in its own message.

### The retire flag, which is not tidiness here

The backend writes the same three objects when a tenant is created. For the
per-tenant Service that would be harmless, but the chrony **Deployment** would
have two renderers, and any difference between them rolls it from whichever side
wrote last. Chrony is what a joining worker asks for the time: rolling it during
a join is a node that never appears.

So `OPERATOR_TENANT_TIME_ENABLED`, in the N7 shape — flag on first (writers
1 → 0), then the operator's tenant domain (0 → 1). The write is extracted into
one function so the flag can be tested directly, and the test also asserts the
guarded calls appear exactly once in the module, because a flag that covers one
of two call sites is worse than no flag.

### Live: the two renderers compared, and a time source that was not there

The stand answered the question before the acceptance could: **the chrony
Deployment did not exist.** Its ConfigMap did, created at 01:18 by the backend's
own client, from the same call that should have created the Deployment beside
it. Both live tenants were holding NTP addresses that served nothing — the exact
shape the new condition exists to name, found by looking for something else.

Who removed it is not established, and it is worth saying so rather than
guessing. What is established: nothing in this repository deletes a Deployment
by that name — neither the backend nor any controller has such a path — the
ConfigMap from the same moment survived, so nothing swept the namespace, and no
operator or backend log mentions it. Events last an hour and the deletion is
older than that. The backend's own writer swallows a failed create with a log
line, so "never created" is as consistent with the evidence as "deleted",
and both are invisible for the same reason: nothing reported on it afterwards.

Then the acceptance the two-renderer question deserved. The backend's rendering
was applied first, from the backend's own code, on the stand:

```
kubectl exec … python -c 'print(build_chrony_config_map(), build_chrony_deployment())'
deployment.apps/kubevirt-ui-ntp created   gen=1  2/2 ready
```

— which also put the live tenants' time back. Then the flag on, the operator's
tenant domain up, and a tenant created through a CR:

```
chrony Deployment  generation 1 -> 1   resourceVersion 1135120 -> 1135120
chrony ConfigMap   resourceVersion 1134857 -> 1136536
tim1  AddressAssigned=True PKIReady=True TimeServed=True(Served: 10.199.0.102:123,
      2 chrony endpoint(s)) GoldenReady=True NamespaceReady=True QuotaReserved=True
```

The Deployment did not move — the two renderers agree on the object that would
roll a joining worker's time source. **The ConfigMap did**, and that is the
finding: the same directives with the explanation moved into a Go comment is a
different file, so each pass rewrote what the other wrote. Harmless in itself —
nothing reloads chronyd on a ConfigMap change — but it is the fight the flag
exists to prevent, arriving on the one object where it did not matter. The Go
text is now byte-identical to the backend's, with the explanation back in the
file where whoever reads it reads it in the cluster, and a test reads
`CHRONY_CONF` out of the backend's source and compares. When that writer is
deleted the test goes with it.

PKIReady=True on that tenant is worth its own line: cert-manager issued the
signer certificate, and the SAN check — the VIP among the IP addresses, both DNS
names present — passed against a real certificate rather than a hand-built one.

### The flake, caught and named

Rather than leaving it to CI: eight runs in a row with the output kept, stopping
at the first red. Run 4 was it —

```
managedtenant_address_test.go:204: the Service was not asked for even with the
pool excluded: Service "tadx-cp-lb" not found
```

— a race in my own test, not in the product. It read a Service it had just
caused to be created through the *cached* client, whose informer had not caught
up. The dangerous half is the assertion beside it: "no Service was created" read
the same way, so a cache one beat behind would have made the safety check pass
for the wrong reason. Both now read straight from the API server, through a
reader added to the suite for exactly this — and the helper that fakes MetalLB
patches the status instead of reading-and-updating it, which is the same race
one object over.

### Correction: only a tenant in a VPC needs an address of its own

The address slice handed every tenant a MetalLB address. The product does not:
`acquire_tenant_vip` is called only for a tenant in a VPC, because on the
default overlay the control plane is reached by the Kamaji Service's ClusterIP,
which is natively routable there. The pool on this lab is twenty addresses;
giving one to every tenant that will never dial it is how it runs out.

Caught while reading `_build_cluster_cr` for the next slice, before anything was
built on top of it — which is the only reason it was cheap. Three things follow
from the same fact and all of them had to move together:

* no cp-lb Service, and no `AddressAssigned` condition, on the default overlay —
  a condition about something the tenant does not have is noise, the same
  argument as `GoldenReady` on a cloud-init tenant;
* no time Service either. A worker there reaches the public servers the way it
  reaches everything else;
* **the signer certificate gets names and no address.** Not cosmetic:
  cert-manager refuses a certificate with an empty `ipAddresses`, so the whole
  chain would fail to issue. Readiness follows — with no address, only the names
  are checked, because only the names are dialled.

## M12d (first slice) — the control plane, declared

Three objects: the CAPI `Cluster`, the `KubevirtCluster` that CAPK reads, and
the `KamajiControlPlane` that actually runs an apiserver. Declared, not built —
Kamaji creates the pods and CAPI wires them together; what this owns is the
description they act on.

Plus the tenant's machine secrets, which the PKI slice missed: the signer
sidecar mounts `<t>-talos-secrets` for the token a worker authenticates with.
Written **once**, create-if-absent, never through `Ensure` — rotating the token
means a new worker cannot authenticate while the existing ones stop being issued
certificates, which presents as a broken signer rather than a changed secret.

### Where a worker is told to join

Two models, decided by whether the tenant has a network of its own.

In a VPC: its own address, and `apiServerPort` on the Cluster so cluster-info
advertises `<vip>:6443`. The endpoint used to be the external ingress name here,
on the assumption that cluster-info already carried the address — it cannot,
because CAPI copies this field into the worker's discovery endpoint and the
worker must fetch cluster-info before it can learn anything from it. So the
worker dialled a name its VPC could neither resolve nor route to, and three lab
runs read that as "CAPK will not bootstrap".

On the default overlay: the Kamaji Service's ClusterIP, natively routable there
and absent when the Cluster is first written. The product polled for it inside
the creating request and patched the field afterwards; here the tenant says what
it is waiting for and comes back.

### A field that does not exist

The product writes `network.additionalPorts` on the KamajiControlPlane to
publish trustd. **KamajiControlPlane has no such field.** Its schema is
advertiseAddress, certSANs, dnsServiceIPs, gateway, ingress, loadBalancerConfig,
serviceAddress, serviceAnnotations, serviceLabels, serviceType — and the API
server prunes anything else without a word. Read off the CRD, then confirmed on
both live tenants: their `network` carries exactly advertiseAddress, certSANs
and serviceType.

It never mattered, because trustd is published by the tenant's own Services —
the LoadBalancer on its address and the ClusterIP beside it — which is where
workers actually reach it. But four tests asserted the port was configured, and
all four passed by reading the dictionary the code had just built rather than
anything a cluster kept. That is the same disease as the constant-grepping test
in T1, one layer up: they protected a write that did nothing.

Removed from the operator before it was copied, and removed from the backend in
the same change, along with the `shared_vip` parameter that existed only to
decide it. What replaces the four: one test asserting that `spec.network`
contains nothing outside the schema, which is the property rather than the
literal.

This is also the first thing found by reading a foreign CRD instead of the code
that writes to it — worth repeating for the other three kinds in this phase.

## M12d (second slice) — the worker pool

Four objects: the machine config a node boots with, the VM shape CAPK stamps
out, the MachineDeployment that scales them, and the health check that replaces
one whose node stops answering.

### Waiting is the design, not an inconvenience

The bootstrap template is **immutable**: whatever is written is what every
worker of this tenant gets for as long as it lives. Kamaji mints the Kubernetes
CA while the config is being assembled, and the two used to race — measured to
the second: a read at 09:41:30 that missed a secret created at 09:41:31, and a
tenant that then failed every boot with `secrets.KubeletController: missing
accepted Kubernetes CAs` twelve seconds in, before any network, so it read as a
mystery rather than a race.

The product waited inside the creating request, up to two minutes. Here there is
nothing to wait in: the tenant reports which certificate is missing and comes
back. Both CAs are required and they are different certificates — the machine CA
is Talos's own, the cluster CA is what the kubelet must trust to talk to the
tenant apiserver — and the test proves one is not enough by supplying only the
first and watching nothing be written.

That also makes N7 nearly redundant for tenants the operator builds: the repair
exists because the product could write a CA-less template, and this cannot. It
stays for the tenants the product already made.

### Two facts that only a cluster taught

`base64` in both directions: Kubernetes stores secret data encoded, the Python
client hands it back still encoded, and Talos wants it that way — but client-go
decodes it first, so the Go port has to encode it again. Silently wrong
otherwise, in a field nothing validates.

And the kubelet image is pinned to the tenant's Kubernetes version rather than
left to the Talos image. Talos ships whatever kubelet matches *its* release; on
a control plane several minors older, the node boots, apid and the kubelet both
report healthy, and it never registers.

### What the health check is worth

`maxUnhealthy: 100%`, because a one-worker tenant is the common case and the
usual guard would refuse to remediate the only worker — exactly when the tenant
is fully down. The unhealthy window is three times the measured three-minute
return, so it moves when the measurement does; the cost is stated rather than
hidden, a genuinely dead worker detected about five minutes later. And a drain
timeout, because a node that is gone cannot be drained: it stopped on a
disruption budget needing a healthy pod it no longer had, and with no timeout
CAPI retried forever while the replacement never arrived.

### Named gap: cloud-init

The operator builds Talos workers only, and says so on the condition rather than
half-building a pool. The cloud-init path is a KubeadmConfigTemplate carrying a
page of shell — a resolv.conf that survives DHCP, a disk moved under `/var/lib`
because a containerDisk overlay reports zero capacity, a kubelet-config filter
for fields a newer control plane emits, and a postmortem script that is
debugging scaffolding rather than product. Porting it, and deciding which of it
is worth keeping, is a slice of its own.

Also not carried over: the per-tenant DNS overrides the product's request object
has (`dns_mode`, `dns_servers`). The CRD has no field for them, and inventing
one here would be a second place to configure the same thing.

## Found by porting: a quota reserving disks that do not exist

Half of every Talos tenant's storage reservation was for capacity that could
never be allocated, and I had just copied the arithmetic across faithfully
before a reviewer pointed at the comment justifying it.

`requests.storage` is **PVC** storage. A worker's `data` disk is an
`emptyDisk` — a qcow2 on the launcher pod's ephemeral storage — and a
cloud-init root is a `containerDisk`, which is the same. Neither is a claim, so
neither can spend that quota. Measured on the stand rather than argued:

```
tenant-uat-t1  PVCs: two 20Gi worker roots, one 1Gi tenant volume
               VM volumes: root=dataVolume, data=emptyDisk
               quota hard 120Gi, used 41Gi
```

120Gi is exactly `surge×20Gi` of `data` disks plus `surge×20Gi` of roots: sixty
of those gigabytes were phantom, and the folder ceiling — which sums every
quota it finds — charged for them. Over-reserving is the safe direction, which
is why nobody noticed; the comment claiming "both are real PVCs" is what made it
survive review.

A cloud-init tenant now reserves **no** PVC storage at all. One of the tests
that pinned the old number said so in its own docstring while asserting the
opposite: *"They boot a containerDisk — no DataVolume, nothing to count"*,
followed by `assert storage == 3 * 20Gi`.

What the `data` disks really consume is the node's ephemeral storage, which no
quota here governs. Named rather than quietly dropped: putting a ceiling on
`requests.ephemeral-storage` would make that request mandatory for every pod in
the namespace, which is the trap the LimitRange exists to avoid.

### One table, both arithmetics

Eight tests pinned the old numbers across two suites, which is how a shared
number drifts: each side is asserted against itself. So there is now
`test/parity/tenant-quota.json` — four shapes, exact bytes — read by a Go test
and a pytest, generated by neither. While the backend still computes tenant
quotas, and it does for every tenant the product creates, two arithmetics decide
one number; the failure worth catching is not either being wrong but the two
disagreeing, because a tenant adopted from one and reconciled by the other has
its quota silently rewritten. Mutating one expectation in the file turns both
suites red, which is the check.

### Method: mutations no longer happen in the working tree

Three times now a reviewer has read a deliberately broken file mid-mutation and
reported it as a defect — the `DeletionTimestamp` guard disabled, then the
worker root pointed at the golden PVC by `claimName`. Both were mutations, both
lived about seven seconds, and neither was ever committed: the file
`managedtenant_workers.go` has one commit, and `claimName` appears nowhere in
its history. In each case the test caught the mutation, which was the point of
making it.

That is my fault, not the reader's: a mutation applied in place is visible to
anything reading the repo while it runs, and a deliberately broken file looks
exactly like a defect. Mutations now run in a throwaway git worktree —

```
git worktree add --detach .mutation HEAD
# edit .mutation/…, run the suite there, read the failure
git worktree remove --force .mutation
```

— which keeps the tree somebody else may be reading pristine, and has the
side-benefit of mutating **committed** state rather than whatever is on disk.

That caveat then bit on its first real use: the harness under test was
uncommitted, the worktree was at HEAD, and the mutation measured the old code
and stayed green — reading as "the restriction is silent" when the restriction
simply was not there. And twice after that I fell back to mutating in the tree
anyway, because the change was uncommitted and the worktree could not see it.

A practice that depends on remembering is not a practice, so it is a script now:
`hack/mutate.sh` copies the **working** tree — uncommitted changes included —
into a throwaway directory, applies the mutation there, runs the tests, and
refuses with a distinct exit code when the expression matched nothing. That last
guard is the one that matters: a mutation run that silently changed nothing
reads exactly like a test that caught something.

## M12d live: the graph built end to end, and five differences

A disposable tenant in a VPC of its own, the same shape as `uat-t1`, built from
one CR — and then its whole graph diffed against the live one, normalised for
names, addresses, tokens and base64.

```
cluster, kamajicontrolplane, kubevirtcluster, machinedeployment,
kubevirtmachinetemplate, machinehealthcheck, talosconfigtemplate
```

All seven objects, from a `ManagedTenant` and nothing else. The health check is
byte-identical to the live one. The others differ in five ways, and only two of
them were agreed beforehand.

### It could not create its own secrets

The first attempt stopped at

```
creating tenant-cmp1/cmp1-talos-secrets: secrets is forbidden:
User "…operator-tenant-controller-manager" cannot create resource "secrets"
```

— the marker granted `get;list;watch` and the code writes. Third time this class
has cost a live run and it will keep costing them: envtest is admin, so no RBAC
check ever fires in the suite.

### Two things it would have got wrong, found only by the diff

**The worker's resolver.** The live config lists `10.96.0.200` first — the
VpcDns address — then the public servers; the operator's listed the public ones
alone. In an isolated VPC that is a node that resolves nothing, and it is
written into an **immutable** template, so it would have been wrong for that
node's whole life.

Fixing it turned up the sharper half. The address exists in two places the
cluster already states it: the network's own status, and kube-ovn's
`vpc-dns-config`, which is where the network controller reads it from. Neither
is an environment variable, and that matters — a VPC the product built has no
ManagedNetwork, so insisting on our own object would be the operator requiring
the world to be its shape. Both are read, in that order, **straight from the API
server**: a cached read a beat early bakes the wrong answer into an immutable
object. And the config is not written at all until one of them answers, which is
the same wait as the two CAs and for the same reason.

**The root clone's storage class.** The live roots are on `ceph-block`; the port
sent them to the *golden's* class. Those are deliberately different pools — the
golden is read-only reference data cloned many times and wants erasure coding,
the clones want replica — so this was not a missing environment variable but a
wrong field. The CRD gained `storage.className` for it.

### Two named gaps and one difference that is not one

`--oidc-*` on the apiserver: the live control plane carries four flags, the
operator wrote none, because the CRD had no field for the per-tenant toggle. It
has one now, and the flags are gated on the issuer being https — an apiserver
told to trust an http issuer refuses to start, and a control plane that will not
start is worse than one without single sign-on.

`infraClusterSecretRef` on the KubevirtCluster: tenant CSI storage, which is
M12e, and confirmed absent rather than assumed.

The rest is additive metadata — a `kubevirt-ui.io/tenant` label on objects the
product leaves unlabelled — and the ingress certSAN, which is empty because the
operator has no ingress domain configured yet.

### The one the diff could not have found

With every difference closed, the tenant still would not come up: six
control-plane containers crash-looping, the apiserver dying on `error creating
leases`. Nothing in any message named a network. The cause was three layers
down, in kine:

```
failed to connect to host=kamaji-postgres-rw.o0-cnpg.svc:
lookup … on 10.96.0.10:53: i/o timeout
```

and the pod's own address gave it away: `10.200.16.29`, inside the tenant's VPC.

**The namespace was pinned to the VPC subnet, and that was mine** — from the
namespace slice, with a comment explaining the reasoning: kube-ovn claims a new
namespace for the cluster overlay, so pin it to the tenant's network or pods
born there land on 10.16/16. Exactly backwards. The control plane lives in that
namespace too, and from inside a VPC it can reach neither the datastore, nor the
ingress, nor anything else the platform runs. Only the worker *launcher pods*
belong in the VPC, and they get there by the annotation on their own template —
which is what the product does, and which I had already ported correctly one
file over.

A test asserted the wrong behaviour, in the same words as the comment. It now
asserts the opposite, with the crash it prevents written into it.

No test could have caught this: envtest has no CNI, so a pod there has no
address at all, and the diff could not either — it compares what is declared,
and both declarations were the same shape. It took a control plane that would
not start.

### Ceasing to write it is not undoing it

The namespace fix stops the operator stamping the annotation; it does not lift
the stamp from a namespace already carrying one, and such a tenant stays broken
after the upgrade that fixed the cause. `tenant-cmp2` was rebuilt rather than
healed — it was disposable — but the reconciler now removes the annotation when
it finds it, and only when the value is **our** old stamp, the tenant's own VPC
subnet. `ovn-default` is kube-ovn's own claim and the value a healthy tenant
namespace carries; taking that away would be the same mistake pointed the other
way. The test asserts both halves, and mutating the removal turns it red.

### A tenant, end to end, from one custom resource

```
Accepted  AddressAssigned  PKIReady  TimeServed
ControlPlaneReady  WorkersReady  GoldenReady  NamespaceReady  QuotaReserved
                                                       all True

roots     cmp3-workers-…-bwtvh-root  20Gi  ceph-block
          cmp3-workers-…-tmq7p-root  20Gi  ceph-block
golden    one DataVolume in the cluster, no new import
quota     hard 160Gi, used 40Gi — the two roots and nothing phantom
```

Two workers, each with its own root clone on the tenant's storage class, from
the golden that already existed.

**What actually held the join up is worth recording precisely**, because it was
not the graph. The control plane went Ready and both machines reached
`Provisioned`, and there they sat: no `nodeRef`, seven minutes. The VPC had no
external plane at all — no transit leg, no default route — so the workers could
reach neither their own control-plane address nor anything else. One field on
the network (`externalPlane.attachments`) and the network controller did the
rest: both attachments, `enableExternal`, and the default route via the border,
matching the live tenant exactly. **Both workers joined forty seconds later.**

That is a measured answer to a question the next slice was going to ask: the
join needed the VPC's external plane and **nothing from `_wire_tenant_to_transit`
at all** — no SNAT rule was created for this tenant and none was missing. What
that function is still for, on this path, is now an open question with evidence
attached rather than an assumption to port.

## M12d (third slice, part one) — the transit plane's rules

The user settled what this plane is for, and it is worth writing down because it
decides the shape: **a tenant's workers reach their control plane, and the CSI
path reaches the host API, over the underlay leg — so that an egress gateway
falling over takes the internet and nothing else.** The lab's plan says the same
thing in one line: cp-transit is VLAN 300, L2-only, *without a gateway leg*.
Ceph is not on this path at all; tenant workers never touch it.

That also corrects something I got wrong. I had reported that the join needed
nothing from the product's transit wiring, having checked one artefact of three.
Measured properly, the live tenants carry all three and my disposable one
carried none:

```
                                uat-net-t1              cmp-net
policy route @30000             ip4.dst == 10.199.0.0/22   —
OvnEip (nat) on cp-transit      cpt-eip-uat-t1 → .1.4      —
OvnSnatRule                     10.200.4.0/22 → .1.4       —
```

It joined anyway, by a return path I have not identified — the subnet does not
NAT outgoing and the tenant prefix is not announced. So "nothing was needed" was
not a conclusion I could stand behind; three things the reference creates were
absent, and the reference documents exactly the silent failure their absence
produces.

### The rules, ported and checked against the reference itself

The arithmetic is the interesting part and it is easy to get almost right. The
deny is scoped by **source** to the range kube-ovn actually allocates from — the
subnet minus its excludeIps — because the whole subnet would put the nodes and
the control-plane VIP on the left of a drop rule; and it is taken whole rather
than as its first /24, or the rule is about the tenants numbered lowest and the
129th quietly falls out of it.

Go has no `address_exclude` and no `summarize_address_range`, so both are
written here. Rather than trust that, the reference was asked for its own
answers on five inputs and they are frozen in `test/parity/transit-rules.json`,
which both suites now assert:

```
10.199.0.0/22 less 10.199.0.1..10.199.0.255 -> 10.199.1.0/24, 10.199.2.0/23
10.0.0.0/24   less 10.0.0.1                 -> /31 /30 /29 /28 /27 /26 /25
```

The second is the one that would have gone unnoticed: a single reserved address
decomposes into seven prefixes, and any of them being off by one bit is a hole
or a locked-out tenant. Mutating one entry in the table turns both suites red.

## M12d (third slice, part two) — the wiring, and the deny that would have taken two tenants down

Three parts, in one order that matters. The VPC gets a port on the plane **and
the policy route protecting it in the same write** — policy routes beat static
ones, so a gateway's catch-all swallows the packets going one hop to the control
plane, and a VPC attached without the guard has a leg it cannot use for the one
thing the leg is for. Then the tenant's subnet gets an address on that plane to
leave under. Then the guard ACLs let that address, and only it, reach that VIP
on those ports.

The SNAT slot is decided on the **whole set** of rules claiming the subnet,
sorted by name, never on the first match: OVN keeps one NAT per logical IP, so
two rules cannot both be in force and the loser reports `ready: true` about a
NAT the router does not have. A rule on another network holding the slot is
**reported, not absorbed** — inheriting it writes the guard for an address on
the wrong plane, which looks configured and works for nothing. A rule wedged by
a missing address gets its finalizer released, but only in that exact state.

### The baseline deny, and why it is withheld

A reviewer stopped this before it reached the stand, and the measurement was
worse than the guess: `cp-transit` carries **no ACLs at all**, while two live
tenants hold `nat` addresses inside the allocatable range. The reference writes
a deny plus per-tenant allows there; on this cluster it is simply absent —
either a rollback artefact or it was never written, and it contradicts the
earlier stand where the guard was a TCP-only whitelist. Recorded as a fact, not
explained.

What matters is what my first pass would have done: added the deny and allows
for the tenant it was reconciling, and **taken both live control planes down in
one patch**, silently, because a deny is the baseline everyone else's allow
punches through and nobody else had one.

So the deny goes in only when every `nat` address on the plane already has an
allow. Withholding leaves the plane exactly as open as it already is — not a
regression — and the tenant's condition says so out loud rather than leaving an
open transit plane to be discovered from an ACL listing. Three related edges
came with it: router-port addresses are not tenant addresses (counting them
keeps an allow alive after its owner is gone), an allow whose source cannot be
read is left alone (not understanding a rule is not a reason to delete it), and
when the live set cannot be read at all nothing is pruned.

The fixture is modelled on the live plane: two foreign `nat` addresses, empty
ACLs. Mutating the withholding away turns it red with "it wrote the baseline and
took the stranger's control plane with it".

### The plane is open, and who closes it

Stated plainly because it is the current state of a production-shaped stand:
**`cp-transit` carries no ACLs at all.** Anything on that subnet can reach
anything else on it. Two live tenants hold addresses there and neither has a
permission written for it, so the baseline cannot go in without taking them
down, and this operator withholds it rather than choosing for somebody.

The user's decision: the operator writes the missing permissions for the
tenants it can **attribute**, and keeps withholding for anything it cannot.

Attributed, never guessed. The address names its tenant — by label, or by the
`cpt-eip-<tenant>` name both writers use — and everything else is read off that
tenant's own control-plane Service: the address it answers on, and the ports it
publishes. A Talos tenant's Service carries trustd and a cloud-init one's does
not; the clock is added when the tenant's time Service exists. Nothing comes
from a shape this operator believes a tenant ought to have, which is the same
rule as reading announce-eligibility off the datapath rather than off a label.

What that gives is convergence rather than a one-off: as tenants come under the
operator the unattributable set empties, and the plane closes by itself on the
pass where it can be closed safely. The alternative — writing the two rules by
hand — leaves the next foreign address to reopen it with nobody watching.

### What is *not* on this plane yet, and should not be pretended

`6444` — the host API a tenant's CSI talks to — appears **nowhere in the
reference**: not in its transit ports, not anywhere in the backend. Measured on
the stand instead of assumed: the tenant's CSI driver runs inside the tenant
cluster and its kubeconfig points at `https://10.198.175.250:6443`, the Talos
VIP on **mgmt**. The guard only pushes `10.199.0.0/22` out the transit leg, so
that traffic takes the default route to the border.

So today the storage control path *does* depend on the gateway — which is
exactly what putting it on this plane is meant to fix, and it is target design
(the lab plan's T13: a private per-VPC URL, `VIP:6444` with `tls-server-name`),
not something to port. Named here so the gap is a decision rather than a
discovery.

### Live: the plane closed, and the leg proved by removing the alternative

The operator's first pass over a tenant did the whole thing on the stand:

```
cp-transit acls: 0 -> 13
  3000 drop          ip4.src == {10.199.1.0/24, 10.199.2.0/23}
  3200 allow-related .1.24 -> .0.104   6443 8132 50001 tcp, 123 udp   (cmp3)
  3200 allow-related .1.4  -> .0.100   6443 8132 50001 tcp, 123 udp   (uat-t1, backfilled)
  3200 allow-related .1.5  -> .0.101   6443 8132 50001 tcp, 123 udp   (uat-t2, backfilled)
```

Both live tenants kept their nodes throughout — the point of writing their
permissions in the same patch as the baseline. And the datapath was checked in
the router rather than in the CRs, which is where a rule that reports ready can
still be absent:

```
lr-nat-list cmp-net     snat 10.199.1.24  10.200.16.0/22
lr-policy-list cmp-net  30000  ip4.dst == 10.199.0.0/22  allow
```

**Then the acceptance that matters, done by removing the alternative rather than
observing survival.** A tenant prefix is announced to the border, so a control
plane reached "successfully" proves nothing while a gateway path exists — the
reply can come back that way, which is exactly the dependency this plane is
meant to remove. So the external leg was taken off `cmp-net` entirely: no
default route, and the `cmp-net-external` router port gone. From a pod inside
the VPC:

```
own VIP    10.199.0.104:6443   open
own VIP    10.199.0.104:8132   open
own VIP    10.199.0.104:123u   open, and answered — receive time stamp 21:29:10
other VIP  10.199.0.100:6443   closed   <- the deny is in force
other VIP  10.199.0.100:123u   no reply
own VIP    10.199.0.104:9999   closed   <- only the ports it was given
internet   1.1.1.1:443         closed   <- there is no gateway path at all
```

The clock is in the table on purpose: it is the port the old guard dropped,
and its absence presents as a node that never joins with nothing naming the
time. Asked for with an NTP query rather than a port scan, so what is recorded
is a server answering, not a port state guessed from silence.

The third line is the one that proves the source: the deny only matches traffic
from `10.199.1.0/24`, so a refusal on the neighbour's VIP means the packets are
arriving as `10.199.1.24` — the transit address — and not as the pod's own. The
fourth says the permission is scoped to the ports it was written for, and the
fifth says none of this is riding a gateway. The leg was put back afterwards and
the internet returned.

### A gap this found: the network can be attached but not detached

Setting `externalPlane.attachments` back to one entry did nothing to the VPC.
The renderer merges — additively, on purpose, so adopting a network the product
already built is a no-op — and nothing removes a leg or a route it no longer
declares. The A/B was done by editing the Vpc by hand, which is not a way to run
a fabric.

It is the same "on but not off" shape as the teardown gaps: every switch in this
migration has been easier to turn on than off, and each time the missing half
has only shown up when somebody needed it. Logged as the next thing owed to the
network controller rather than folded in here.

## Hygiene: the suite stops being cluster-admin

Three live runs have been spent on a verb the code needed and the role did not
grant — `datavolumes/source` for the clone gate, `create` on secrets for the
machine token, a Role that could not be created because the writer did not hold
what it was granting. None of them could fail in a suite where every request is
cluster-admin, which is what envtest hands out.

So the manager now runs impersonating the ServiceAccount the chart gives it,
with the generated `config/rbac/role.yaml` installed and bound. The tests keep
an admin client: they play CDI finishing an import, MetalLB handing out an
address, kube-ovn allocating one — that is the cluster's work, not the
operator's, and it is not what is under test.

Getting the split wrong the first time was instructive. With the tests sharing
the manager's client, 68 tests failed on five verbs — `datavolumes/status`,
`services/status`, `nodes`, `ovn-eips/status`, `ipaddresspools` — and every one
was a test pretending to be another controller, not a controller missing a
right. The right line is: **the test may be the cluster; the operator may not.**

With the line drawn there the suite is green, which is the useful answer: the
role already grants everything the controllers actually do. From here a missing
verb fails in the suite instead of on the stand.

## M12d: the publication that never published

`_ensure_cp_reachable_in_vpc` built a SwitchLBRule to expose a tenant's
control-plane ClusterIP inside its own VPC. It has had **no callers** since the
address model changed, and kube-ovn rejects the rule anyway — the VIP is outside
the subnet the rule lives on. 119 lines, plus its name helper and its plural
constant.

The removal half was still wired in, though, in two places: the create-failure
cleanup and the teardown list. Deleting a cleanup is only safe if there is
nothing left to clean, so that was measured rather than assumed — the stand
carries nine SwitchLBRules and **all nine are kube-ovn's own vpc-dns rules**,
not one tenant control-plane rule among them. Nothing is orphaned by taking the
cleanup out with the thing it cleaned up after.

Gone with them: the tests that exercised the dead path, and the two rows in the
chart's RBAC contract that granted create/patch/delete on `switch-lb-rules`.
That last one is the part worth noticing — the permission outlived the caller,
which is how a role ends up wider than the code that justified it.

## M12d: a network can now be detached, and one leg cannot

The gap the live A/B found: `externalPlane.attachments` could add a leg and
never remove one, so detaching meant editing the Vpc by hand. The renderer
merges on purpose — adopting a network the product built has to write nothing —
so "live minus wanted" is not the answer: it would delete another writer's work,
which is the thing the merge exists to prevent.

The third input is the record of what was applied last time, which the status
already keeps. An entry goes only when it **was ours and is no longer wanted**;
a network adopted from the product has an empty record, so nothing of its is
touched until this operator has written it once itself. The default route is
withdrawn the same way, matched on the next hop it was written with, so somebody
else's default route through another gateway stays.

### The leg that cannot be withdrawn

A reviewer caught what this would otherwise have created: the tenant controller
**attaches** the control-plane leg, because a tenant's workers reach their
control plane over it. Give the network controller the power to withdraw it and
one leg has two writers with opposite intentions — the flap landing on the one
path that must not flap.

So a leg a live tenant is living behind is not this object's to take back,
however the declaration reads. Scoped deliberately: only the control-plane leg,
and only while a tenant declares this network. An egress leg *can* be taken away
from a tenant — that is a deliberate loss of internet, which is the entire point
of the two planes being separate — but losing the other one is losing the
cluster.

Both directions are tested, and mutating the hold away turns the second red with
"it took the control-plane leg from under a tenant". A third thing fell out of
writing the test: a declaration naming an egress subnet it no longer attaches
contradicts itself, and the controller already refuses it — the test was wrong,
not the code.

### Live: detached through the declaration, and the one leg that would not go

Yesterday's A/B needed a hand edit of the Vpc because the CR could only attach.
Done properly now, with a live tenant behind the network:

```
declared: attachments []  egressSubnet ""
before:   extra ["cp-transit","external"]   default route via 10.199.4.254
after:    extra ["cp-transit"]              no static routes
          lrp cmp-net-external   gone
          lrp cmp-net-cp-transit 10.199.1.23   still there
```

The egress leg went with its route and its router port. The control-plane leg
stayed, because a tenant declares this network — and the tenant behind it did
not notice: 2/2 workers, `TransitReady=Wired` throughout. The legs were put back
afterwards and the two product tenants were untouched the whole time.

That is the refusal doing its job in the only way that counts: the declaration
asked for both legs to go, and one of them is not the declaration's to remove.

### And the harness, checked on all three of its cases

The RBAC harness was proved on one gap; the other two are the ones that actually
cost live runs, so they were run too — through `hack/mutate.sh`, which is the
point of having it:

```
role.yaml less `datavolumes/source`        -> controller package FAILs
role.yaml less rbac `create`               -> "clone grant … not found"
role.yaml less `secrets` create            -> the live error, in the suite
```

All three now fail here instead of on a cluster. The direct-call tests did not
need a restricted client after all: the manager-driven ones reach those paths.

## M12e (first slice) — one renderer for the addons

There were two, and they did not agree.

`tenants_addons` builds the release the create path writes; `tenant_reconciler`
builds one of its own when a release is missing. Same object, different content:

```
                       create path              repair path
kubeConfig secret      <t>-admin-kubeconfig     <t>-kubeconfig
kubeConfig key         super-admin.svc          value
install.disableWait    true                     absent
remediation            retries 5                retries 5, retryInterval 30s
labels                 tenant, addon            + reconciler-managed
```

Both kubeconfig secrets exist on the stand and both keys are real — I checked,
having assumed otherwise. The difference that matters is `disableWait`, and the
comment explaining it is in the file that has it: without it the CNI install
waits for workloads that the install is supposed to cause, times out, Flux
remediates by uninstalling, and the release sits in `uninstalling` for ever.
Measured on the lab with zero nodes registered.

The repair path omits it — and fires **only when a release is missing**, which
is the state a fresh tenant is in, which is exactly when omitting it wedges. All
five live releases carry the create path's shape, so the repair has not written
one yet; it is a trap set rather than a fire lit.

So: one renderer, and the acceptance is byte parity rather than resemblance —
"the same HelmRelease as through the old UI" is what the plan asks for and it is
now a table both suites read, built from the catalogue the stand carries and all
four addons it offers, including alloy's config substitution and the CSI
driver's namespace. Removing `disableWait` from the port turns three of the four
red.

## M12e (second slice) — what has to be placed inside the tenant

One object, against a different API server, and it cannot be written until the
tenant's own control plane answers — which is why the product does it from a
timer and why this is a phase of its own rather than part of the create path.

Talos hands a worker one token for two jobs. trustd authenticates the machine
with it, and the kubelet uses the same value as a kubeadm bootstrap credential —
and that half needs a `bootstrap-token-<id>` Secret in the **tenant's**
kube-system, which Kamaji does not create even though it creates the RBAC
around it.

Its absence names nothing, which is what makes it worth a condition of its own:
the signer issues the certificate, apid and the kubelet both report healthy, and
the cluster has no node at all — because the kubelet's TLS bootstrap has nothing
to authenticate with and never files a CSR.

Three refusals came out of writing it, and each is a different kind:

* a control plane that is not answering yet is **waited for**, not failed — that
  is what a cold tenant looks like and it fixes itself;
* a machine token that is not `id.secret` is **refused** — nothing about it
  improves by coming back, and the tenant does not get dialled for something
  that cannot be derived;
* the credential is written **once**. It never rotates, and rewriting it would
  invalidate what every existing worker holds.

The tenant's API is reached by the in-cluster address out of the admin secret,
never the external one: those name an ingress host this process has no reason to
resolve or route to.

Tested against a stand-in for the tenant cluster rather than a second envtest.
What is under test is which object is placed, where, and when — a real second
API server would be testing controller-runtime. Removing `auth-extra-groups`
from the port turns it red: without that group the CSR is filed by somebody the
auto-approvers do not recognise, which is the same invisible failure one step
along.

### The bound on somebody else's API server

Everything in that phase talks to a different cluster, and a control plane that
accepts connections and never answers is neither an error nor a refusal — it is
silence. Without a bound, one tenant in that state holds this controller's pass,
and with it every other tenant's, for as long as it stays that way. A rolling
control plane and a cut VIP path both look like it.

Bounded twice, because the two cover different things: `context.WithTimeout`
around the calls, and `Timeout` on the client itself, since a request that never
gets a response header is not covered by anything the caller passes. Ten
seconds — nothing here is urgent, the credential is placed once and the pass
returns every ten seconds anyway.

The mutation is the nicest demonstration in this log: taking the deadline out
does not turn the test red, it makes it **hang** — `panic: test timed out after
5m0s` — which is exactly what the silent tenant would do to the controller. It
also showed that `hack/mutate.sh` had no bound of its own, so a mutant that
hangs hung the script; it passes `-timeout` now.

## M12e (third slice) — the addons are written by one thing now

The renderer from the first slice is wired, and the two writers it replaces are
retired behind `OPERATOR_TENANT_ADDONS_ENABLED` — the create path and the
reconcile loop's repair, gated separately, because a flag covering one of two
writers is worse than no flag: it reads as handed over while the other keeps
writing.

Two decisions worth their own lines.

**The catalogue's required components are installed whether or not they were
asked for.** A tenant without its CNI is not a smaller tenant; its nodes never
go Ready and nothing on the page says why. Required is not a default the caller
may drop, and the chain is expressed as Flux's own `dependsOn` — namespaces,
then the CNI, then everything else — rather than by ordering the writes, because
the ordering has to hold across restarts and this operator's write order does
not.

**A stuck release is named, not counted.** The plan asks for a tenant with an
undeployable addon to go Degraded with a reason while its neighbours keep
reconciling, and the useful thing to say is *which* release is stuck and what
Flux said about it. The test asserts the message names the failing one and does
not name the one that installed.

### An intermittent, recorded rather than smoothed

One full run of the controller suite failed on `TestUnderlayHealsTheGatewayLabel`
— the label came back, the counter had not caught up inside twenty seconds. It
passes three times out of three alone and two full runs since have been clean.

The plausible mechanism is mine: this slice adds a HelmRelease informer and a
1600-line CRD to the suite, which makes every startup and every pass a little
slower, and that assertion was already close to its edge. Not chased further
because two clean runs is thin evidence either way — but the output is kept, so
the next occurrence can be diffed against this one instead of re-argued.

### Two credentials, two opposite disciplines

The storage driver's credential goes into the tenant beside the kubelet's, and
they are handled deliberately differently.

The bootstrap token is **the** credential: written once, never rewritten,
because rotating it invalidates what every existing worker holds. The storage
one is a **copy** of a credential that lives on the host, so it is kept in
step — a stale copy is a driver that cannot reach the host API, and every volume
it is asked for fails with an authentication error that says nothing about a
secret.

Kept in step, not rewritten: an unchanged copy is left alone, because this runs
on every pass. And a tenant with no storage is not given one — the host side is
absent for those, and absence there is not a failure to report.

Both mutations land where they should: stopping the update leaves the copy stale
*and* rewrites one that had not changed, and the test says both.

### A soak that does not measure a moving tree

The suite is five minutes a run now, so a series of them outlives any single
call and has to be left in the background — where it would otherwise be
measuring a tree still being edited, which has already invalidated one series
here. `hack/soak.sh` copies the tree first, like the mutation script, runs N
times keeping every run's output, and stops at the first that is not green.

### The sediment, found on the stand rather than in the plan's summary

The plan lists "addon namespace sediment after Disable — clean up after
itself (a measured finding)". Here is the measurement, still true today:

```
uat-t1 namespaces list: [default, tigera-operator, kube-system, uat-t1-alloy]
uat-t1 releases:        calico, kubevirt-csi-driver, namespaces
```

Alloy was enabled and later disabled. Disabling deleted its HelmRelease and left
`uat-t1-alloy` in the list of namespaces the tenant's cluster should have,
because the thing that added the entry only ever added.

And the entry is wrong on its own terms. The enable-later path prefixes the
namespace with the tenant name while the release's `targetNamespace` is
unprefixed — so the namespaces chart created `uat-t1-alloy` while alloy
installed into `alloy`. The tenant carries an empty namespace named after
something it does not have, and never got the namespace it did use (it relied on
`createNamespace` instead). Two conventions in one list, visible above: three
unprefixed entries and one prefixed.

Rendering the whole set every pass makes the list follow by construction — there
is no add-only path to leave anything behind. What needed saying out loud is the
release: an addon no longer wanted has its release retired, and only ever ours,
by the label this operator puts on them. A release in that namespace nobody here
wrote belongs to somebody else.

### Predicting an adoption, which found the defect before the cluster did

Before adopting a live tenant, the useful question is what the operator *would*
write to it. Asked read-only, against `uat-t1`: render what this operator would
produce for its three releases and diff against what is there. Three kinds of
answer came back, and only one of them was a surprise.

**The sediment**, expected: the namespaces list loses `uat-t1-alloy`. That is the
correction, not a regression.

**A parameter**, expected once seen: the live CSI release carries
`infraStorageClassName: ceph-block` where rendering from catalogue defaults
gives `""`. Nothing is wrong with either — the tenant was created with that
parameter, and an adopting CR has to carry it. That is a requirement of the
adoption procedure rather than a defect, and it is the kind of thing that would
otherwise be discovered as a storage class quietly becoming empty.

**And one defect, mine**: every live release carries
`chart.spec.reconcileStrategy: ChartVersion`, which Flux defaults in and nothing
here renders. Writing the spec wholesale strips it, Flux writes it back, and the
two rewrite each other for ever — a resourceVersion that never settles and
nothing changing. Exactly the shape kube-ovn's route defaults produce, in a
different object, and the reason `MergeRoutes` exists; the addon writer needed
the same and did not have it.

So the spec is now laid over what is there rather than replacing it — deep,
because the defaults arrive nested; lists replaced, because half of somebody
else's list is not a value anybody chose. The test drives three passes over a
release Flux has defaulted and asserts resourceVersion does not move.

Worth naming as method: this cost one read-only query and found a write loop
that would have run on the first adopted tenant. The diff before the write is
the cheapest thing in this migration.

## The flake had a mechanism, and the first fix made it worse

Five soak runs, five green, at 305 seconds each — and the timing is the part
worth reading: within a third of a second across five runs. A machine under load
does not do that. So "the addon informer made everything slower and pushed the
assertion over" was wrong, and the flake had to be a race in the thing itself.

It is. The heal counter accumulates **in memory** — `Status.LabelHeals += healed`
— and is persisted by the status write at the end of the pass. When that write
loses a conflict the reconcile requeues, the next pass re-reads the object,
finds the label already correct, and has nothing left to count. The increment is
gone for good. So the number is not "how many heals happened" but "how many
heals whose status write happened to land", and its entire purpose is to be
evidence that something else keeps rewriting that label.

**The obvious fix made it worse, and an existing test said so immediately.**
Retrying the status write on conflict means re-applying a status computed from
the read that lost — and for a counter that goes *backwards*:

```
timed out waiting for the heal to be counted: labelHeals = 1, was 2
```

Two heals had been counted; the retry wrote back one. A lost increment is a
number that is too small; last-writer-wins on a counter is a number that is
wrong in both directions.

So the generic retry is reverted, with the reason written where somebody would
otherwise add it again, and the counter is incremented against a fresh read by
the controller that owns it. A count is not a fact: a fact can be recomputed
from the world on the next pass, and a count cannot.

## Preparing the adoption: the same lesson, on the object that matters most

The addon writer needed a merge because Flux defaults `reconcileStrategy` into
a HelmRelease. Asking the same question of the other objects, before adopting
anything, gave a worse answer.

The live `KamajiControlPlane` carries thirteen spec fields. This renders eight.
The five it does not — `controlPlaneEndpoint`, `controllerManager`, `kine`,
`registry`, `scheduler` — are Kamaji's own, including the endpoint the control
plane settled on. Writing the spec wholesale strips all five on the first
adopted tenant.

So the merge moved to `internal/kube`, where writing objects lives, and the
control plane and the Cluster are laid over what is there. `KubevirtCluster`
turned out safe by construction — that writer only touches labels and the
managed-by annotation — which is worth knowing rather than assuming.

Two smaller things fell out of writing the test, both of them the test being
wrong rather than the code:

* it asserted on `kine.image`, which is not in the CRD schema, so the API
  server pruned it and the assertion was about a field that had never been
  stored;
* it drove the reconciler directly while the running manager reconciled the
  same tenant — two writers, and "resourceVersion did not move" cannot be
  asserted with two. The tenant is paused in that test now, so the call under
  test is the only writer.
