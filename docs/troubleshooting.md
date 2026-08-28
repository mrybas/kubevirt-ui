# Troubleshooting

Diagnoses that cost real time to find, kept in one place so the next person
spends minutes instead of an afternoon. Each entry names the **symptom as it
actually appears**, because the expensive part is almost never the fix — it is
that the symptom points somewhere else.

Add to this file rather than starting a runbook per case.

---

## A tenant node never joins

**Check the clock first — before the network.**

```
talosctl -n <node-ip> time
```

Talos does not start the kubelet until its clock is synchronised, and it
reports that as:

```
waiting for time sync
```

Nothing in the logs names the clock. The node boots, the VM is Running, the
Machine says `InfrastructureReady=True`, and the node simply never appears —
which is indistinguishable from a routing or firewall fault, and is where half
a day went in T8 before anyone looked at the time.

**Why it happens:** a worker in an isolated VPC has no egress until a gateway
or routed leg exists, so a public NTP pool is unreachable. Time is served from
the tenant's own control-plane VIP on the transit plane (`<vip>:123/udp`)
precisely so joining never depends on the internet plane.

**If the clock is the problem, check in this order:**

| # | Check | Expected |
|---|---|---|
| 1 | `kubectl get svc -n tenant-<t> <t>-ntp` | `LoadBalancer`, external IP = the tenant's CP VIP |
| 2 | `kubectl get deploy -n kubevirt-ui-system kubevirt-ui-ntp` | at least one replica Ready |
| 3 | transit ACL on `cp-transit` | an `allow-related` with `udp.dst == 123` for this tenant's EIP |
| 4 | the machine config | `machine.time.servers` starts with the tenant's VIP |

Step 3 is the one that hides: the transit guard is a whitelist, and it listed
TCP ports only until T22. A published NTP service that the guard drops looks
exactly like no NTP service at all.

---

## A Talos node boots, is "running and ready", and no node appears

Read the guest console before anything else:

```
kubectl logs -n tenant-<t> virt-launcher-<vm>-xxxxx -c guest-console-log --tail=40
```

Talos names its own stage there. Two very different stalls look identical from
the outside:

```
[talos] task startAllServices (1/1): service "kubelet" to be "up"
pulling image ghcr.io/siderolabs/kubelet:v1.32.1: starting...
fetch failed ... dial tcp 140.82.121.34:443: i/o timeout host=ghcr.io
```

That is **not** the clock — it is the kubelet image. Talos pulls it at runtime
from ghcr.io, so a worker in a VPC with no egress cannot start a kubelet even
with its time perfectly synchronised. Measured as a clean A/B on one node,
changing only the VPC's default route:

| egress | result |
|---|---|
| removed | no `waiting for time sync` anywhere; stuck on the image pull |
| restored | `machine is running and ready`, node joined, tenant Ready |

So "the join must survive an egress outage" needs a registry reachable from
the tenant network — a mirror or a pull-through cache — not just local NTP.
Time was one soft dependency on the internet plane; the registry is the next
one, and it is larger.

---

## A server answers nothing — check its endpoints before its configuration

An address that resolves, accepts packets and never replies looks exactly like
a daemon refusing to serve. Check this first:

```
kubectl get endpointslice -n <ns> -l kubernetes.io/service-name=<svc>
```

Empty endpoints and a healthy pod almost always mean the Service and the pods
are in **different namespaces**. A Service selects only pods beside it; the
selector matching by label is not enough.

This cost most of an afternoon on the tenant NTP service: it was created in
the tenant namespace while chrony runs in `kubevirt-ui-system`, so the address
was announced, the ACL passed it, the pods were Ready — and every query timed
out. The diagnosis went into chrony's configuration and found two genuine but
unrelated bugs on the way down before reaching the empty endpoint list.

Order to work in: endpoints → the Service's own selector and ports → the
server's configuration. It is the cheapest check and it is the one that
distinguishes "nothing is listening for you" from "something refused you".

---

## `kubectl auth can-i` says no and the action plainly works

Subresources need the flag, not a slash:

```
kubectl auth can-i create datavolumes --subresource=source -n <ns> --as=<subject>   # yes
kubectl auth can-i create datavolumes/source          -n <ns> --as=<subject>   # no
```

The second form answers **no** for a subject that holds the permission. It is
the more natural thing to type, it produces a confident wrong answer, and
nothing about the output suggests the query was malformed.

Which is worse than having no check at all: a tool that lies in the direction
of "you lack this" sends the diagnosis toward RBAC when RBAC is fine. When the
datapath and the check disagree, the datapath wins — a DataVolume that cloned
successfully is proof of permission no `can-i` can overrule.

Cross-namespace clones have **two** subjects, and they are asked about
separately:

| what creates the DataVolume | subject CDI evaluates |
|---|---|
| the backend, directly | the backend's ServiceAccount |
| KubeVirt, from a VM's `dataVolumeTemplate` | the **VM namespace's** `default` SA |

Granting one and not the other looks like a permission that is handled.

---

## Before measuring anything, prove the change arrived

A pod whose Deployment you just edited may still be the previous ReplicaSet.
A rollout that cannot start leaves the old pods serving, and `kubectl get
deploy` showing `1/2` reads as "scaling up", not "the new one cannot run".

```
kubectl get rs -n <ns> -l app=<app>          # old must be desired=0
kubectl get pod <pod> -o jsonpath='{.spec.containers[0].command}'
kubectl logs <pod> | head                    # does it say what the new config says?
```

Three times now a conclusion has been drawn from an object the change never
reached: an ACL still in its old `/24` form, a shadow drop that a reconcile had
not yet removed, and a chrony pod serving the image's built-in configuration
(`Selected source time.cloudflare.com`) while the ConfigMap said `local
stratum 10`. In every case the measurement was correct and meaningless.

