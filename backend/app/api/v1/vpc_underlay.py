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

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field, field_validator

from app.core.auth import User, require_admin, require_auth
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

logger = logging.getLogger(__name__)
router = APIRouter()

INFRA_SUBNET_LABEL = {"kubevirt-ui.io/purpose": "infrastructure"}
MANAGED_LABEL = {"kubevirt-ui.io/managed": "true"}

WORKAROUND_LABEL = "kubevirt-ui.io/workaround"
WORKAROUND_REASON = "kubevirt-ui.io/workaround-reason"
WORKAROUND_REMOVE_WHEN = "kubevirt-ui.io/workaround-remove-when"

LINK_WATCHER_NAME = "provider-link-up"
CILIUM_EXEMPT_NAME = "cilium-gateway-exempt"


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
    link_watcher_image: str = "mirror.gcr.io/library/busybox:1.36"
    cilium_source_ip_exempt: bool = Field(
        False,
        description=(
            "Deploy the cilium-gateway-exempt DaemonSet. Only needed when "
            "Cilium runs in chaining mode: it enforces that an endpoint emits "
            "only its own source address, and an egress gateway is a router "
            "forwarding replies from the whole internet, which Cilium drops "
            "as 'Invalid source ip'. Off by default — it is a no-op cost on "
            "clusters without Cilium."
        ),
    )
    cilium_namespace: str = "kube-system"
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


def build_link_watcher(data: VpcUnderlayRequest, kubeovn_ns: str) -> dict[str, Any]:
    """DaemonSet that keeps the provider NIC administratively up.

    A workaround, deliberately labelled as one. `ip link set up` on an
    already-up interface is a no-op, so the loop costs nothing; it exists
    only because kube-ovn never rechecks the link after bridge init.
    """
    script = (
        "while true; do\n"
        f"  ip link set dev {data.interface} up 2>/dev/null\n"
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
                        "image": data.link_watcher_image,
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
    ]

    if data.link_watcher:
        objects.append(await _ensure_daemonset(k8s, build_link_watcher(data, kubeovn_ns)))
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
            detail="cilium_source_ip_exempt=false — not chaining Cilium",
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

    for ds_name, ds_ns in ((LINK_WATCHER_NAME, kubeovn_ns), (CILIUM_EXEMPT_NAME, None)):
        if ds_ns is None:
            continue
        try:
            await k8s.apps_api.read_namespaced_daemon_set(name=ds_name, namespace=ds_ns)
            objects.append(UnderlayObject(
                kind="DaemonSet", name=ds_name, namespace=ds_ns,
                state="exists", workaround=True,
            ))
        except ApiException:
            objects.append(UnderlayObject(
                kind="DaemonSet", name=ds_name, namespace=ds_ns,
                state="missing", workaround=True,
            ))

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
