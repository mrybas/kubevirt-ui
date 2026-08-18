# Border router

The BGP speaker the tenant prefixes are announced to, and the only device
outside the cluster that the routed egress plane depends on. BIRD 3.2.1 on
`10.198.175.254`, config in `/etc/bird.conf`.

Everything here was learned by breaking it. Read the "what bites" section
before changing anything.

## What talks to it

| protocol | peers | what it carries |
|---|---|---|
| `tenants` | the announcer nodes, AS 65030 | one `/22` per routed VPC, next hop = that VPC's own router leg |
| `vpcgw` | VpcEgressGateway pods, AS 65010 | the hub tenants' prefixes, next hop = a gateway pod |

The protocol was called `b3test` while B3 was an experiment; it is the
production return path now and is named `tenants`. Session instances still
appear as `b31`, `b32` — those come from `dynamic name "b3"`, not from the
protocol name.

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

A healthy stand: `tenants` and `vpcgw` `Passive`, one `b3N` per announcer node,
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

## The firewall rules do not say what is allowed

Zone `public` carries six source-scoped BGP rich rules **and** a blanket
`179/tcp` in `ports`. The blanket port wins: it admits BGP from anywhere, so
the rich rules constrain nothing. That alone would only be untidy. The trap is
the other half — measured with `ss -tnp | grep :179`, the four live sessions
come from

    10.198.160.3, 10.198.160.4    the announcer nodes
    10.199.4.6,   10.199.4.7      the VPC gateway legs (vpc1, vpc2)

and the second pair was matched by **no rich rule at all**. So a reader who
trusted the rich rules as the policy and removed the redundant-looking
`179/tcp` would have dropped both VPC gateway sessions — the return path for
every VPC that peers through `vpcgw` — while the rule list still looked
deliberate.

Applied since: the duplicate `10.198.160.0/20 port 179/tcp` rule (already
covered by the `service name="bgp"` rule for the same source) is gone, and
`10.199.4.0/24 service name="bgp"` is added. Neither changes what passes today;
together they make the rich rules describe the sessions that actually exist.

Verify before trusting the list, always against the sessions rather than the
rules:

    ss -tnp | grep ':179'                       # who is really peering
    firewall-cmd --zone=public --list-rich-rules # who is nominally allowed

### Decisions left open — each can lock somebody out

* Remove `179/tcp` from `ports`, making the rich rules load-bearing. Safe as
  of the measurement above, and only as long as it is re-taken first.
* `rule family="ipv4" source address="192.168.1.0/24" accept` is a blanket
  accept for a whole network, unscoped by service, where everything else in the
  zone is BGP-only. Narrow it, or confirm it is the admin network and meant to
  be that wide.
* Masquerade and notrack are firewalld **direct** rules. They are permanent
  (checked — a `--reload` will not drop them), but they never appear in
  `firewall-cmd --list-all`, so an audit of the border does not see the NAT
  that the whole tenant range depends on. Policy objects are visible; direct
  rules are not.

## Tenant↔tenant

`tenant-hairpin` is a firewalld **policy object**, not a zone rule, and that
placement is deliberate: policy objects are evaluated before the zone's
`forward: yes`, which is what lets it cut traffic that enters and leaves on the
same interface. Proved functionally with a temporary log rule
(`IN=eth1.310 OUT=eth1.310`), not by reading chain order — the chain view lies
in both directions.