---

## A VPC is reachable from the management network

Every VPC on the routed plane carries a baseline deny at priority 3300 —
`drop / to-lport / ip4.src == <node>/32` per cluster node. A VPC that answers
a plain `curl` from a node is missing it.

```
kubectl get subnet <vpc>-default -o jsonpath='{.spec.acls[*].priority}' | tr ' ' '\n' | sort -u
```

The rule is written at create time and re-applied by the isolation reconciler,
which runs when a VPC is created or deleted — not on a timer. A VPC that
predates the baseline stays open until the next reconcile.

Deliberately open VPCs carry `kubevirt-ui.io/isolation: disabled`. Absence of
that annotation is **not** consent: it means no choice was recorded, and the
reconciler will isolate.

---

## A peering exists, shows Active, and carries nothing

Check for the allow, not the route:

```
kubectl get subnet <vpc>-default -o json \
  | jq '.spec.acls[] | select(.match | contains("<peer-cidr>"))'
```

A peering writes the link and the routes. Isolation still drops the peer's
prefix unless an `allow-related` above it says otherwise, and that allow is
derived from the peering list — so if it is missing, the isolation pass has
not run since the peering was created.

Overlapping prefixes are a different failure with the same appearance: the
objects are written on both routers and no packet crosses, because each router
already has a more specific route for the other's range. The UI refuses those
at creation; a peering made by hand can still have them.

---

## Announcements exist but the upstream router has no route

`Network → BGP Peering` shows what was **handed to FRR**, which is intent, not
proof. Whether the router accepted a prefix is a fact only the router holds:

```
birdc show route         # BIRD
vtysh -c "show ip bgp"   # FRR
```

Check `config_errors` on that page first. FRR keeps its last good
configuration when it rejects a new one, so the session stays Established and
the existing prefixes keep flowing — only newly added VPCs go unannounced, and
nothing else shows it.

---

## `ip route` looks like a route with no next hop

```
10.200.8.0/22 proto bird metric 32
```

That is an ECMP route whose next hops are on the following, tab-indented
lines. `grep '^10.200'` drops them and makes a healthy multipath route look
like a black hole. Read the full `ip route show <prefix>` output.

---

## A restore said Completed and nothing came back

Read the warnings, not the errors:

```
kubectl -n <velero-ns> get restore <name> -o jsonpath='{.status}'
# phase: Completed  errors: 0  warnings: 66  progress: 279/279
```

Velero's default `existingResourcePolicy` is `none`: an object the target
namespace already has is **left exactly as it is** and counted as a warning,
not an error, and the restore still reports Completed. Restoring a VM onto a
namespace where it is still running therefore changes nothing at all, and the
machine keeps whatever disk it had — which reads as "the backup restored an
empty disk".

The restores list in the product shows this as *Completed with warnings*, with
the count. To actually replace what is there, choose "Overwrite them from the
backup" in the restore dialog; to bring a machine back beside the live one,
restore into a different namespace.

Not the cause, though it looks like one: the restored DataVolumes still say
`source: pvc <golden image>`. They also carry
`cdi.kubevirt.io/storage.prePopulated`, which tells CDI to adopt the claim
rather than clone anything, so that source is inert.

---

## A VPC blackholes for a few seconds, every minute or so

Symptom: from inside a VM, traffic to the VPC gateway and to the external
subnet never drops, and anything beyond the border does — in bursts of a few
seconds, tens of seconds apart, with no BGP session flap and no route
withdrawal. Nothing in the control plane moves, so nothing in the product's
status or in a BGP monitor shows it.

Look at the border's neighbour table, not at BGP:

```
ip -s neigh show 10.199.4.10        # the VPC's external-subnet address
watch -n1 'ip neigh show 10.199.4.10'
```

Each outage lines up with `REACHABLE → DELAY → PROBE → INCOMPLETE` on that
entry, and the MAC never changes when it comes back: the address is fine, the
revalidation is what fails. OVN answers the initial ARP and then does not
answer the unicast probe, so the border discards the entry and traffic stops
until a broadcast lookup succeeds again.

This is the underlay, not the tenant: a static neighbour entry on the border
for the VPC's external address removes it, and is the usual fix on a lab
border. Measured in UAT run 4: ten outages in ten minutes, each two to ten
seconds, every 40–115 seconds.

## Machines take much longer to create than they should, and the CSI plugin restarts

Symptom: creating machines in batches is slow, `csi-rbdplugin` pods show a
climbing restart count, and events carry `FailedMapVolume`. Nothing in the
product reports an error — the machines do come up, eventually.

Look at what the image clones from:

```
kubectl -n <ns> get managedimage <name> \
  -o jsonpath='{.status.cloneSource}{"\n"}'
kubectl -n <ns> get managedimage <name> \
  -o jsonpath='{range .status.conditions[?(@.type=="SnapshotReady")]}{.reason}: {.message}{"\n"}{end}'
```

`cloneSource: pvc` means every clone makes CDI take a throwaway snapshot first —
twice the storage operations per machine, which is what drives the node plugin
into restarting under load. The condition says why: no VolumeSnapshotClass for
the provisioner, no CDI StorageProfile for the class, no snapshot type in the
cluster, or a snapshot that failed to be taken.

`cloneSource: snapshot` means the fast path is in effect and the slowness is
somewhere else — check whether the machine's disk is larger than the image, in
which case CDI runs an extra pod per machine purely to trigger the expansion.

Two more checks worth having:

```
kubectl get volumesnapshot -A | grep -c tmp-snapshot   # should be 0 at rest
kubectl get storageprofile <class> -o jsonpath='{.status.cloneStrategy}{"\n"}'
```
