# Border router

The BGP speaker the tenant prefixes are announced to, and the only device
outside the cluster that the routed egress plane depends on. BIRD 3.2.1 on
`10.198.175.254`, config in `/etc/bird.conf`.

Everything here was learned by breaking it. Read the "what bites" section
before changing anything.

## What talks to it

| protocol | peers | what it carries |
|---|---|---|
| `b3test` | the announcer nodes, AS 65030 | one `/22` per routed VPC, next hop = that VPC's own router leg |
| `vpcgw` | VpcEgressGateway pods, AS 65010 | the hub tenants' prefixes, next hop = a gateway pod |

Both are **dynamic**: a `neighbor range` accepts sessions rather than naming
peers, and BIRD creates an instance per peer (`b31`, `vpc10`, …).

## What bites

**Overlapping ranges silently steal sessions.** A `metallb` protocol used to
range over `10.198.160.0/20` — every node — so BIRD handed it the incoming
sessions from the B3 announcers and killed them:

```
mlb3: Error: Bad peer AS: 65030
```

The announcer showed `Active`, the border showed nothing wrong, and one node's
announcements simply never arrived. Removed 2026-08-18; MetalLB runs in L2
mode here and speaks no BGP at all. **If two protocols can match the same
address, assume the wrong one wins.**

**Dynamic instances are never removed.** A gateway pod that goes away leaves
its instance retrying `No route to host` forever. Nine had accumulated by
2026-08-18 alongside three dead MetalLB ones — a wall of red that hides the one
session that matters. Neither `birdc restart <proto>` nor a reconfigure clears
them; only `systemctl restart bird` does, and that drops every session for
about fifteen seconds (they all come back on their own).

**A reconfigure can drop sessions.** `birdc configure` removes and rebuilds
the dynamic children of any protocol whose config changed. Expect a gap; the
tenant prefixes leave the kernel table until the peers reconnect.

**A route with no `via` is not broken.** ECMP prints its next hops on
following, tab-indented lines:

```
10.200.8.0/22 proto bird metric 32
	nexthop via 10.199.4.6 dev eth1.310 weight 1
	nexthop via 10.199.4.7 dev eth1.310 weight 1
```

`grep '^10.200'` drops them and makes a healthy multipath route look like a
black hole. Read `ip route show <prefix>` in full.

## Routine checks

```
birdc show protocols                    # every dynamic instance, one line each
birdc show route protocol b31           # what one announcer is sending
ip route show | grep '^10.200'          # what the kernel actually installed
journalctl -u bird --since -10min       # "Bad peer AS", range collisions
```

A healthy stand: `b3test` and `vpcgw` `Passive`, one `b3N` per announcer node,
one `vpcN` per live gateway pod, and nothing else.

## Changing the peer range

The product generates the stanza it expects — do not hand-write it:

```
GET /api/v1/bgp/gateway-config      # Network → BGP Peering → Show Config Examples
```

It derives the range from the nodes that actually announce, so the border and
the announcement generator cannot drift. A `/32` there means a single
announcer: the prefix disappears from the border when that one node reboots.
The generated config says so in a comment when it happens.

After editing:

```
bird -p -c /etc/bird.conf     # parse without touching the running daemon
birdc configure               # apply; expect the sessions to bounce
```

## Announcement redundancy

`B3_ANNOUNCE_NODES` (default 2) decides how many nodes carry the
announcement; `B3_ANNOUNCE_NODE_LIST` pins specific ones and overrides it.
Both must be matched by the border's `neighbor range` or the extra announcers
sit in `Active` forever, which looks like a network fault and is a whitelist.

Redundancy of the *announcement*, not of the path: every announcer advertises
the same prefix with the same next hop, so there is nothing to load-balance.
It exists so one node's reboot does not remove a tenant's return path.

## Known hardening gaps (not yet applied)

Decisions, not chores — each one can lock somebody out:

* `rule family="ipv4" source address="192.168.1.0/24" accept` in zone `public`
  is a blanket accept for a whole network, unscoped by service. Everything
  else in that zone is BGP-only. Narrow it or confirm it is the admin network
  and meant to be that wide.
* `10.198.160.0/20` is allowed BGP twice — once as `service name="bgp"` and
  once as `port 179/tcp`. Harmless, and a reader has to check both to know
  what is permitted.
* The masquerade and notrack rules are firewalld **direct** rules. They work
  (they are in `nat` and `raw`), but they do not appear in
  `firewall-cmd --list-all` — someone auditing the border will not see them.
  Policy objects are visible; direct rules are not.

## Tenant↔tenant

`tenant-hairpin` is a firewalld **policy object**, not a zone rule, and that
placement is deliberate: policy objects are evaluated before the zone's
`forward: yes`, which is what lets it cut traffic that enters and leaves on the
same interface. Proved functionally with a temporary log rule
(`IN=eth1.310 OUT=eth1.310`), not by reading chain order — the chain view lies
in both directions.
