"""Underlay fabric for VPC egress gateways.

A VPC created through the wizard needs a physical path out: a ProviderNetwork
on a dedicated NIC, a Vlan, and an external Subnet reachable through a NAD.
The rest of the UI *looks* for that fabric (`network._find_infra_subnet`) and
never builds it, so on a fresh cluster a wizard-created VPC comes up attached
to nothing — which is why it was applied by hand from the lab's `vpc-bgp/`
manifests.

Everything here is idempotent: run it once per cluster, re-run it freely.

Two of the objects are workarounds, not architecture, and are labelled as
such (`kubevirt-ui.io/workaround`) with the condition that retires them. They
exist because of behaviour in kube-ovn and Cilium that may be fixed upstream;
when it is, delete them rather than inheriting them forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field, field_validator

from app.core.auth import User, require_admin, require_auth
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.core.operator import (
    OPERATOR_GROUP,
    OPERATOR_VERSION,
    underlay_path_enabled,
)

logger = logging.getLogger(__name__)
router = APIRouter()

INFRA_SUBNET_LABEL = {"kubevirt-ui.io/purpose": "infrastructure"}
MANAGED_LABEL = {"kubevirt-ui.io/managed": "true"}

WORKAROUND_LABEL = "kubevirt-ui.io/workaround"
WORKAROUND_REASON = "kubevirt-ui.io/workaround-reason"
WORKAROUND_REMOVE_WHEN = "kubevirt-ui.io/workaround-remove-when"

LINK_WATCHER_NAME = "provider-link-up"
CILIUM_EXEMPT_NAME = "cilium-gateway-exempt"

EXTERNAL_GW_LABEL = "ovn.kubernetes.io/external-gw"

# Where to find an image that is certainly present on the gateway nodes: the
# kube-ovn CNI runs there, and it carries iproute2 because it uses it itself.
KUBEOVN_CNI_DAEMONSET = "kube-ovn-cni"
# Only if that lookup fails. Public and therefore rottable — the previous
# default stopped serving a layer and the watcher never started.
_FALLBACK_WATCHER_IMAGE = "docker.io/library/busybox:1.36"

# The ProviderNetwork controller initialises the OVS bridge per node and only
# then reports the node ready, so right after a create there is nothing to
# label yet. Measured at ~20s on the lab.
_READY_NODE_POLL_SECONDS = 2
_READY_NODE_POLL_ATTEMPTS = 20


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class VpcUnderlayRequest(BaseModel):
    """What the fabric needs to know about this site's physical network."""

    interface: str = Field(
        ...,
        description=(
            "Dedicated NIC for the provider network, e.g. 'eth1'. Must NOT be "
            "the node's management interface: kube-ovn enslaves it into "
            "br-external and migrates its address to the bridge, which on "
            "Talos does not hold — the node loses its address and goes "
            "NotReady until it is rebooted."
        ),
    )
    external_cidr: str = Field(..., description="CIDR of the external segment")
    external_gateway: str = Field(..., description="Gateway address on that segment")
    vlan_id: int = Field(
        0, ge=0, le=4094,
        description=(
            "VLAN id, 0 for untagged. Note that tagged frames do not always "
            "survive an overlay underneath (measured on OpenNebula VXLAN: two "
            "pods on different workers could not ARP each other while "
            "untagged frames between the same NICs were fine)."
        ),
    )
    exclude_nodes: list[str] = Field(
        default_factory=list,
        description="Nodes without the dedicated NIC — typically control planes.",
    )
    exclude_ips: list[str] = Field(
        default_factory=list,
        description="Ranges of the external CIDR kube-ovn must not allocate from.",
    )
    provider_network_name: str = "external"
    vlan_name: str = "vlan-external"
    subnet_name: str = "ext-sub"

    # --- workarounds, opt-in per site --------------------------------------
    link_watcher: bool = Field(
        True,
        description=(
            "Deploy the provider-link-up DaemonSet. kube-ovn raises the "
            "provider NIC once at bridge init and never rechecks; on Talos it "
            "drops back DOWN minutes later, and a down provider NIC is the "
            "most misleading failure in this stack — OVS lists the port, the "
            "bridge mapping is right, pods get addresses, and every frame is "
            "swallowed. Turn off where the link is known to stay up."
        ),
    )
    link_watcher_image: str = Field(
        "",
        description=(
            "Image for the link watcher. Empty means reuse the image kube-ovn's "
            "own CNI DaemonSet is running, which is already pulled on exactly "
            "the nodes this watcher runs on and carries iproute2. A named "
            "public image is a dependency that can rot: "
            "mirror.gcr.io/library/busybox:1.36 stopped serving one of its "
            "layers and the watcher sat in ImagePullBackOff — desired 3, "
            "ready 0 — which looks like a healthy DaemonSet in every summary."
        ),
    )
    cilium_source_ip_exempt: bool | None = Field(
        None,
        description=(
            "Deploy the cilium-gateway-exempt DaemonSet. Only needed when "
            "Cilium runs in chaining mode: it enforces that an endpoint emits "
            "only its own source address, and an egress gateway is a router "
            "forwarding replies from the whole internet, which Cilium drops "
            "as 'Invalid source ip'. Left unset it is decided by looking at "
            "the cluster: Cilium's own ConfigMap says whether it chains."
        ),
    )
    cilium_namespace: str = ""
    cilium_image: str = "quay.io/cilium/cilium:v1.20.0"

    @field_validator("interface", "provider_network_name", "vlan_name", "subnet_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v.strip()


class UnderlayObject(BaseModel):
    kind: str
    name: str
    namespace: str = ""
    state: str  # created | exists | failed | skipped
    detail: str = ""
    workaround: bool = False


class VpcUnderlayResponse(BaseModel):
    objects: list[UnderlayObject]
    ready: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Builders — kept pure so the shapes are testable without a cluster
# ---------------------------------------------------------------------------

def build_provider_network(data: VpcUnderlayRequest) -> dict[str, Any]:
    spec: dict[str, Any] = {"defaultInterface": data.interface}
    if data.exclude_nodes:
        spec["excludeNodes"] = list(data.exclude_nodes)
    return {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "ProviderNetwork",
        "metadata": {"name": data.provider_network_name, "labels": dict(MANAGED_LABEL)},
        "spec": spec,
    }


def build_vlan(data: VpcUnderlayRequest) -> dict[str, Any]:
    return {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Vlan",
        "metadata": {"name": data.vlan_name, "labels": dict(MANAGED_LABEL)},
        "spec": {"id": data.vlan_id, "provider": data.provider_network_name},
    }


def subnet_provider(data: VpcUnderlayRequest, kubeovn_ns: str) -> str:
    """The `<nad>.<ns>.ovn` string tying the subnet to its NAD.

    Structural, and compared character for character by kube-ovn: get it
    wrong and the egress gateway is refused outright with "please set correct
    provider of subnet ... to get the network-attachment-definition".
    """
    return f"{data.subnet_name}.{kubeovn_ns}.ovn"


def build_external_nad(data: VpcUnderlayRequest, kubeovn_ns: str) -> dict[str, Any]:
    """NAD for the external subnet, in the namespace where gateways run.

    kube-ovn attaches a gateway's external interface through Multus rather
    than the primary CNI, so without this the gateway cannot be created.
    """
    return {
        "apiVersion": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {
            "name": data.subnet_name,
            "namespace": kubeovn_ns,
            "labels": dict(MANAGED_LABEL),
        },
        "spec": {
            "config": json.dumps({
                "cniVersion": "0.3.1",
                "type": "kube-ovn",
                "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
                "provider": subnet_provider(data, kubeovn_ns),
            }),
        },
    }


def build_external_subnet(data: VpcUnderlayRequest, kubeovn_ns: str) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "protocol": "IPv4",
        "cidrBlock": data.external_cidr,
        "gateway": data.external_gateway,
        "vlan": data.vlan_name,
        "provider": subnet_provider(data, kubeovn_ns),
        # The gateways SNAT (or route) for themselves; the underlay must not.
        "natOutgoing": False,
        # The gateway address belongs to upstream kit that need not answer
        # kube-ovn's ping, and a failed check blocks the subnet.
        "disableGatewayCheck": True,
    }
    if data.exclude_ips:
        spec["excludeIps"] = list(data.exclude_ips)
    labels = {**MANAGED_LABEL, **INFRA_SUBNET_LABEL}
    return {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Subnet",
        "metadata": {"name": data.subnet_name, "labels": labels},
        "spec": spec,
    }


