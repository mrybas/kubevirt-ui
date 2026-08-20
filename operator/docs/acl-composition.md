# How tenant isolation is expressed, and why it stops growing

Written before the composer, because the shape of the rules decides whether the
controller that owns them is viable at all.

## What is there today

One tenant subnet on the stand, `uat-net-t1-default`, with five tenant networks
in the cluster:

```
p=3300  to-lport    drop           ip4.src == 10.198.160.1/32 … .6/32   (6 rows, one per node)
p=3200  to-lport    allow-related  ip4.src == 10.200.4.0/22            (own)
p=3200  from-lport  allow-related  ip4.dst == 10.200.4.0/22            (own)
p=3000  to-lport    drop           ip4.src == <each other tenant>/22   (5 rows)
p=3000  from-lport  drop           ip4.dst == <each other tenant>/22   (5 rows)
                                                                        18 total
```

The peer drops are enumerated. That is `2·(N−1)` rows on every subnet, so
`2·N·(N−1)` rows in the cluster — and every create and every delete rewrites the
list on every other subnet. At the 400 tenants this design is aimed at, that is
roughly 320 000 ACL rows and 400 read-modify-writes per network created.

## Why they are enumerated

Not by design. The drop used to be scoped to `TENANT_SUPERNET` alone, and it was
wrong for a measurable reason: the deployment's supernet was `10.198.192.0/18`
while the allocator handed out `10.{200+N}.0.0/24`. Two "Isolated" networks,
`acme-net` 10.100.0.0/24 and `beta-net` 10.205.0.0/24, had exactly one rule
between them — a drop of a range containing neither. Nothing blocked them. They
merely had no route yet, and the first BGP announcement would have turned
"Isolated" into a caption.

Enumerating the peers fixed that, correctly, and left the growth behind.

## What changed

The supernet and the allocator now agree. Measured on the stand:

```
TENANT_SUPERNET=10.200.0.0/14   → 10.200.0.0 .. 10.203.255.255
TENANT_VPC_PREFIX=22

10.200.0.0/22   inside
10.200.4.0/22   inside
10.200.8.0/22   inside
10.200.12.0/22  inside
10.200.24.0/22  inside
```

So `drop ip4.src == 10.200.0.0/14` denies exactly the set the five enumerated
rows deny, in one row instead of five, and it does not grow.

## The rule the composer uses

Deny the supernet once; carve the exceptions above it. The exceptions already
exist and already have priorities:

| priority | rule | why |
|---|---|---|
| 3300 | drop each management address | the node network must not open connections into a tenant |
| 3200 | allow own subnet, both directions | traffic inside the network |
| 3100 | allow each peered network | derived from peerings, which are the truth about who may talk |
| 3000 | **drop the supernet**, both directions | everything else that is a tenant |
| — | (no rule) | anything outside the supernet — the internet, shared services — stays allowed |

Semantically identical to the enumeration, and `O(1)` in the number of tenants.

## Not address sets, and not port groups

The obvious scaling answer — hoist the peer list into an OVN address set and
match against it — was considered and is not needed. kube-ovn exposes no
address-set resource in its API group (`kubectl api-resources
--api-group=kubeovn.io` lists none), so it would mean reaching past kube-ovn to
OVN directly, for a list that should not exist in the first place. A rule set
that does not enumerate its peers has nothing to hoist.

`SecurityGroup` is a real kube-ovn object and maps to OVN port groups, but it
attaches to ports rather than to a subnet's ACL list, and moving isolation there
would change which object is authoritative mid-migration. Left alone.

## The trap this must not walk back into

The supernet drop is only correct while the supernet actually contains the
tenants. That was false once and nothing noticed, so the composer does not
assume it: on every pass it checks each tenant network's CIDR against the
supernet, and any network that falls **outside** gets its own enumerated drop
alongside the aggregate. Normally that list is empty and the cost is one
comparison per network; when it is not, isolation still holds and the condition
says which network is out of range.

That is the difference between a configuration value that has to be right and
one that is checked.

## What the composer must own, and what it must not touch

Single writer of `Subnet.spec.acls` on the subnets it owns, and nothing else.
Ownership is per object, marked by an annotation, and it transfers only when the
rendered list already equals the live one — so the first reconcile after
adoption writes nothing. A live list containing a row the composer cannot
account for does not get adopted at all; it is named and left alone, because
silently dropping somebody's rule is worse than declining to manage it.
