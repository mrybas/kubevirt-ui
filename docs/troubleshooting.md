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