def _workaround_meta(name: str, namespace: str, reason: str, remove_when: str) -> dict:
    """Mark a workaround so it can be found, and say why it exists.

    The reason is prose and belongs in an annotation. It was a label value
    once, and the API server refuses those outright — spaces are not allowed,
    and 63 characters is the ceiling — so both DaemonSets came back 422 while
    the four fabric objects were created: an underlay that looked built and had
    no link watcher behind it. The label is the marker you select on; the
    sentence is the thing you read.
    """
    return {
        "name": name,
        "namespace": namespace,
        "labels": {**MANAGED_LABEL, "app": name, WORKAROUND_LABEL: "true"},
        "annotations": {
            WORKAROUND_REASON: reason,
            WORKAROUND_REMOVE_WHEN: remove_when,
        },
    }


def build_link_watcher(
    data: VpcUnderlayRequest, kubeovn_ns: str, image: str = "",
    interfaces: list[str] | None = None,
) -> dict[str, Any]:
    """DaemonSet that keeps every provider NIC administratively up.

    A workaround, deliberately labelled as one. `ip link set up` on an
    already-up interface is a no-op, so the loop costs nothing; it exists
    only because kube-ovn never rechecks the link after bridge init.

    It covers **all** provider interfaces, not just the one being built.
    There is a single DaemonSet per cluster, so a per-underlay script meant the
    second build silently disowned the first NIC: after the egress underlay was
    added, the watcher ran only `eth0.310` and `eth0.300` went down on two
    workers, taking the control-plane transit with it while OVS still showed
    the port and the subnet still read Ready. Underlays come in pairs in the
    target design, so that is the normal path.

    `image` is the resolved image — normally kube-ovn's own, discovered by the
    caller. An explicit `link_watcher_image` in the request wins over it.
    """
    wanted = list(dict.fromkeys([*(interfaces or []), data.interface]))
    watched = " ".join(wanted)
    script = (
        "while true; do\n"
        f"  for i in {watched}; do\n"
        '    ip link set dev "$i" up 2>/dev/null\n'
        "  done\n"
        "  sleep 10\n"
        "done\n"
    )
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": _workaround_meta(
            LINK_WATCHER_NAME, kubeovn_ns,
            reason="kube-ovn does not re-assert the provider link",
            remove_when=(
                "kube-ovn re-raises the provider interface after bridge init, "
                "or the node OS keeps it up on its own — then delete this "
                "DaemonSet and confirm tx counters still move on the provider "
                "NIC (ovs-ofctl dump-ports)."
            ),
        ),
        "spec": {
            "selector": {"matchLabels": {"app": LINK_WATCHER_NAME}},
            "template": {
                "metadata": {"labels": {"app": LINK_WATCHER_NAME}},
                "spec": {
                    "hostNetwork": True,
                    # Only nodes that actually carry the provider NIC.
                    "nodeSelector": {"ovn.kubernetes.io/external-gw": "true"},
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [{
                        "name": "link-up",
                        "image": data.link_watcher_image or image or _FALLBACK_WATCHER_IMAGE,
                        "securityContext": {"privileged": True},
                        "command": ["/bin/sh", "-c"],
                        "args": [script],
                        "resources": {
                            "requests": {"cpu": "5m", "memory": "16Mi"},
                            "limits": {"memory": "32Mi"},
                        },
                    }],
                },
            },
        },
    }


