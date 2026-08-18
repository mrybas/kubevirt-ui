"""Announce tenant VPC prefixes from their own routers, not from a gateway.

The hub model put a pair of VpcEgressGateway pods between every tenant and the
outside: the tenant's traffic was SNAT'd to a gateway address, and the gateway
announced the tenant's /22. Everything downstream inherited that pod's
lifetime — the announcement, the default route's next hop, and the BGP session
all moved when a pod was replaced.

B3 removes the pods from the path. The tenant's own router already has a leg on
the external VLAN; the border learns the tenant's /22 with **that leg as the
next hop** and routes to it directly. Traffic keeps its real addresses in both
directions (measured: `10.200.0.6 > 10.199.4.254` on the wire, and an inbound
`200` to a pod), the SNAT that serves the control-plane path is untouched
because OVN scopes SNAT per gateway port, and nothing in the data path depends
on a pod being alive.

This module renders the FRRConfiguration that carries those announcements.

**The raw form is not a preference.** frr-k8s exposes no next-hop field in its
structured API, so raw is the only way to say the one thing B3 needs. Three
lines in it were each measured, and each fails silently when missing:

  * `no bgp ebgp-requires-policy` — FRR refuses to advertise to an eBGP peer
    without an explicit policy. Without it the session comes up Established and
    advertises nothing; the only hint is `(Policy)` in `show bgp ipv4 summary`.
  * `no bgp network import-check` — a `network` statement is ignored unless
    the prefix is in the node's RIB, and a tenant /22 never is.
  * the next hop must be set on the **outbound neighbor route-map**. Setting
    it on `network <cidr> route-map X` instead advertises next-hop `0.0.0.0`,
    which the border resolves to the node — the announcement then looks
    accepted while pointing at the wrong place.

Observability has three tiers, because no single one is sufficient (T16):

  * `BGPSessionState` — is the session up (declarative, per node/peer);
  * `FRRNodeState.lastReloadResult` — did FRR accept what we generated. A bad
    config is fail-safe: FRR keeps the previous one, so live announcements
    survive — but the *new* VPC is silently not added, which is precisely the
    "attached but not announced" state this whole design exists to avoid;
  * what is actually advertised — only `show bgp ... advertised-routes` knows.
    `runningConfig` is configuration, not state: the ebgp-requires-policy trap
    looks perfectly healthy in it while nothing is announced.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from kubernetes_asyncio.client import ApiException

from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

logger = logging.getLogger(__name__)

FRRK8S_GROUP = "frrk8s.metallb.io"
FRRK8S_VERSION = "v1beta1"

CONFIG_NAME = "kubevirt-ui-b3"


def frr_namespace() -> str:
    return os.getenv("B3_FRR_NAMESPACE", "o0-metallb")


def border_peer() -> str | None:
    return os.getenv("B3_BGP_PEER") or None


def local_asn() -> int:
    return int(os.getenv("B3_LOCAL_ASN", "65030"))


def peer_asn() -> int:
    return int(os.getenv("B3_PEER_ASN", "65000"))


def external_subnet() -> str:
    return os.getenv("B3_EXTERNAL_SUBNET", "external")


def vpc_gateway() -> str | None:
    """The border's address on the external VLAN — the VPCs' default next hop.

    A different address of the same box than `border_peer()`: that one is where
    BGP is spoken, this one is where packets go. Conflating them was tempting
    and would have produced a VPC whose default route pointed at a management
    address the external plane cannot reach.
    """
    return os.getenv("B3_VPC_GATEWAY") or None


def b3_enabled() -> bool:
    """B3 needs both halves configured: somewhere to send, someone to tell."""
    return bool(border_peer() and vpc_gateway())


def announce_replicas() -> int:
    """How many nodes carry the announcement.

    Redundancy of the *announcement*, not of the path: every node advertises
    the same prefix with the same next hop (the tenant's own router leg), so
    there is no traffic to split and no ECMP to reason about. One node dying
    must not take a tenant's return path with it; that is all this is for.
    """
    try:
        return max(1, int(os.getenv("B3_ANNOUNCE_NODES", "2")))
    except ValueError:
        return 2


@dataclass(frozen=True)
class Announcement:
    """One tenant prefix and the router leg the border should send it to."""

    vpc: str
    cidr: str
    next_hop: str


def _slug(vpc: str) -> str:
    """Prefix-list name fragment — FRR wants no dots or slashes."""
    return "".join(c if c.isalnum() or c == "-" else "-" for c in vpc).upper()


def render_raw_config(
    announcements: list[Announcement], *, peer: str, asn: int, remote_asn: int,
) -> str:
    """The FRR snippet, deterministic so an unchanged input never churns the CR.

    One outbound route-map with a branch per VPC: `set ip next-hop` is only
    honoured there, and a branch each is what lets two tenants advertise the
    same prefix length toward the same peer with different next hops (measured
    live with 10.200.0.0/22 → .4.1 and 10.200.24.0/22 → .4.9).
    """
    ordered = sorted(announcements, key=lambda a: (a.vpc, a.cidr))

    lines = [
        f"router bgp {asn}",
        # Each of the next two is silent when missing — see the module docstring.
        " no bgp ebgp-requires-policy",
        " no bgp network import-check",
        f" neighbor {peer} remote-as {remote_asn}",
        f" neighbor {peer} timers 10 30",
        " address-family ipv4 unicast",
    ]
    for a in ordered:
        lines.append(f"  network {a.cidr}")
    if ordered:
        # Next hop belongs here and nowhere else.
        lines.append(f"  neighbor {peer} route-map B3-NH out")
    lines += [" exit-address-family", "!"]

    for a in ordered:
        lines.append(f"ip prefix-list PL-{_slug(a.vpc)} seq 5 permit {a.cidr}")
    lines.append("!")

    for i, a in enumerate(ordered, start=1):
        lines += [
            f"route-map B3-NH permit {i * 10}",
            f" match ip address prefix-list PL-{_slug(a.vpc)}",
            f" set ip next-hop {a.next_hop}",
        ]

    return "\n".join(lines) + "\n"


def build_frr_configuration(
    announcements: list[Announcement], nodes: list[str], *,
    peer: str, asn: int, remote_asn: int, namespace: str,
) -> dict:
    """The FRRConfiguration CR, pinned to a fixed set of nodes."""
    return {
        "apiVersion": f"{FRRK8S_GROUP}/{FRRK8S_VERSION}",
        "kind": "FRRConfiguration",
        "metadata": {
            "name": CONFIG_NAME,
            "namespace": namespace,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "nodeSelector": {
                "matchExpressions": [{
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": sorted(nodes),
                }],
            },
            "raw": {
                "priority": 10,
                "rawConfig": render_raw_config(
                    announcements, peer=peer, asn=asn, remote_asn=remote_asn,
                ),
            },
        },
    }


async def collect_announcements(k8s) -> list[Announcement]:
    """VPCs that actually leave through the external plane, and their subnets.

    Two conditions, and the second one is not optional:

    * the VPC has a **leg** on the external network — that leg is the next hop,
      so without it the border would learn a route into a black hole;
    * its **default route leaves through that same plane** — the next hop of
      `0.0.0.0/0` is an address inside the external subnet.

    A leg alone is not enough, and assuming it was announced half the lab: an
    egress gateway's own VPC has a leg, and so does every tenant still on the
    VEG hub — but a hub tenant's default route points at the gateway's transit
    address, and its traffic leaves SNAT'd from a gateway pod. Announcing its
    prefix here as well puts a second, competing path to the same /22 on the
    border while the working one is somebody else's. Measured: the generator's
    first run offered six prefixes, of which four had no business being there.
    """
    try:
        eips = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="ovn-eips",
        )
        subnets = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
        )
    except ApiException as e:
        logger.warning("B3: could not read kube-ovn objects: %s", e)
        return []

    external = os.getenv("B3_EXTERNAL_SUBNET", "external")

    external_cidr = ""
    for item in subnets.get("items", []) or []:
        if item.get("metadata", {}).get("name") == external:
            external_cidr = (item.get("spec", {}) or {}).get("cidrBlock", "")
    if not external_cidr:
        logger.warning("B3: external subnet %r has no CIDR; announcing nothing",
                       external)
        return []

    routed = await _vpcs_routed_via(k8s, external_cidr)

    legs: dict[str, str] = {}
    for item in eips.get("items", []) or []:
        spec = item.get("spec", {}) or {}
        if spec.get("type") != "lrp" or spec.get("externalSubnet") != external:
            continue
        address = (item.get("status", {}) or {}).get("v4Ip")
        # `<vpc>-external` by construction; the CR carries no vpc field for lrps.
        name = item.get("metadata", {}).get("name", "")
        vpc = name[: -len(f"-{external}")] if name.endswith(f"-{external}") else ""
        if address and vpc and vpc in routed:
            legs[vpc] = address

    out: list[Announcement] = []
    for item in subnets.get("items", []) or []:
        spec = item.get("spec", {}) or {}
        vpc = spec.get("vpc")
        cidr = spec.get("cidrBlock")
        if vpc in legs and cidr:
            out.append(Announcement(vpc=vpc, cidr=cidr, next_hop=legs[vpc]))
    return out


async def _vpcs_routed_via(k8s, external_cidr: str) -> set[str]:
    """VPCs whose default route hands traffic to the external plane.

    This is the marker of "on B3" — it is the datapath itself, not a label
    somebody has to remember to set, and it cannot drift away from reality.
    """
    import ipaddress

    try:
        net = ipaddress.ip_network(external_cidr, strict=False)
    except ValueError:
        return set()

    try:
        vpcs = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="vpcs",
        )
    except ApiException as e:
        logger.warning("B3: could not read VPCs: %s", e)
        return set()

    out: set[str] = set()
    for item in vpcs.get("items", []) or []:
        name = item.get("metadata", {}).get("name", "")
        for route in (item.get("spec", {}) or {}).get("staticRoutes") or []:
            if route.get("cidr") != "0.0.0.0/0":
                continue
            try:
                hop = ipaddress.ip_address(route.get("nextHopIP", ""))
            except ValueError:
                continue
            if hop in net:
                out.add(name)
    return out


async def pick_announce_nodes(k8s, count: int) -> list[str]:
    """Ready worker nodes, chosen deterministically so the CR does not churn.

    Control-plane nodes are excluded, and an explicit list wins over both.
    Plain `sorted()` over every node put `cp-1`/`cp-2` first, and the border
    peers with workers — so the announcement went out from nodes nothing was
    listening to and every B3 prefix silently vanished from the border while
    the CR looked perfect. Measured, on this lab, in this task.
    """
    explicit = [n.strip() for n in (os.getenv("B3_ANNOUNCE_NODE_LIST") or "").split(",") if n.strip()]
    if explicit:
        return sorted(explicit)[:count]

    try:
        nodes = await k8s.core_api.list_node()
    except ApiException as e:
        logger.warning("B3: could not list nodes: %s", e)
        return []

    ready = []
    for node in nodes.items or []:
        labels = (node.metadata.labels or {})
        if "node-role.kubernetes.io/control-plane" in labels:
            continue
        conditions = getattr(node.status, "conditions", None) or []
        if any(c.type == "Ready" and c.status == "True" for c in conditions):
            ready.append(node.metadata.name)
    return sorted(ready)[:count]


async def reload_failures(k8s, nodes: list[str]) -> dict[str, str]:
    """Nodes where FRR refused the config we generated, with its own words.

    Cheap first tier of the "announced == attached" invariant: FRR keeps the
    previous config when a reload fails, so live announcements survive — and a
    newly attached VPC silently is not added. Measured: a bad directive lands
    in `lastReloadResult` verbatim, naming the offending line.
    """
    failures: dict[str, str] = {}
    for name in nodes:
        try:
            state = await k8s.custom_api.get_cluster_custom_object(
                group=FRRK8S_GROUP, version=FRRK8S_VERSION,
                plural="frrnodestates", name=name,
            )
        except ApiException:
            continue
        status = state.get("status", {}) or {}
        for field in ("lastConversionResult", "lastReloadResult"):
            result = (status.get(field) or "").strip()
            if result and result != "success":
                failures[name] = f"{field}: {result.splitlines()[0][:200]}"
                break
    return failures


async def ensure_announcements(k8s) -> dict[str, object]:
    """Bring the FRRConfiguration in line with what the cluster actually has.

    Writes only when the rendered config differs. The alternative — patching
    every pass — reloads FRR for nothing, and a reload is the one moment a
    session can flap.

    Returns a small report the caller can log or surface; `failures` is the
    cheap tier of the "announced == attached" invariant.
    """
    peer = border_peer()
    if not peer:
        return {"skipped": "B3_BGP_PEER is not set"}

    ns = frr_namespace()
    announcements = await collect_announcements(k8s)
    nodes = await pick_announce_nodes(k8s, announce_replicas())
    if not nodes:
        return {"skipped": "no Ready nodes to announce from"}

    desired = build_frr_configuration(
        announcements, nodes,
        peer=peer, asn=local_asn(), remote_asn=peer_asn(), namespace=ns,
    )

    current = None
    try:
        current = await k8s.custom_api.get_namespaced_custom_object(
            group=FRRK8S_GROUP, version=FRRK8S_VERSION, namespace=ns,
            plural="frrconfigurations", name=CONFIG_NAME,
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning("B3: could not read %s: %s", CONFIG_NAME, e)
            return {"error": str(e)}

    if current is None:
        await k8s.custom_api.create_namespaced_custom_object(
            group=FRRK8S_GROUP, version=FRRK8S_VERSION, namespace=ns,
            plural="frrconfigurations", body=desired,
        )
        logger.info("B3: created %s announcing %d prefix(es) from %s",
                    CONFIG_NAME, len(announcements), ", ".join(nodes))
    elif current.get("spec") != desired["spec"]:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=FRRK8S_GROUP, version=FRRK8S_VERSION, namespace=ns,
            plural="frrconfigurations", name=CONFIG_NAME,
            body={"spec": desired["spec"]},
            _content_type="application/merge-patch+json",
        )
        logger.info("B3: updated %s — now %d prefix(es) from %s",
                    CONFIG_NAME, len(announcements), ", ".join(nodes))

    failures = await reload_failures(k8s, nodes)
    if failures:
        # Live announcements survive a rejected reload; new ones silently do
        # not arrive. Saying so is the whole point of this tier.
        logger.error("B3: FRR rejected the configuration on %s", failures)

    return {
        "announced": [(a.vpc, a.cidr, a.next_hop) for a in announcements],
        "nodes": nodes,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# What the upstream router has to be told
# ---------------------------------------------------------------------------

async def external_subnet_cidr(k8s) -> str:
    """CIDR of the network the VPC legs sit in — where the next hops live."""
    try:
        subnets = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
        )
    except ApiException:
        return ""
    name = external_subnet()
    for item in subnets.get("items", []) or []:
        if item.get("metadata", {}).get("name") == name:
            return (item.get("spec", {}) or {}).get("cidrBlock", "")
    return ""


def _one_announcer_note(node_cidr: str) -> str:
    """A /32 is one node. Said out loud, because the config looks complete."""
    if not node_cidr.endswith("/32"):
        return ""
    return (
        "\n# NOTE: this range covers a single node. Every tenant prefix then has\n"
        "# exactly one announcer, and losing that node withdraws all of them.\n"
        "# Widen it to the node subnet once more than one node announces.\n"
    )


def render_border_bird(*, node_cidr: str, supernet: str, prefix_len: int,
                       leg_cidr: str, local: int, remote: int) -> str:
    """BIRD stanza for the router this cluster peers with.

    Transcribed from the configuration that is live on the lab border, with
    the two lines that were paid for:

      * `graceful restart off` — with it on, BIRD kept a dead session's routes
        and built ECMP across a live and a dead next hop; half the traffic
        went into a hole.
      * `merge paths on` — several nodes announce the same prefix; without it
        BIRD installs one path and the redundancy is decorative.

    The import filter is generated from the same setting that allocates the
    VPCs, because these two disagreeing is a VPC that exists and cannot be
    reached: the filter took `{22,22}` while a VPC was carved as a /24, and
    the announcement was rejected by a router nobody thought to look at.
    """
    return f"""# Upstream side of the routed egress plane.
# Announcements arrive from the cluster nodes, but their next hop is a VPC
# router leg in {leg_cidr or "the external subnet"} — a third-party next hop.
# This router must have an interface in that network (list it under
# `protocol direct`), or every accepted route is installed as unreachable.
{_one_announcer_note(node_cidr)}
log syslog all;
router id <THIS_ROUTER_IP>;

protocol device {{ scan time 10; }}

# The leg addresses are only usable as next hops because this router is on
# their L2. List the interface that carries {leg_cidr or "the external subnet"}.
protocol direct {{
  ipv4;
  interface "<IFACE_IN_{leg_cidr or "EXTERNAL_SUBNET"}>";
}}

protocol kernel {{
  ipv4 {{ export all; import all; }};
  learn;
  # Several nodes announce the same prefix; without this only one path is
  # installed and losing that node takes the tenant down.
  merge paths on;
}}

# Only one protocol may range over these addresses. If this router already
# has a BGP protocol whose `neighbor range` covers {node_cidr}, widen that
# one instead of adding this — two overlapping ranges take each other's
# sessions and report it as "Bad peer AS".
protocol bgp k8s_nodes {{
  local as {remote};
  # A range, not a list: nodes are replaced, and a /32 per node means the
  # cluster silently has exactly one announcer.
  neighbor range {node_cidr} as {local};
  dynamic name "k8s";
  # OFF on purpose. With graceful restart on, a restarting speaker's routes
  # are kept and ECMP is built across a live and a dead next hop.
  graceful restart off;
  connect retry time 10;
  hold time 30;
  keepalive time 10;
  ipv4 {{
    import filter {{
      if net ~ [ {supernet}{{{prefix_len},{prefix_len}}} ] then accept;
      reject;
    }};
    export none;
  }};
}}

# BFD is not requested from this side, so no `protocol bfd` is needed for
# these sessions; failure detection is the 30s hold time.
#
# Verify: birdc show protocols | grep k8s
# Routes: birdc show route
"""


def render_border_frr(*, node_cidr: str, supernet: str, prefix_len: int,
                      leg_cidr: str, local: int, remote: int) -> str:
    """The same thing for a router running FRR."""
    return f"""! Upstream side of the routed egress plane.
! The next hop of each announcement is a VPC router leg in
! {leg_cidr or "the external subnet"}, not the peer address — this router needs
! an interface in that network or the routes are installed as inaccessible.
{_one_announcer_note(node_cidr).replace("#", "!")}
router bgp {remote}
  no bgp ebgp-requires-policy
  bgp listen range {node_cidr} peer-group k8s
  neighbor k8s peer-group
  neighbor k8s remote-as {local}
  neighbor k8s timers 10 30
  !
  address-family ipv4 unicast
    neighbor k8s activate
    neighbor k8s route-map tenants in
    ! Several nodes announce the same prefix — without multipath only one
    ! is installed and losing that node takes the tenant down.
    maximum-paths 4
  exit-address-family
!
ip prefix-list tenants seq 10 permit {supernet} ge {prefix_len} le {prefix_len}
route-map tenants permit 10
  match ip address prefix-list tenants
!
! Verify: vtysh -c "show bgp summary"
! Routes: vtysh -c "show ip bgp"
"""


async def node_peer_range(k8s) -> str:
    """The range the upstream should accept sessions from.

    Derived from the announcing nodes' own addresses. A single node yields a
    /32, which is honest — and is exactly the state worth widening before
    announcement redundancy means anything.
    """
    import ipaddress

    nodes = await pick_announce_nodes(k8s, announce_replicas())
    addresses: list[str] = []
    for name in nodes:
        try:
            node = await k8s.core_api.read_node(name)
        except Exception:  # noqa: BLE001 - a missing node must not kill the page
            continue
        for addr in (node.status.addresses or []):
            if addr.type == "InternalIP":
                addresses.append(addr.address)
                break

    if not addresses:
        return "<NODE_SUBNET>"
    if len(addresses) == 1:
        return f"{addresses[0]}/32"
    try:
        return str(_supernet_of([ipaddress.ip_network(f"{a}/32") for a in addresses]))
    except ValueError:
        return f"{addresses[0]}/32"


def _supernet_of(nets: list) -> object:
    """Smallest prefix covering every address — never wider than needed."""
    import ipaddress

    candidate = nets[0]
    while not all(n.subnet_of(candidate) for n in nets):
        candidate = candidate.supernet()
        if candidate.prefixlen == 0:
            break
    return ipaddress.ip_network(candidate)
