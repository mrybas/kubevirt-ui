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