# Endpoint ids come first in a block; labels follow underneath, so track the
# last id seen and emit it when a gateway label shows up.
_CILIUM_EXEMPT_SCRIPT = """while true; do
  cilium-dbg endpoint list 2>/dev/null | awk '
    /^[0-9]+/            { id = $1 }
    /vpc-egress-gateway/ { if (id != "") { print id; id = "" } }
    /vpc-nat-gw/         { if (id != "") { print id; id = "" } }
  ' | sort -u | while read -r ep; do
    if cilium-dbg endpoint config "$ep" 2>/dev/null | grep -qi "SourceIPVerification *: *Enabled"; then
      echo "exempting endpoint $ep (VPC gateway)"
      cilium-dbg endpoint config "$ep" SourceIPVerification=disable 2>&1 | tail -1
    fi
  done
  sleep 15
done
"""


async def detect_cilium(k8s: Any) -> tuple[bool, str]:
    """Whether Cilium is chaining, and the namespace it lives in.

    The form defaulted the answer to "no" and the namespace to `kube-system`.
    On this cluster Cilium chains (`cni-chaining-mode: generic-veth`) and lives
    in `o0-cilium`, so the build reported the workaround as
    "skipped — not chaining Cilium" while ticking the box by hand still put the
    DaemonSet in an empty namespace. Both answers are in the cluster already.
    """
    for ns_candidate in (None,):  # search all namespaces
        try:
            cms = await k8s.core_api.list_config_map_for_all_namespaces(
                field_selector="metadata.name=cilium-config",
            )
        except Exception as e:
            logger.warning(f"Could not read cilium-config: {e}")
            return False, "kube-system"
        for cm in cms.items:
            data = cm.data or {}
            mode = (data.get("cni-chaining-mode") or "").strip().lower()
            chaining = bool(mode) and mode != "none"
            return chaining, cm.metadata.namespace
    return False, "kube-system"


def build_cilium_exempt(data: VpcUnderlayRequest) -> dict[str, Any]:
    """DaemonSet exempting VPC gateway endpoints from Cilium source-IP checks.

    Per-endpoint on purpose. The global `enable-source-ip-verification: false`
    fixes it in one line and disables anti-spoofing for every pod in the
    cluster — a bad trade when tenant worker VMs are root-accessible to their
    tenants. Selects on the label kube-ovn puts on gateway workloads, so VPCs
    created later are covered without anyone remembering.
    """
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": _workaround_meta(
            CILIUM_EXEMPT_NAME, data.cilium_namespace,
            reason="Cilium chaining drops forwarded traffic from gateway pods",
            remove_when=(
                "Cilium stops enforcing source-IP verification on kube-ovn "
                "gateway endpoints, or the cluster no longer chains Cilium — "
                "then delete this and check `cilium-dbg monitor --type drop` "
                "for 'Invalid source ip' on the gateway."
            ),
        ),
        "spec": {
            "selector": {"matchLabels": {"app": CILIUM_EXEMPT_NAME}},
            "template": {
                "metadata": {"labels": {"app": CILIUM_EXEMPT_NAME}},
                "spec": {
                    "hostNetwork": True,
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [{
                        "name": "exempt",
                        # The agent image ships cilium-dbg and talks to the
                        # local agent over its socket — no API access needed.
                        "image": data.cilium_image,
                        "securityContext": {"privileged": True},
                        "env": [
                            {"name": "CILIUM_SOCK", "value": "/var/run/cilium/cilium.sock"},
                        ],
                        "command": ["/bin/sh", "-c"],
                        "args": [_CILIUM_EXEMPT_SCRIPT],
                        "volumeMounts": [
                            {"name": "cilium-run", "mountPath": "/var/run/cilium"},
                        ],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "32Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                    }],
                    "volumes": [{
                        "name": "cilium-run",
                        "hostPath": {"path": "/var/run/cilium", "type": "Directory"},
                    }],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Apply helpers
# ---------------------------------------------------------------------------

async def _ensure_cluster_obj(
    k8s, plural: str, body: dict[str, Any], kind: str,
) -> UnderlayObject:
    name = body["metadata"]["name"]
    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=plural, body=body,
        )
        return UnderlayObject(kind=kind, name=name, state="created")
    except ApiException as e:
        if e.status == 409:
            return UnderlayObject(kind=kind, name=name, state="exists")
        logger.warning(f"Underlay: could not create {kind} {name}: {e}")
        return UnderlayObject(kind=kind, name=name, state="failed", detail=str(e.reason))


async def _ensure_namespaced_obj(
    k8s, group: str, version: str, plural: str, body: dict[str, Any], kind: str,
) -> UnderlayObject:
    name = body["metadata"]["name"]
    ns = body["metadata"]["namespace"]
    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group=group, version=version, namespace=ns, plural=plural, body=body,
        )
        return UnderlayObject(kind=kind, name=name, namespace=ns, state="created")
    except ApiException as e:
        if e.status == 409:
            return UnderlayObject(kind=kind, name=name, namespace=ns, state="exists")
        logger.warning(f"Underlay: could not create {kind} {ns}/{name}: {e}")
        return UnderlayObject(
            kind=kind, name=name, namespace=ns, state="failed", detail=str(e.reason),
        )


async def _ready_nodes(k8s, provider_network_name: str, *, wait: bool) -> list[str]:
    """Nodes whose OVS bridge for this provider network came up.

    Optionally waits, because the caller has just created the ProviderNetwork
    and the controller has not visited the nodes yet.
    """
    attempts = _READY_NODE_POLL_ATTEMPTS if wait else 1
    for attempt in range(attempts):
        try:
            pn = await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="provider-networks", name=provider_network_name,
            )
        except ApiException:
            return []
        nodes = pn.get("status", {}).get("readyNodes") or []
        if nodes or attempt == attempts - 1:
            return list(nodes)
        await asyncio.sleep(_READY_NODE_POLL_SECONDS)
    return []


async def _kubeovn_cni_image(k8s, kubeovn_ns: str) -> str:
    """The image kube-ovn's own CNI DaemonSet runs, or "" if unreadable."""
    try:
        ds = await k8s.apps_api.read_namespaced_daemon_set(
            name=KUBEOVN_CNI_DAEMONSET, namespace=kubeovn_ns,
        )
    except ApiException as e:
        logger.warning(f"Underlay: could not read {KUBEOVN_CNI_DAEMONSET}: {e}")
        return ""
    containers = ds.spec.template.spec.containers or []
    return containers[0].image if containers else ""


def _daemonset_state(ds) -> UnderlayObject:
    """Whether the DaemonSet is doing anything, not whether it exists.

    Both ways it can exist and do nothing are the same class of failure this
    fabric keeps producing: scheduled nowhere (no node carries the label), or
    scheduled and never started (the image stopped being pullable). Either
    reads as a healthy DaemonSet in every summary that counts objects.
    """
    status = ds.status
    desired = getattr(status, "desired_number_scheduled", 0) or 0
    ready = getattr(status, "number_ready", 0) or 0
    meta = ds.metadata
    if desired == 0:
        return UnderlayObject(
            kind="DaemonSet", name=meta.name, namespace=meta.namespace,
            state="failed", workaround=True,
            detail="scheduled on no node — nothing matches its nodeSelector",
        )
    if ready == 0:
        return UnderlayObject(
            kind="DaemonSet", name=meta.name, namespace=meta.namespace,
            state="failed", workaround=True,
            detail=f"0/{desired} pods ready — check image pulls and events",
        )
    return UnderlayObject(
        kind="DaemonSet", name=meta.name, namespace=meta.namespace,
        state="exists", workaround=True, detail=f"{ready}/{desired} ready",
    )


async def _all_provider_interfaces(k8s) -> list[str]:
    """Default interface of every ProviderNetwork in the cluster.

    Per-node overrides (`customInterfaces`) are included too — a node that
    carries the NIC under a different name still needs it raised.
    """
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="provider-networks",
        )
    except ApiException as e:
        logger.warning(f"Could not list ProviderNetworks for the link watcher: {e}")
        return []

    names: list[str] = []
    for item in result.get("items", []):
        spec = item.get("spec", {}) or {}
        if spec.get("defaultInterface"):
            names.append(spec["defaultInterface"])
        for custom in spec.get("customInterfaces") or []:
            if custom.get("interface"):
                names.append(custom["interface"])
    return list(dict.fromkeys(names))


async def _heal_gateway_labels(k8s, provider_network_name: str) -> tuple[list[str], list[str], list[str]]:
    """Bring `external-gw` back to `true` on every ready node of the network.

    Returns (already correct, healed now, could not touch).

    The label is not set once and kept. On the lab it was found sitting at an
    explicit `false` on all three workers, with nothing in `managedFields`
    claiming it — so whoever writes it is not necessarily us, and arguing about
    the author does not bring the underlay back. What matters is that the
    DaemonSet that keeps the provider links up selects on this label: no label,
    no pod, and `kubectl rollout status` still says "successfully rolled out"
    because zero desired pods are all ready.

    Nothing then reports anything wrong until the links go down on their own —
    which took two hours, and took the transit and egress planes with them.
    Hence: re-check on every read, not only at build time.
    """
    nodes = await _ready_nodes(k8s, provider_network_name, wait=False)
    ok, healed, failed = [], [], []
    for node_name in nodes:
        try:
            node = await k8s.core_api.read_node(name=node_name)
            if (node.metadata.labels or {}).get(EXTERNAL_GW_LABEL) == "true":
                ok.append(node_name)
                continue
            await k8s.core_api.patch_node(
                name=node_name,
                body={"metadata": {"labels": {EXTERNAL_GW_LABEL: "true"}}},
            )
            healed.append(node_name)
            logger.warning(
                "Underlay: %s had %s != true; restored. The link watcher "
                "selects on it and schedules nowhere without it.",
                node_name, EXTERNAL_GW_LABEL,
            )
        except ApiException as e:
            logger.warning(f"Underlay: could not label node {node_name}: {e}")
            failed.append(node_name)
    return ok, healed, failed


async def _label_gateway_nodes(k8s, provider_network_name: str) -> UnderlayObject:
    """Mark the nodes that carry the provider NIC.

    Everything that has to land on those nodes selects on this label — the
    link watcher here, and kube-ovn's own gateway placement. Without it the
    link-watcher DaemonSet is created, schedules nowhere, and reports 0/0
    desired: the fabric looks complete and nothing is watching the link.

    Reported as fabric rather than as a workaround: a provider network whose
    nodes are unlabelled cannot host a gateway at all.
    """
    nodes = await _ready_nodes(k8s, provider_network_name, wait=True)
    if not nodes:
        return UnderlayObject(
            kind="NodeLabel", name=EXTERNAL_GW_LABEL, state="failed",
            detail=(
                f"ProviderNetwork/{provider_network_name} reports no ready nodes. "
                "Check that the interface exists on the workers and that the OVS "
                "bridge initialised (ProviderNetwork .status.conditions)."
            ),
        )

    labelled, failed = [], []
    for node_name in nodes:
        try:
            node = await k8s.core_api.read_node(name=node_name)
            if (node.metadata.labels or {}).get(EXTERNAL_GW_LABEL) == "true":
                continue
            await k8s.core_api.patch_node(
                name=node_name,
                body={"metadata": {"labels": {EXTERNAL_GW_LABEL: "true"}}},
            )
            labelled.append(node_name)
        except ApiException as e:
            logger.warning(f"Underlay: could not label node {node_name}: {e}")
            failed.append(node_name)

    if failed:
        return UnderlayObject(
            kind="NodeLabel", name=EXTERNAL_GW_LABEL, state="failed",
            detail=f"could not label: {', '.join(failed)}",
        )
    return UnderlayObject(
        kind="NodeLabel", name=EXTERNAL_GW_LABEL,
        state="created" if labelled else "exists",
        detail=f"{len(nodes)} node(s) carry the provider NIC: {', '.join(nodes)}",
    )


async def _ensure_daemonset(k8s, body: dict[str, Any]) -> UnderlayObject:
    name = body["metadata"]["name"]
    ns = body["metadata"]["namespace"]
    try:
        await k8s.apps_api.create_namespaced_daemon_set(namespace=ns, body=body)
        return UnderlayObject(
            kind="DaemonSet", name=name, namespace=ns, state="created", workaround=True,
        )
    except ApiException as e:
        if e.status == 409:
            # Reconcile rather than recreate: the image or the interface may
            # have changed between runs.
            try:
                await k8s.apps_api.patch_namespaced_daemon_set(
                    name=name, namespace=ns, body=body,
                )
                return UnderlayObject(
                    kind="DaemonSet", name=name, namespace=ns, state="exists",
                    workaround=True, detail="reconciled",
                )
            except ApiException as patch_exc:
                logger.warning(f"Underlay: could not patch DaemonSet {ns}/{name}: {patch_exc}")
                return UnderlayObject(
                    kind="DaemonSet", name=name, namespace=ns, state="failed",
                    workaround=True, detail=str(patch_exc.reason),
                )
        logger.warning(f"Underlay: could not create DaemonSet {ns}/{name}: {e}")
        return UnderlayObject(
            kind="DaemonSet", name=name, namespace=ns, state="failed",
            workaround=True, detail=str(e.reason),
        )


# ---------------------------------------------------------------------------
# Operator path
#
# With OPERATOR_UNDERLAY_ENABLED the backend writes one object saying what the
# site's physical network looks like, and the operator keeps the fabric, the
# node label and the workaround DaemonSets in line with it. The difference that
# matters is not where the code lives: it is that the gateway label is checked
# on a watch instead of on a page view. It used to be healed by the GET below,
# so on this lab it sat at an explicit `false` on all three workers for two
# hours while the link watcher that selects on it was scheduled nowhere and
# every summary called the DaemonSet healthy.
# ---------------------------------------------------------------------------

UNDERLAY_PLURAL = "managedunderlays"


def underlay_cr_name(data: VpcUnderlayRequest) -> str:
    """The custom resource is named after the subnet it builds.

    Derived rather than allocated: the subnet name is what the rest of the UI
    already looks the fabric up by, it is unique per cluster because the Subnet
    itself is, and a name nobody has to allocate is a name two concurrent
    requests cannot disagree about.
    """
    return data.subnet_name


def build_underlay_cr(data: VpcUnderlayRequest, kubeovn_ns: str) -> dict[str, Any]:
    """The request, as the object the operator reconciles.

    Cilium is passed through exactly as asked, including "unset": the operator
    decides that one by reading the cluster, and a backend that resolved it
    first would freeze today's answer into the object.
    """
    spec: dict[str, Any] = {
        "interface": data.interface,
        "externalCIDR": data.external_cidr,
        "externalGateway": data.external_gateway,
        "vlanID": data.vlan_id,
        "providerNetworkName": data.provider_network_name,
        "vlanName": data.vlan_name,
        "subnetName": data.subnet_name,
        "kubeOVNNamespace": kubeovn_ns,
        "linkWatcher": data.link_watcher,
    }
    if data.exclude_nodes:
        spec["excludeNodes"] = list(data.exclude_nodes)
    if data.exclude_ips:
        spec["excludeIPs"] = list(data.exclude_ips)
    if data.link_watcher_image:
        spec["linkWatcherImage"] = data.link_watcher_image
    if data.cilium_source_ip_exempt is not None:
        spec["ciliumSourceIPExempt"] = data.cilium_source_ip_exempt
    if data.cilium_namespace:
        spec["ciliumNamespace"] = data.cilium_namespace
    if data.cilium_image:
        spec["ciliumImage"] = data.cilium_image
    return {
        "apiVersion": f"{OPERATOR_GROUP}/{OPERATOR_VERSION}",
        "kind": "ManagedUnderlay",
        "metadata": {"name": underlay_cr_name(data), "labels": dict(MANAGED_LABEL)},
        "spec": spec,
    }


async def read_underlay_cr(k8s, name: str) -> dict[str, Any] | None:
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            plural=UNDERLAY_PLURAL, name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _condition(cr: dict[str, Any], kind: str) -> dict[str, Any]:
    for cond in ((cr.get("status") or {}).get("conditions") or []):
        if cond.get("type") == kind:
            return cond
    return {}


def response_from_cr(cr: dict[str, Any]) -> VpcUnderlayResponse:
    """The operator's status, in the shape the wizard already reads.

    The four fabric objects are reported from one condition rather than
    enumerated, because that is exactly what the condition means: the operator
    sets FabricReady only once all four exist. Inventing four separate states
    the operator does not publish would be a nicer-looking answer that is not
    an answer.
    """
    spec = cr.get("spec") or {}
    status = cr.get("status") or {}
    kubeovn_ns = spec.get("kubeOVNNamespace") or ""

    fabric_cond = _condition(cr, "FabricReady")
    fabric_ok = fabric_cond.get("status") == "True"
    fabric_state = "exists" if fabric_ok else "failed"
    fabric_detail = "" if fabric_ok else fabric_cond.get("message", "")

    objects = [
        UnderlayObject(
            kind="ProviderNetwork", name=spec.get("providerNetworkName", ""),
            state=fabric_state, detail=fabric_detail,
        ),
        UnderlayObject(
            kind="Vlan", name=spec.get("vlanName", ""),
            state=fabric_state, detail=fabric_detail,
        ),
        UnderlayObject(
            kind="NetworkAttachmentDefinition", name=spec.get("subnetName", ""),
            namespace=kubeovn_ns, state=fabric_state, detail=fabric_detail,
        ),
        UnderlayObject(
            kind="Subnet", name=spec.get("subnetName", ""),
            state=fabric_state, detail=fabric_detail,
        ),
    ]

    label_cond = _condition(cr, "NodesLabelled")
    labelled = status.get("labelledNodes") or []
    objects.append(UnderlayObject(
        kind="NodeLabel", name=EXTERNAL_GW_LABEL,
        state="exists" if label_cond.get("status") == "True" else "missing",
        detail=label_cond.get("message", ""),
    ))

    # A count that keeps climbing is the only evidence that something else is
    # still writing this label, so it is surfaced rather than buried in status.
    heals = status.get("labelHeals") or 0
    if heals:
        objects.append(UnderlayObject(
            kind="NodeLabel", name=f"{EXTERNAL_GW_LABEL} (heals)",
            state="exists",
            detail=(
                f"restored {heals} time(s) since this underlay was created. "
                "Something other than the operator writes this label; the "
                "fabric stays up either way."
            ),
        ))

    for entry in status.get("daemonSets") or []:
        state = {
            "running": "exists",
            "skipped": "skipped",
            "absent": "missing",
        }.get(entry.get("state", ""), "failed")
        objects.append(UnderlayObject(
            kind="DaemonSet", name=entry.get("name", ""),
            namespace=entry.get("namespace", ""),
            state=state, workaround=True, detail=entry.get("detail", ""),
        ))

    ready = fabric_ok and label_cond.get("status") == "True"
    if ready:
        detail = (
            f"Underlay ready — VPC egress gateways can attach. "
            f"{len(labelled)} node(s) carry the provider NIC."
        )
    else:
        reasons = [c.get("message", "") for c in (fabric_cond, label_cond)
                   if c.get("status") == "False" and c.get("message")]
        detail = "; ".join(reasons) or "The operator has not reported on this underlay yet."
    return VpcUnderlayResponse(objects=objects, ready=ready, detail=detail)


async def ensure_underlay_cr(
    k8s, data: VpcUnderlayRequest, kubeovn_ns: str,
) -> VpcUnderlayResponse:
    """Write the object and wait for the operator to answer for it.

    The wait is bounded and its expiry is not an error: the fabric is being
    built either way, and a request that hangs until a bridge comes up on three
    nodes is a worse answer than "not ready yet, here is what it says so far".
    """
    body = build_underlay_cr(data, kubeovn_ns)
    name = body["metadata"]["name"]
    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            plural=UNDERLAY_PLURAL, body=body,
        )
    except ApiException as e:
        if e.status != 409:
            raise
        # Merge patch on spec only: the operator owns status, and replacing the
        # whole object would race its writes.
        await k8s.custom_api.patch_cluster_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            plural=UNDERLAY_PLURAL, name=name,
            body={"spec": body["spec"]},
            _content_type="application/merge-patch+json",
        )

    for attempt in range(_READY_NODE_POLL_ATTEMPTS):
        cr = await read_underlay_cr(k8s, name)
        if cr is None:
            await asyncio.sleep(_READY_NODE_POLL_SECONDS)
            continue
        status = cr.get("status") or {}
        # Only a status generated from the spec just written says anything
        # about it. Reading a stale one would report the previous request's
        # verdict as this one's.
        fresh = status.get("observedGeneration") == (cr.get("metadata") or {}).get("generation")
        response = response_from_cr(cr)
        if fresh and (response.ready or attempt == _READY_NODE_POLL_ATTEMPTS - 1):
            return response
        await asyncio.sleep(_READY_NODE_POLL_SECONDS)

    cr = await read_underlay_cr(k8s, name)
    if cr is None:
        return VpcUnderlayResponse(
            objects=[], ready=False,
            detail=f"ManagedUnderlay/{name} was written but has not been read back yet.",
        )
    return response_from_cr(cr)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/underlay", response_model=VpcUnderlayResponse)
async def ensure_vpc_underlay(
    request: Request, data: VpcUnderlayRequest,
    user: User = Depends(require_admin),
) -> VpcUnderlayResponse:
    """Build (idempotently) the physical path VPC egress gateways need.

    Order matters: the Vlan references the ProviderNetwork, and the Subnet
    references both the Vlan and the NAD. A failure at any step is reported
    per object rather than aborting, because a half-built fabric is easier to
    finish than to diagnose from a single error.
    """
    k8s = request.app.state.k8s_client
    from app.api.v1.network import _find_kubeovn_namespace
    kubeovn_ns = await _find_kubeovn_namespace(k8s)

    if underlay_path_enabled():
        # One writer per object: with this on, the backend writes intent and
        # nothing else, so the four objects below are the operator's alone.
        return await ensure_underlay_cr(k8s, data, kubeovn_ns)

    # Both Cilium answers come from the cluster unless the caller insisted.
    chaining, cilium_ns = await detect_cilium(k8s)
    if data.cilium_source_ip_exempt is None:
        data.cilium_source_ip_exempt = chaining
    if not data.cilium_namespace:
        data.cilium_namespace = cilium_ns

    objects: list[UnderlayObject] = [
        await _ensure_cluster_obj(
            k8s, "provider-networks", build_provider_network(data), "ProviderNetwork",
        ),
        await _ensure_cluster_obj(k8s, "vlans", build_vlan(data), "Vlan"),
        await _ensure_namespaced_obj(
            k8s, "k8s.cni.cncf.io", "v1", "network-attachment-definitions",
            build_external_nad(data, kubeovn_ns), "NetworkAttachmentDefinition",
        ),
        await _ensure_cluster_obj(
            k8s, "subnets", build_external_subnet(data, kubeovn_ns), "Subnet",
        ),
        # After the ProviderNetwork, because it is the source of the node list,
        # and before the link watcher, which selects on the label.
        await _label_gateway_nodes(k8s, data.provider_network_name),
    ]

    if data.link_watcher:
        image = await _kubeovn_cni_image(k8s, kubeovn_ns)
        objects.append(await _ensure_daemonset(
            k8s,
            build_link_watcher(
                data, kubeovn_ns, image,
                # Every provider NIC in the cluster, not just this build's —
                # there is one DaemonSet, and rewriting it per underlay is how
                # the first NIC lost its keeper.
                interfaces=await _all_provider_interfaces(k8s),
            ),
        ))
    else:
        objects.append(UnderlayObject(
            kind="DaemonSet", name=LINK_WATCHER_NAME, namespace=kubeovn_ns,
            state="skipped", workaround=True,
            detail="link_watcher=false — provider NIC assumed to stay up",
        ))

    if data.cilium_source_ip_exempt:
        objects.append(await _ensure_daemonset(k8s, build_cilium_exempt(data)))
    else:
        objects.append(UnderlayObject(
            kind="DaemonSet", name=CILIUM_EXEMPT_NAME, namespace=data.cilium_namespace,
            state="skipped", workaround=True,
            detail=(
                "not needed — Cilium is not chaining on this cluster"
                if not chaining else
                "cilium_source_ip_exempt=false — asked for explicitly"
            ),
        ))

    failed = [o for o in objects if o.state == "failed"]
    ready = not failed
    detail = (
        "Underlay ready — VPC egress gateways can attach."
        if ready
        else "Some objects failed: " + ", ".join(f"{o.kind}/{o.name}" for o in failed)
    )
    logger.info(f"VPC underlay ensure: ready={ready} ({detail})")
    return VpcUnderlayResponse(objects=objects, ready=ready, detail=detail)


@router.get("/underlay", response_model=VpcUnderlayResponse)
async def get_vpc_underlay(
    request: Request,
    provider_network_name: str = "external",
    vlan_name: str = "vlan-external",
    subnet_name: str = "ext-sub",
    user: User = Depends(require_auth),
) -> VpcUnderlayResponse:
    """Report which pieces of the underlay exist.

    Answers the question a wizard user actually has — "will a VPC I create
    here have a way out?" — without them reading four object types by hand.
    """
    k8s = request.app.state.k8s_client
    from app.api.v1.network import _find_kubeovn_namespace
    kubeovn_ns = await _find_kubeovn_namespace(k8s)

    if underlay_path_enabled():
        cr = await read_underlay_cr(k8s, subnet_name)
        if cr is not None:
            # Note what this branch does *not* do: heal. The operator does that
            # on a watch, and a reader that also repaired would hide how long
            # the label had been wrong — which is the number worth seeing.
            return response_from_cr(cr)
        # No object yet. Fall through and describe whatever is actually there,
        # so a fabric applied by hand still shows up instead of reading as
        # absent.

    async def _cluster_state(plural: str, name: str, kind: str) -> UnderlayObject:
        try:
            await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural=plural, name=name,
            )
            return UnderlayObject(kind=kind, name=name, state="exists")
        except ApiException as e:
            state = "missing" if e.status == 404 else "failed"
            return UnderlayObject(
                kind=kind, name=name, state=state, detail="" if e.status == 404 else str(e.reason),
            )

    objects = [
        await _cluster_state("provider-networks", provider_network_name, "ProviderNetwork"),
        await _cluster_state("vlans", vlan_name, "Vlan"),
        await _cluster_state("subnets", subnet_name, "Subnet"),
    ]

    # A fabric whose nodes are unlabelled hosts nothing, and the omission is
    # invisible from the objects above — the DaemonSet exists at 0/0 desired.
    #
    # Reading also repairs. The label drifted to an explicit `false` on the lab
    # and stayed there; the link watcher had nowhere to run, and two hours later
    # both provider links were down and had taken transit and egress with them.
    # Reporting that after the fact is not much use when a read can just fix it.
    try:
        ok, healed, failed = await _heal_gateway_labels(k8s, provider_network_name)
        nodes = await k8s.core_api.list_node(label_selector=f"{EXTERNAL_GW_LABEL}=true")
        names = [n.metadata.name for n in nodes.items]
    except ApiException as e:
        objects.append(UnderlayObject(
            kind="NodeLabel", name=EXTERNAL_GW_LABEL, state="failed", detail=str(e.reason),
        ))
    else:
        if failed:
            detail = f"could not label: {', '.join(failed)}"
            state = "failed"
        elif healed:
            detail = (
                f"restored on {', '.join(healed)} — the label had drifted and the "
                "link watcher was scheduled nowhere"
            )
            state = "created"
        elif names:
            detail = f"{len(names)} node(s): {', '.join(names)}"
            state = "exists"
        else:
            detail = ("no node carries the provider NIC label — gateways and the "
                      "link watcher have nowhere to run")
            state = "missing"
        objects.append(UnderlayObject(
            kind="NodeLabel", name=EXTERNAL_GW_LABEL, state=state, detail=detail,
        ))

    # Found by their marker label rather than by name in a known namespace:
    # the Cilium one lives wherever Cilium does, which this endpoint is never
    # told, so it used to be skipped entirely and never appeared here at all.
    try:
        found = await k8s.apps_api.list_daemon_set_for_all_namespaces(
            label_selector=f"{WORKAROUND_LABEL}=true",
        )
        seen = {ds.metadata.name: ds for ds in found.items}
    except ApiException as e:
        logger.warning(f"Underlay: could not list workaround DaemonSets: {e}")
        seen = {}

    for ds_name, default_ns in ((LINK_WATCHER_NAME, kubeovn_ns), (CILIUM_EXEMPT_NAME, "")):
        ds = seen.get(ds_name)
        if ds is None:
            objects.append(UnderlayObject(
                kind="DaemonSet", name=ds_name, namespace=default_ns,
                state="missing", workaround=True,
            ))
            continue
        objects.append(_daemonset_state(ds))

    # The workarounds are optional; the fabric is what decides readiness.
    fabric = [o for o in objects if not o.workaround]
    ready = all(o.state == "exists" for o in fabric)
    missing = [f"{o.kind}/{o.name}" for o in fabric if o.state != "exists"]
    return VpcUnderlayResponse(
        objects=objects,
        ready=ready,
        detail=(
            "Underlay present."
            if ready
            else "Missing: " + ", ".join(missing) + ". VPC egress gateways "
                 "cannot attach until these exist — POST to this path to build them."
        ),
    )
