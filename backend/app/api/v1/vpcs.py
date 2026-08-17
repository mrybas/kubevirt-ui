"""VPC Management API endpoints (Kube-OVN).

Dedicated VPC router covering:
  - CRUD for Vpc CRD (kubeovn.io/v1)
  - VPC peering connections
  - Static route management
"""

import ipaddress
import os
import json
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.core.b3_announce import b3_enabled, external_subnet, vpc_gateway
from app.core.tenant_transit import transit_subnet_name
from app.api.v1.subnet_acls import (
    ISOLATION_PRIORITY_DROP,
    ISOLATION_PRIORITY_OWN,
    SubnetAcl,
    _acls_to_spec,
    build_isolation_acls,
    build_mgmt_deny_acls,
)
from app.api.v1.tenants_common import (
    _ensure_cluster_config,
    _host_service_cidr,
    _tenant_ns,
    _vpcdns_forward_dns,
    _vpcdns_vip,
)
from app.config import get_settings
from app.core.allocators import (
    allocate_peering_link,
    allocate_vpc_cidr,
    assert_cidr_free,
)
from app.core.auth import User, require_auth, require_admin
from app.core.constants import (
    KUBEOVN_API_GROUP,
    KUBEOVN_API_VERSION,
    KUBEOVN_SYSTEM_SUBNETS as SYSTEM_SUBNETS,
    KUBEOVN_SYSTEM_VPC as SYSTEM_VPC_NAME,
    KYVERNO_API_GROUP,
    KYVERNO_API_VERSION,
)
from app.core.errors import k8s_error_to_http
from app.core.groups import (
    get_user_namespaces,
    is_admin,
    is_env_viewer,
    is_folder_viewer,
    load_folders,
)
from app.models.vpc import (
    VpcCreateRequest,
    VpcDnsResponse,
    VpcDnsStatus,
    VpcDnsUpdateRequest,
    VpcListResponse,
    VpcPeeringCreateRequest,
    VpcPeeringInfo,
    VpcResponse,
    VpcScopeRequest,
    VpcStaticRoute,
    VpcStaticRoutesUpdateRequest,
    VpcSubnetInfo,
)

logger = logging.getLogger(__name__)
router = APIRouter()

KUBEOVN_GROUP = KUBEOVN_API_GROUP
KUBEOVN_VERSION = KUBEOVN_API_VERSION


# ============================================================================
# Helpers
# ============================================================================



def _parse_vpc(item: dict[str, Any]) -> VpcResponse:
    """Parse a Kube-OVN Vpc CR into VpcResponse."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    labels = metadata.get("labels", {})

    conditions = status.get("conditions", [])
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conditions
    )
    if not ready and status.get("standby") is not None:
        ready = status.get("standby", False) or status.get("default", False)

    name = metadata.get("name", "")
    if name == SYSTEM_VPC_NAME:
        origin = "system"
    elif labels.get("kubevirt-ui.io/managed") == "true":
        origin = "ui"
    else:
        origin = "external"

    return VpcResponse(
        name=name,
        origin=origin,
        tenant=labels.get("kubevirt-ui.io/tenant"),
        # T7 — surface folder/env scope from labels so the wizard can render
        # them on the dropdown without re-fetching label dicts client-side.
        folder=labels.get("kubevirt-ui.io/folder"),
        environment=labels.get("kubevirt-ui.io/environment"),
        enable_nat_gateway=spec.get("enableExternal", False),
        default_subnet=status.get("defaultLogicalSwitch"),
        namespaces=spec.get("namespaces", []),
        static_routes=[
            VpcStaticRoute(
                cidr=r.get("cidr", ""),
                nextHopIP=r.get("nextHopIP", ""),
                policy=r.get("policy", "policyDst"),
            )
            for r in spec.get("staticRoutes", [])
        ],
        ready=ready,
        conditions=conditions,
    )


async def _mgmt_deny_sources(k8s) -> list[str]:
    """Where "the management plane" is, for the deny that keeps it out.

    `TENANTS_MGMT_CIDR` is authoritative and takes a **list** — a management
    plane is a set of prefixes, and in one cluster it is a /10 while in another
    it is a /24 (T21).

    Without it, fall back to **each node's own address as a /32** rather than
    to a prefix length nobody knows. Autodiscovery genuinely cannot learn the
    mask: the API reports node addresses, not the network they sit on. The
    previous fallback guessed `/24` from the first node it saw and, on a lab
    whose node network is a `/20`, covered every node by coincidence — a
    security rule holding for a reason that could change with one new node.
    A `/32` per node is exact, cannot over-block, and follows the node list.

    Two traps were hit reaching this value, both worth keeping in view:

    * nothing on the VPC path populates the cluster-config cache, so it has to
      be loaded first — otherwise the deny is simply not written;
    * it must be read through the accessor, never with
      `from ... import _cluster_config`. That binds the module's value **at
      import time** — `None` — and no later assignment inside that module ever
      reaches this name. The rule then exists in the code, passes its tests,
      is present in the created object, and is absent from the cluster.
    """
    explicit = [
        c.strip() for c in (os.getenv("TENANTS_MGMT_CIDR") or "").split(",")
        if c.strip()
    ]
    if explicit:
        return explicit

    try:
        nodes = await k8s.core_api.list_node()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not block create
        logger.warning("Could not list nodes for the mgmt deny: %s", exc)
        return []

    addresses = [
        addr.address
        for node in (nodes.items or [])
        for addr in (getattr(node.status, "addresses", None) or [])
        if addr.type == "InternalIP" and addr.address
    ]
    return [f"{ip}/32" for ip in sorted(dict.fromkeys(addresses))]


def _has_isolation_acls(spec: dict[str, Any]) -> bool:
    """Whether a subnet spec carries the tenant-isolation catch-all drop.

    The drop is the rule that does the isolating — the allow rules above it
    only carve exceptions out of it — so its presence is what "isolated"
    means. Checked on the live object rather than remembered in a label, so
    an admin editing the ACLs by hand is reflected honestly.
    """
    return any(
        a.get("action") == "drop" and a.get("priority") == ISOLATION_PRIORITY_DROP
        for a in spec.get("acls", []) or []
    )


async def _get_vpc_subnets(k8s, vpc_name: str) -> tuple[list[VpcSubnetInfo], bool]:
    """Subnets belonging to a VPC, and whether any of them is isolated."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
        )
    except ApiException:
        return [], False

    subnets = []
    isolated = False
    for item in result.get("items", []):
        if item.get("spec", {}).get("vpc") == vpc_name:
            spec = item.get("spec", {})
            st = item.get("status", {})
            isolated = isolated or _has_isolation_acls(spec)
            subnets.append(VpcSubnetInfo(
                name=item["metadata"]["name"],
                cidr_block=spec.get("cidrBlock", ""),
                gateway=spec.get("gateway", ""),
                available_ips=st.get("v4availableIPs", 0),
                used_ips=st.get("v4usingIPs", 0),
            ))
    return subnets, isolated


# Provider key of the VpcDns secondary NIC. kube-ovn's CNI reads per-provider
# annotations off the pod template under this prefix.
VPCDNS_NAD_PROVIDER = "ovn-nad.default.ovn"
VPCDNS_ROUTES_ANNOTATION = f"{VPCDNS_NAD_PROVIDER}.kubernetes.io/routes"
# Subnet backing the default cluster overlay — its gateway is the next hop for
# the service network from a VpcDns pod's secondary NIC.
OVN_DEFAULT_SUBNET = "ovn-default"


async def _overlay_gateway(k8s) -> str | None:
    """Gateway of the default cluster overlay subnet (`10.16.0.1` upstream).

    Read rather than assumed: the overlay CIDR is a cluster install choice,
    and a wrong next hop here produces exactly the silent failure this whole
    route exists to prevent.
    """
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            name=OVN_DEFAULT_SUBNET,
        )
    except ApiException as exc:
        logger.warning(
            f"Could not read subnet {OVN_DEFAULT_SUBNET!r} for the VpcDns "
            f"service-network route: {exc}"
        )
        return None
    return (subnet.get("spec", {}) or {}).get("gateway") or None


async def _ensure_vpc_dns_service_route(k8s, vpc_name: str) -> bool:
    """Give a VpcDns pod a route to the service network.

    A VpcDns pod's secondary NIC gets exactly one route into the default
    overlay — `10.96.0.1/32`, set by kube-ovn-controller and not configurable
    — so the apiserver ClusterIP is reachable and the overlay subnet is
    on-link, but the kube-dns ClusterIP is not: that packet takes the pod's
    default route out into the VPC and dies there.

    That is the only reason the Corefile ever pinned CoreDNS *pod* IPs. Pod IPs
    are ephemeral, so the pin breaks on the next CoreDNS reschedule — and
    breaks silently: measured, after a `rollout restart coredns` moved the pods
    from 10.16.0.8/9 to 10.16.0.26/28, VPC DNS answered nothing while the
    VpcDns object still reported ACTIVE=true with both pods 1/1 Running.

    Adding a route for the whole service network removes the pin: forwarding
    to the stable kube-dns ClusterIP then works and nothing goes stale.
    kube-ovn's CNI honours a per-provider `routes` annotation on the pod
    template, and the VpcDns controller leaves it in place.

    Best-effort: the Deployment is created by the kube-ovn controller after the
    VpcDns CR, so on the create path it may not exist yet — the recreate
    endpoint applies it later. Returns True when the annotation is in place.
    """
    from app.api.v1.network import _find_kubeovn_namespace

    service_cidr = _host_service_cidr()
    if not service_cidr:
        logger.warning(
            f"VpcDns {vpc_name!r}: host service CIDR unknown, cannot add the "
            "service-network route; DNS in this VPC will not resolve"
        )
        return False

    gateway = await _overlay_gateway(k8s)
    if not gateway:
        return False

    kubeovn_ns = await _find_kubeovn_namespace(k8s)
    deploy_name = f"vpc-dns-{vpc_name}-dns"
    routes = json.dumps([{"dst": service_cidr, "gw": gateway}])
    patch = {
        "spec": {
            "template": {
                "metadata": {"annotations": {VPCDNS_ROUTES_ANNOTATION: routes}},
            },
        },
    }

    try:
        await k8s.apps_api.patch_namespaced_deployment(
            name=deploy_name, namespace=kubeovn_ns, body=patch,
        )
    except ApiException as exc:
        if exc.status == 404:
            logger.info(
                f"VpcDns Deployment {kubeovn_ns}/{deploy_name} not created yet; "
                "the service-network route will be applied on the next reconcile"
            )
        else:
            logger.warning(
                f"Could not annotate {kubeovn_ns}/{deploy_name} with the "
                f"service-network route: {exc}"
            )
        return False

    logger.info(
        f"VpcDns {vpc_name!r}: routed {service_cidr} via {gateway} on "
        f"{kubeovn_ns}/{deploy_name}"
    )
    return True


def _build_vpc_dns_corefile(cluster_dns_ip: str) -> str:
    """Corefile for VpcDns: one forward to the cluster's own CoreDNS.

    Everything, not just cluster.local. The public half used to go straight to
    8.8.8.8 and timed out, for a structural reason: a VpcDns pod runs in the
    kube-ovn namespace while the VPC egress gateway selects the *tenant*
    namespace, so the pod is never steered through the gateway and has no path
    to the internet at all. Cluster CoreDNS is reachable and already resolves
    the public internet, so a single forward covers both zones and drops the
    egress dependency entirely.

    The target is the kube-dns ClusterIP, which is stable — reachable only
    because `_ensure_vpc_dns_service_route` routes the service network over
    the secondary NIC. Forwarding to CoreDNS *pod* IPs instead would work
    until the next reschedule and then fail silently.
    """
    return (
        ".:53 {\n"
        "    errors\n"
        "    health\n"
        "    ready\n"
        "    cache 30\n"
        f"    forward . {cluster_dns_ip} {{\n"
        "        prefer_udp\n"
        "        policy round_robin\n"
        "    }\n"
        "    loop\n"
        "    reload\n"
        "    loadbalance\n"
        "}\n"
    )


async def _ensure_vpc_dns_prereqs(k8s) -> None:
    """Idempotently create the cluster-wide resources that VpcDns expects.

    kube-ovn upstream lets the operator define these manually; we automate
    them at first VPC create so the user doesn't have to apply YAML by hand.
    Run from `_ensure_vpc_dns` (auto-create path) AND from the recreate
    endpoint.

    Resources:
      - NetworkAttachmentDefinition `ovn-nad` in `default` ns — secondary
        NIC bridge into the default cluster overlay.
      - ConfigMap `vpc-dns-config` in kube-ovn ns — gates the VpcDns
        controller (`enable-vpc-dns: true`) and tells it which NAD + VIP
        to use.
      - ConfigMap `vpc-dns-corefile` in kube-ovn ns — CoreDNS Corefile
        mounted by EVERY VpcDns Deployment in the cluster, so its content is
        deliberately cluster-generic: one forward to the kube-dns ClusterIP.

    The matching ServiceAccount `vpc-dns` in the kube-ovn ns is shipped
    by k8s-bootstrap (static infrastructure, no runtime data).
    """
    from app.api.v1.network import _find_kubeovn_namespace
    kubeovn_ns = await _find_kubeovn_namespace(k8s)
    vip = _vpcdns_vip()
    labels = {"kubevirt-ui.io/managed": "true"}

    # 1. NetworkAttachmentDefinition (default ns, shared across VPCs)
    nad_body = {
        "apiVersion": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {"name": "ovn-nad", "namespace": "default", "labels": labels},
        "spec": {
            "config": json.dumps({
                "cniVersion": "0.3.1",
                "type": "kube-ovn",
                "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
                "provider": "ovn-nad.default.ovn",
            }),
        },
    }
    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group="k8s.cni.cncf.io", version="v1", namespace="default",
            plural="network-attachment-definitions", body=nad_body,
        )
        logger.info("Created VpcDns prereq NAD default/ovn-nad")
    except ApiException as e:
        if e.status != 409:
            logger.warning(f"VpcDns NAD ensure failed: {e}")

    # 2. ConfigMap vpc-dns-config (static gate config — create-if-missing)
    cm_config = client_v1_configmap(
        name="vpc-dns-config",
        namespace=kubeovn_ns,
        labels=labels,
        data={
            "coredns-vip": vip,
            "enable-vpc-dns": "true",
            "nad-name": "ovn-nad",
            "nad-provider": "ovn-nad.default.ovn",
            # kube-ovn turns `k8s-service-host` into the ONE /32 route it puts
            # on the VpcDns pod's secondary NIC — everything else leaves over
            # the VPC interface, where the cluster service network does not
            # exist. Left at its default (the kubernetes API address) the pod
            # can reach the API and nothing else, so every forward to the
            # kube-dns ClusterIP in the Corefile below times out and each
            # query comes back SERVFAIL. Naming the forward target here is
            # what makes the route and the Corefile agree.
            "k8s-service-host": _vpcdns_forward_dns(),
            "k8s-service-port": "53",
        },
    )
    try:
        await k8s.core_api.create_namespaced_config_map(
            namespace=kubeovn_ns, body=cm_config,
        )
        logger.info(f"Created VpcDns prereq ConfigMap {kubeovn_ns}/vpc-dns-config")
    except ApiException as e:
        if e.status == 409:
            # A ConfigMap written before the route/forward mismatch was
            # understood keeps a `k8s-service-host` pointing at the API
            # server, which silently breaks DNS in every VPC. Bring just
            # those two keys forward; the rest is the operator's to own.
            try:
                await k8s.core_api.patch_namespaced_config_map(
                    name="vpc-dns-config", namespace=kubeovn_ns,
                    body={"data": {
                        "k8s-service-host": _vpcdns_forward_dns(),
                        "k8s-service-port": "53",
                    }},
                )
            except ApiException as patch_exc:
                logger.warning(f"VpcDns config CM patch failed: {patch_exc}")
        else:
            logger.warning(f"VpcDns config CM ensure failed: {e}")

    # 3. ConfigMap vpc-dns-corefile. Note the blast radius: this one ConfigMap
    # is mounted by EVERY VpcDns in the cluster, so a change here affects all
    # VPCs at once. CoreDNS's `reload` plugin picks it up without restarting a
    # single pod. Content is stable now that it names the kube-dns ClusterIP
    # rather than pod IPs, so re-rendering is a no-op in practice.
    corefile = _build_vpc_dns_corefile(_vpcdns_forward_dns())
    cm_corefile = client_v1_configmap(
        name="vpc-dns-corefile",
        namespace=kubeovn_ns,
        labels=labels,
        data={"Corefile": corefile},
    )
    try:
        await k8s.core_api.create_namespaced_config_map(
            namespace=kubeovn_ns, body=cm_corefile,
        )
        logger.info(
            f"Created VpcDns prereq ConfigMap {kubeovn_ns}/vpc-dns-corefile "
            f"(forwarding to {_vpcdns_forward_dns()})"
        )
    except ApiException as e:
        if e.status == 409:
            try:
                await k8s.core_api.patch_namespaced_config_map(
                    name="vpc-dns-corefile", namespace=kubeovn_ns,
                    body={"data": {"Corefile": corefile}},
                )
                logger.info(
                    f"Refreshed VpcDns Corefile (forwarding to "
                    f"{_vpcdns_forward_dns()})"
                )
            except ApiException as patch_exc:
                logger.warning(f"VpcDns corefile CM patch failed: {patch_exc}")
        else:
            logger.warning(f"VpcDns corefile CM create failed: {e}")


def client_v1_configmap(*, name: str, namespace: str, labels: dict, data: dict) -> dict:
    """Tiny helper for ConfigMap bodies — avoids importing V1ConfigMap from
    kubernetes_asyncio just for two callsites."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "data": data,
    }


# ---------------------------------------------------------------------------
# Kyverno DNS-injection policy — one ClusterPolicy per custom VPC.
#
# Pods landing in a custom VPC inherit kubelet's `--cluster-dns=10.96.0.10`,
# which points at the cluster CoreDNS ClusterIP — unreachable across VPC
# boundaries. VpcDns gives us a per-VPC CoreDNS deployment fronted by a
# cluster-wide VIP (OVN SwitchLB routes it locally), but the pod has to
# be TOLD to use that VIP. We mutate pod.spec.dnsPolicy/dnsConfig at
# admission time so any workload (CDI importer, KubeVirt virt-launcher,
# user Deployments) in a VPC ns transparently resolves through VpcDns.
#
# Why per-VPC instead of one global policy:
#   - lifecycle matches the VPC (delete VPC → delete its policy),
#   - matches the UI's per-VPC view/edit story,
#   - leaves room for a future per-VPC VIP without a schema change.
# ---------------------------------------------------------------------------


def _kyverno_policy_name(vpc_name: str) -> str:
    return f"kubevirt-ui-vpc-dns-{vpc_name}"


def _build_kyverno_dns_policy(vpc_name: str, vip: str) -> dict[str, Any]:
    """Build a Kyverno ClusterPolicy that injects VpcDns into pods.

    Match strategy: pods in namespaces with `kubevirt-ui.io/managed=true`
    (excludes all system namespaces by construction). Two contexts:

      - `nsobj`: the namespace object, for its
        `ovn.kubernetes.io/logical_switch` annotation.
      - `vpcSubnets`: list of subnet names whose `spec.vpc` equals this
        VPC, fetched via apiCall so the policy follows subnet changes
        without re-rendering.

    Precondition: `nsobj`'s logical-switch annotation is in `vpcSubnets`.
    Only then mutate: set dnsPolicy=None + dnsConfig.nameservers=[vip] +
    standard cluster.local search list. `background: false` keeps the
    policy out of background scans — we mutate only on CREATE.
    """
    return {
        "apiVersion": f"{KYVERNO_API_GROUP}/{KYVERNO_API_VERSION}",
        "kind": "ClusterPolicy",
        "metadata": {
            "name": _kyverno_policy_name(vpc_name),
            "labels": {
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/vpc": vpc_name,
            },
            "annotations": {
                "policies.kyverno.io/title": f"Inject VpcDns ({vpc_name})",
                "policies.kyverno.io/category": "DNS",
                "policies.kyverno.io/description": (
                    f"Mutates pods landing on a subnet of VPC '{vpc_name}' "
                    f"to use the per-VPC VpcDns VIP ({vip}) instead of the "
                    "unreachable cluster CoreDNS ClusterIP."
                ),
            },
        },
        "spec": {
            "background": False,
            # Don't block pod admission if our context.apiCall errors out
            # (e.g. kube-ovn API briefly unreachable, RBAC drifts). DNS
            # injection is opt-in convenience; admission denial would be
            # destructive for every workload landing in a managed ns.
            "failurePolicy": "Ignore",
            "rules": [
                {
                    "name": "set-dnsconfig",
                    "match": {
                        "any": [
                            {
                                "resources": {
                                    "kinds": ["Pod"],
                                    "namespaceSelector": {
                                        "matchLabels": {
                                            "kubevirt-ui.io/managed": "true",
                                        },
                                    },
                                },
                            },
                        ],
                    },
                    "context": [
                        {
                            "name": "nsobj",
                            "apiCall": {
                                "urlPath": "/api/v1/namespaces/{{ request.namespace }}",
                            },
                        },
                        {
                            "name": "vpcSubnets",
                            "apiCall": {
                                "urlPath": f"/apis/{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}/subnets",
                                "jmesPath": f"items[?spec.vpc=='{vpc_name}'].metadata.name | @",
                            },
                        },
                    ],
                    "preconditions": {
                        "all": [
                            {
                                "key": '{{ nsobj.metadata.annotations."ovn.kubernetes.io/logical_switch" || \'\' }}',
                                "operator": "AnyIn",
                                "value": "{{ vpcSubnets }}",
                            },
                        ],
                    },
                    "mutate": {
                        "patchStrategicMerge": {
                            "spec": {
                                "dnsPolicy": "None",
                                "dnsConfig": {
                                    "nameservers": [vip],
                                    "searches": [
                                        "{{ request.namespace }}.svc.cluster.local",
                                        "svc.cluster.local",
                                        "cluster.local",
                                    ],
                                    "options": [{"name": "ndots", "value": "5"}],
                                },
                            },
                        },
                    },
                },
            ],
        },
    }


async def _ensure_vpc_dns_policy(k8s, vpc_name: str) -> None:
    """Create or patch the per-VPC Kyverno DNS-injection ClusterPolicy.

    Skipped for the default cluster VPC (`ovn-cluster`) — its pods reach
    the standard cluster CoreDNS just fine.

    Idempotent: 409 on create → switch to replace. All other errors are
    logged-and-swallowed; VPC create still succeeds. If Kyverno isn't
    installed (404 on the API group) we silently no-op — the policy is
    not load-bearing for VPC create, only for in-VPC pod DNS.
    """
    if vpc_name == SYSTEM_VPC_NAME:
        return
    vip = _vpcdns_vip()
    body = _build_kyverno_dns_policy(vpc_name, vip)
    policy_name = _kyverno_policy_name(vpc_name)
    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KYVERNO_API_GROUP, version=KYVERNO_API_VERSION,
            plural="clusterpolicies", body=body,
        )
        logger.info(f"Created Kyverno ClusterPolicy {policy_name} for VPC {vpc_name!r}")
        return
    except ApiException as e:
        if e.status == 409:
            # Already exists — patch with our latest body so VIP / labels
            # / rules stay in sync.
            try:
                await k8s.custom_api.patch_cluster_custom_object(
                    group=KYVERNO_API_GROUP, version=KYVERNO_API_VERSION,
                    plural="clusterpolicies", name=policy_name, body=body,
                    _content_type="application/merge-patch+json",
                )
                logger.info(f"Patched Kyverno ClusterPolicy {policy_name}")
            except ApiException as patch_exc:
                logger.warning(
                    f"Kyverno ClusterPolicy {policy_name} patch failed: {patch_exc}"
                )
            return
        if e.status == 404:
            # Kyverno CRDs not installed — nothing to do. Log once and move on.
            logger.info(
                "Kyverno CRDs not present; skipping VpcDns policy for VPC "
                f"{vpc_name!r}. Install the kyverno bootstrap component to "
                "enable in-VPC pod DNS injection."
            )
            return
        logger.warning(
            f"Kyverno ClusterPolicy create for VPC {vpc_name!r} failed: {e}; "
            "in-VPC pods will not have DNS until policy is created manually."
        )


async def _delete_vpc_dns_policy(k8s, vpc_name: str) -> None:
    """Delete the per-VPC Kyverno DNS-injection ClusterPolicy.

    404 (already gone, or Kyverno CRDs absent) is tolerated.
    """
    if vpc_name == SYSTEM_VPC_NAME:
        return
    policy_name = _kyverno_policy_name(vpc_name)
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KYVERNO_API_GROUP, version=KYVERNO_API_VERSION,
            plural="clusterpolicies", name=policy_name,
        )
        logger.info(f"Deleted Kyverno ClusterPolicy {policy_name}")
    except ApiException as e:
        if e.status != 404:
            logger.warning(
                f"Failed to delete Kyverno ClusterPolicy {policy_name}: {e}"
            )


async def _ensure_vpc_dns(
    k8s, vpc_name: str, subnet_name: str, *, swallow: bool = True,
) -> None:
    """Create the per-VPC VpcDns CR if it doesn't already exist.

    Per kube-ovn 1.16 docs (vpc/vpc-internal-dns/) a VpcDns is a per-VPC
    DNS deployment that forwards to cluster CoreDNS via the cluster-wide
    shared coredns-vip (autodiscovered, exposed via
    ``tenants_common._vpcdns_vip``). The VIP is consumed by workloads in
    the VPC via the subnet's ``spec.dhcpV4Options.dns_server`` (set in
    ``create_vpc`` at subnet creation time — separate from this call).

    Prereqs the upstream chart bakes in (k8s-bootstrap is expected to
    ensure-exist): the ``vpc-dns-config`` ConfigMap in ``kube-ovn`` ns
    and the ``ovn-nad`` NAD in ``default`` ns. If they're missing the
    VpcDns CR still creates but the VpcDns controller leaves the
    deployment NotReady — admin can fix prereqs and the controller
    reconciles. We don't pre-flight check here.

    409 (already exists) is ALWAYS treated as success — the helper is
    idempotent. Other failures depend on ``swallow``:

    - ``swallow=True`` (default, used by VPC create-time auto-call):
      logged as a warning and SWALLOWED. VPC create still succeeds; the
      admin can manually create the VpcDns later via the explicit
      recreate endpoint.
    - ``swallow=False`` (used by the explicit recreate endpoint): the
      `ApiException` PROPAGATES so the caller can surface a 502 to the
      admin — they explicitly asked us to (re)create it, so silent
      failure would mislead them into thinking it worked.
    """
    # Provision cluster-wide prereqs (NAD + 2 ConfigMaps with autodiscovered
    # values) before creating the CR — VpcDns controller no-ops without them.
    # Idempotent: safe to call from both the VPC create flow and the
    # explicit recreate endpoint. Errors here are non-fatal (admin can
    # debug the partial state); they just reduce the chance VpcDns reaches
    # ACTIVE on first reconcile.
    try:
        await _ensure_vpc_dns_prereqs(k8s)
    except Exception as exc:
        logger.warning(
            f"VpcDns prereq ensure for VPC {vpc_name!r} hit a non-fatal error: {exc}"
        )

    body = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "VpcDns",
        "metadata": {
            "name": f"{vpc_name}-dns",
            "labels": {
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/vpc": vpc_name,
            },
        },
        "spec": {
            "vpc": vpc_name,
            "subnet": subnet_name,
        },
    }
    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpc-dnses", body=body,
        )
        logger.info(f"Created VpcDns {vpc_name}-dns for VPC {vpc_name!r}")
    except ApiException as e:
        if e.status == 409:
            pass  # idempotent — fall through to policy ensure
        elif swallow:
            logger.warning(
                f"VpcDns auto-create for VPC {vpc_name!r} failed: {e}; "
                "admin may need manual setup. Workloads will still get an "
                "IP via the subnet's dhcpV4Options but DNS resolution via "
                "the VPC-internal DNS forwarder will not work."
            )
            return
        else:
            # swallow=False — propagate so the caller can surface a real
            # error to the user instead of pretending success.
            raise

    # Widen the VpcDns pod's secondary-NIC route from the single /32 that
    # `k8s-service-host` buys us to the whole service network, so CoreDNS can
    # reach other cluster services too.
    #
    # Best-effort, and it usually misses on a fresh VPC: the kube-ovn
    # controller creates the Deployment *after* this CR, so there is nothing
    # to patch yet and only the recreate endpoint applies it later. That race
    # is why DNS is no longer left depending on this call — `k8s-service-host`
    # in `vpc-dns-config` makes kube-ovn write the route to the forward target
    # itself, at Deployment creation, with nothing to lose.
    await _ensure_vpc_dns_service_route(k8s, vpc_name)

    # Pair the VpcDns CR with a Kyverno ClusterPolicy that mutates pods
    # landing in this VPC's namespaces to use the VpcDns VIP as DNS.
    # Without this, kubelet's --cluster-dns wins and in-VPC pods (CDI,
    # virt-launcher, user workloads) try to reach the unreachable cluster
    # CoreDNS ClusterIP. Errors are swallowed inside _ensure_vpc_dns_policy.
    await _ensure_vpc_dns_policy(k8s, vpc_name)



async def _vpc_owning_cidr(k8s, cidr: str) -> str | None:
    """The VPC whose subnet covers `cidr`, or None when nothing here owns it.

    "Nothing here" is the normal case for a corporate prefix reached over BGP:
    the allow-ACL is all we can add, and the upstream router carries the path.
    """
    try:
        wanted = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        logger.warning(f"Shared network {cidr!r} is not a CIDR; skipping peering")
        return None

    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
        )
    except ApiException as e:
        logger.warning(f"Could not list subnets to resolve {cidr}: {e}")
        return None

    for subnet in result.get("items", []):
        spec = subnet.get("spec", {}) or {}
        vpc = spec.get("vpc")
        block = spec.get("cidrBlock")
        if not vpc or not block or vpc == SYSTEM_VPC_NAME:
            continue
        try:
            other = ipaddress.ip_network(block, strict=False)
        except ValueError:
            continue
        if wanted.subnet_of(other) or other.subnet_of(wanted):
            return vpc
    return None


def _vpc_nat_gateway_enabled() -> bool:
    """Whether a VPC may own a per-VPC OVN NAT gateway. Off by default."""
    return (os.getenv("VPC_NAT_GATEWAY_ENABLED") or "").lower() in ("1", "true", "yes")


async def _peer_shared_cidrs(k8s, vpc_name: str, shared_cidrs: list[str]) -> None:
    """Peer with whichever VPCs own the shared prefixes.

    The allow-ACL only lifts the isolation drop; it does not create a route.
    Measured: a VPC created with `shared_cidrs=[10.198.200.0/24]` had the
    allow rules at priority 3100 and

        spec.staticRoutes: [{"cidr": "0.0.0.0/0", "nextHopIP": ...}]

    — nothing pointing at the shared range — so the ping went out the default
    route and died. Adding the peering by hand fixed it instantly, at two
    hops (ttl=62) instead of a hairpin through the upstream.

    Best-effort per prefix: a shared network living outside this cluster has
    no VPC to peer with and is left to the ACL plus whatever the upstream
    router knows. A failure here does not fail the VPC create — the VPC is
    already up and the peering can be added from its page.
    """
    for cidr in shared_cidrs:
        peer = await _vpc_owning_cidr(k8s, cidr)
        if not peer:
            logger.info(
                f"Shared network {cidr} is not owned by a VPC here; leaving it "
                "to the allow-ACL and upstream routing"
            )
            continue
        if peer == vpc_name:
            continue
        try:
            await _create_peering_pair(k8s, vpc_name, peer)
            logger.info(f"Peered {vpc_name!r} with {peer!r} for shared network {cidr}")
        except Exception as e:
            logger.warning(
                f"Could not peer {vpc_name!r} with {peer!r} for {cidr}: {e}. "
                "The allow-ACL is in place; add the peering from the VPC page."
            )


async def _get_vpc_peerings(k8s, vpc_name: str) -> list[VpcPeeringInfo]:
    """Peerings involving a VPC, read from `Vpc.spec.vpcPeerings`.

    That spec field is the peering — there is no separate object to look at,
    so this reports what is actually configured on the router.
    """
    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=vpc_name,
        )
    except ApiException:
        return []

    return _peerings_from_item(item, vpc_name)


def _peerings_from_item(item: dict, vpc_name: str) -> list[VpcPeeringInfo]:
    """The peerings written on one Vpc object.

    Split out of `_get_vpc_peerings` so a listing that already holds the
    object does not have to fetch it again — and, more to the point, so it
    stops reporting zero. `GET /vpcs` said `peerings: []` for a VPC whose
    detail page listed `acme-net-to-ovn-cluster`, so the Networks table read
    "Peerings 0" for every row while the routers were peered.
    """
    peerings = []
    for entry in item.get("spec", {}).get("vpcPeerings", []) or []:
        remote = entry.get("remoteVpc", "")
        connect_ip = entry.get("localConnectIP", "")
        local_ip = connect_ip.split("/")[0]
        try:
            network = ipaddress.ip_network(connect_ip, strict=False)
            hosts = [str(h) for h in network.hosts()]
            link_cidr = str(network)
            remote_ip = next((h for h in hosts if h != local_ip), "")
        except ValueError:
            link_cidr, remote_ip = "", ""

        peerings.append(VpcPeeringInfo(
            name=f"{vpc_name}-to-{remote}",
            local_vpc=vpc_name,
            remote_vpc=remote,
            link_cidr=link_cidr,
            local_connect_ip=local_ip,
            remote_connect_ip=remote_ip,
        ))
    return peerings


# ============================================================================
# VPC CRUD
# ============================================================================

@router.get("", response_model=VpcListResponse)
async def list_vpcs(
    request: Request,
    tenant: str | None = None,
    folder: str | None = None,
    environment: str | None = None,
    user: User = Depends(require_auth),
) -> VpcListResponse:
    """List all VPCs, optionally filtered by tenant or folder/env scope.

    Non-admin users see only VPCs bound to namespaces they have folder/env
    access to. The built-in ``ovn-cluster`` system VPC is always excluded for
    non-admins.

    T7 — `folder` / `environment` query params drive the tenant-create wizard
    dropdown. A VPC matches when:
      - `folder == folder` AND
      - (`environment == environment` OR the VPC has no env label set —
        a folder-wide VPC is eligible for every env under its folder)
    `environment` alone (without `folder`) is rejected as ambiguous.
    """
    if environment and not folder:
        raise HTTPException(
            status_code=400,
            detail="`environment` requires `folder` (an env is always scoped under a folder)",
        )

    k8s = request.app.state.k8s_client

    try:
        kwargs: dict[str, str] = {}
        if tenant:
            kwargs["label_selector"] = f"kubevirt-ui.io/tenant={tenant}"
        # No selector otherwise: a VPC made by CLI or GitOps is still part of
        # this cluster, and hiding it broke the attach dialog and the CIDR
        # overlap check as well as the list itself (backlog U24). Provenance
        # rides along as `origin` for the badge. Non-admins are still narrowed
        # by the access filter further down.

        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", **kwargs,
        )
    except ApiException as e:
        if e.status == 404:
            return VpcListResponse(items=[], total=0)
        raise k8s_error_to_http(e, "VPC operation")

    vpcs = []
    for item in result.get("items", []):
        vpc = _parse_vpc(item)
        vpc.subnets, vpc.isolated = await _get_vpc_subnets(k8s, vpc.name)
        vpc.peerings = _peerings_from_item(item, vpc.name)
        vpcs.append(vpc)

    # T7 — folder/env filter applied AFTER fetch (label selector with OR semantics
    # over the env label isn't expressible in K8s field selectors; in-memory is fine).
    if folder:
        vpcs = [v for v in vpcs if v.folder == folder]
    if environment:
        # Match exact env OR folder-wide VPC (no env label).
        vpcs = [v for v in vpcs if v.environment == environment or not v.environment]

    if not is_admin(user.groups, user):
        # B4 (T9): a folder-scoped VPC that was just created has an empty
        # `spec.namespaces` (no tenants attached yet), so the namespace-
        # overlap filter alone would hide the VPC from the folder admin who
        # is supposed to pick it in the wizard. OR-in a label-based check
        # against the folder access machinery: folder-viewer sees folder-wide
        # VPCs, env-viewer sees env-scoped VPCs.
        user_ns = set(await get_user_namespaces(k8s, user))
        try:
            folders_map = await load_folders(k8s)
        except HTTPException as e:
            # 404 → no folders configured yet → no label-based access; fall
            # back to ns-overlap only. Other errors bubble up.
            if e.status_code == 404:
                folders_map = {}
            else:
                raise

        def _label_accessible(v: VpcResponse) -> bool:
            if not v.folder:
                return False  # unlabeled VPC — admin-only via existing filter
            folder_meta = folders_map.get(v.folder)
            if not folder_meta:
                return False  # folder no longer exists or unparseable meta
            if v.environment is None:
                return is_folder_viewer(user, folder_meta)
            return is_env_viewer(user, folder_meta, v.environment)

        vpcs = [
            v for v in vpcs
            if v.name != SYSTEM_VPC_NAME
            and ((set(v.namespaces or []) & user_ns) or _label_accessible(v))
        ]

    return VpcListResponse(items=vpcs, total=len(vpcs))


async def _tenant_vpc_cidrs(k8s_client: Any, exclude: str | None = None) -> list[str]:
    """Default-subnet CIDRs of every managed tenant VPC except `exclude`."""
    out: list[str] = []
    try:
        subnets = await k8s_client.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
        )
    except Exception as e:
        logger.warning(f"Could not list subnets for isolation scoping: {e}")
        return out

    for item in subnets.get("items", []):
        spec = item.get("spec", {}) or {}
        vpc = spec.get("vpc")
        cidr = spec.get("cidrBlock")
        if not vpc or not cidr or vpc == SYSTEM_VPC_NAME or vpc == exclude:
            continue
        if (item.get("metadata") or {}).get("deletionTimestamp"):
            continue  # on its way out; not a peer to block
        if spec.get("vlan"):        # underlay/provider subnet, not a tenant VPC
            continue
        out.append(cidr)
    return out


async def reconcile_isolation_acls(k8s_client: Any) -> int:  # noqa: C901
    """Re-scope every isolated VPC's drop rules to the current set of peers.

    A VPC created later is a prefix the older ones have never heard of, and
    their ACLs are written as literal matches — so without this pass the new
    tenant is reachable from every tenant that predates it.
    """
    updated = 0
    mgmt_cidr = await _mgmt_deny_sources(k8s_client) if b3_enabled() else []
    try:
        subnets = await k8s_client.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
        )
    except Exception as e:
        logger.warning(f"Isolation reconcile: could not list subnets: {e}")
        return 0

    tenant_subnets = [
        i for i in subnets.get("items", [])
        if (i.get("spec", {}) or {}).get("vpc") not in (None, "", SYSTEM_VPC_NAME)
        and not (i.get("spec", {}) or {}).get("vlan")
        # A subnet being deleted is still listed — kube-ovn holds it with a
        # finalizer for a while. Counting it left every other VPC dropping a
        # prefix that no longer exists, which then blocks whoever is handed
        # that CIDR next.
        and not (i.get("metadata", {}) or {}).get("deletionTimestamp")
    ]
    supernet = get_settings().tenant_supernet

    for item in tenant_subnets:
        spec = item.get("spec", {}) or {}
        name = item["metadata"]["name"]
        existing = spec.get("acls") or []
        if not any(a.get("action") == "drop" for a in existing):
            continue                      # this VPC was created un-isolated
        shared = [
            a["match"].split("== ")[-1] for a in existing
            if a.get("action") == "allow-related"
            and a.get("priority", 0) < ISOLATION_PRIORITY_OWN
            and "== " in a.get("match", "")
        ]
        peers = [
            (s.get("spec") or {}).get("cidrBlock") for s in tenant_subnets
            if s["metadata"]["name"] != name
        ]
        acls = build_isolation_acls(
            subnet_cidr=spec.get("cidrBlock", ""),
            tenant_supernet=supernet,
            shared_cidrs=sorted(set(shared)),
            peer_cidrs=[p for p in peers if p],
        )
        # This function replaces `spec.acls` wholesale, so anything it does not
        # know about is deleted by it. The management deny is written at create
        # time by a different author, and was silently stripped here within the
        # minute — a rule present in the code, in the tests and in the created
        # object, and gone from the cluster. Both authors carry the baseline.
        acls = [*acls, *build_mgmt_deny_acls(mgmt_cidr)] if acls else acls
        if not acls:
            # Nothing left to be isolated from. Leaving the old literal drops
            # in place is not harmless: the allocator hands those very CIDRs
            # to the next VPC, so the new tenant would be born unreachable
            # from this one for a reason nobody could see in its own spec.
            # Keep the management deny: it is a drop, but not one of the
            # peer drops this branch exists to retire.
            keep = {(a.direction, a.match) for a in build_mgmt_deny_acls(mgmt_cidr)}
            body = [
                a for a in existing
                if a.get("action") != "drop"
                or (a.get("direction"), a.get("match")) in keep
            ]
            if body == existing:
                continue
            await k8s_client.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
                name=name,
                body={"spec": {"acls": body}},
                _content_type="application/merge-patch+json",
            )
            updated += 1
            continue
        await k8s_client.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
            name=name,
            body={"spec": {"acls": [a.model_dump() for a in acls]}},
            _content_type="application/merge-patch+json",
        )
        updated += 1
    return updated


@router.post("", response_model=VpcResponse, status_code=201)
async def create_vpc(request: Request, data: VpcCreateRequest, user: User = Depends(require_admin)) -> VpcResponse:
    """Create a VPC with a default subnet.

    If subnet_cidr is not provided, auto-allocates a /24 from 10.{200+N}.0.0/24.

    Also auto-creates a per-VPC VpcDns CR (forwards to cluster CoreDNS via
    the cluster-wide ``coredns-vip``) and stamps the VIP into the default
    subnet's ``spec.dhcpV4Options.dns_server`` so workloads get DNS via
    standard DHCP — no webhook, no cloud-init munging. Mirrors the solo-VM
    DNS path. If VpcDns provisioning fails (e.g. upstream prereqs missing)
    the VPC is still created and the failure is logged; admin can recover.
    """
    k8s = request.app.state.k8s_client

    # Required for `_vpcdns_vip()` below — populates the cluster-config cache
    # (autodiscovers the kube-dns ClusterIP + the VpcDns shared VIP).
    await _ensure_cluster_config(k8s)

    if data.subnet_cidr:
        cidr = data.subnet_cidr
        # A hand-picked CIDR bypasses the allocator, so nothing else has
        # checked it. Overlapping VPCs break peering routes, the CIDR-based
        # isolation ACLs, and BGP (two gateways deriving the same router-id).
        await assert_cidr_free(k8s, cidr)
        parts = cidr.split("/")[0].rsplit(".", 1)
        gateway = f"{parts[0]}.1"
    else:
        cidr, gateway = await allocate_vpc_cidr(k8s)

    # Resolve namespace bindings.
    # Precedence (matches V1 contract): explicit data.namespaces wins;
    # otherwise fall back to data.tenant → ["tenant-<name>"]; otherwise none.
    if data.namespaces:
        bind_namespaces: list[str] = list(data.namespaces)
    elif data.tenant:
        bind_namespaces = [_tenant_ns(data.tenant)]
    else:
        bind_namespaces = []

    # Validate each bound namespace: must exist AND carry kubevirt-ui.io/managed=true.
    # The managed label is the contract for "this namespace was created by us
    # (project / tenant)" so we don't accidentally bind a VPC to kube-system or
    # an unrelated user namespace.
    for ns in bind_namespaces:
        try:
            ns_obj = await k8s.core_api.read_namespace(name=ns)
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=400,
                    detail=f"Namespace '{ns}' does not exist",
                )
            raise k8s_error_to_http(e, f"reading namespace '{ns}'")
        ns_labels = (ns_obj.metadata.labels or {})
        if ns_labels.get("kubevirt-ui.io/managed") != "true":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Namespace '{ns}' is not managed by kubevirt-ui "
                    "(missing label kubevirt-ui.io/managed=true). VPCs can "
                    "only be bound to managed namespaces."
                ),
            )

    # Tenant isolation, decided before the subnet manifest is built so the
    # ACLs land at creation rather than in a follow-up patch a caller could
    # never make. `tenant_supernet` scopes the catch-all drop; with none
    # configured we create the VPC un-isolated and report that truthfully
    # instead of writing a rule set that would blackhole the internet.
    isolation_acls: list[SubnetAcl] = []
    peer_cidrs: list[str] = []
    if data.isolated:
        supernet = get_settings().tenant_supernet
        peer_cidrs = await _tenant_vpc_cidrs(k8s, exclude=data.name)
        if peer_cidrs or supernet:
            isolation_acls = build_isolation_acls(
                subnet_cidr=cidr,
                tenant_supernet=supernet,
                shared_cidrs=data.shared_cidrs,
                peer_cidrs=peer_cidrs,
            )
        else:
            logger.warning(
                f"VPC {data.name!r}: isolation requested but there is neither "
                "another tenant VPC nor a TENANT_SUPERNET to scope the drop to "
                "— creating without isolation ACLs."
            )

    # Baseline, on every VPC and not only the isolated ones: with B3 the
    # tenant's prefix is routed from the node network, so "the management
    # network cannot open connections into a tenant" stops being true by
    # accident and has to be written down. Shipped in the same change as the
    # B3 attachment above on purpose — a VPC that gets the route without the
    # rule is reachable from every node for as long as the gap lasts.
    if b3_enabled():
        isolation_acls = [
            *isolation_acls, *build_mgmt_deny_acls(await _mgmt_deny_sources(k8s)),
        ]

    labels: dict[str, str] = {"kubevirt-ui.io/managed": "true"}
    if data.tenant:
        labels["kubevirt-ui.io/tenant"] = data.tenant
    # T7 — folder/env scoping. Stamped on VPC + default subnet so the
    # tenant-create wizard can filter by `GET /api/v1/vpcs?folder=&environment=`.
    if data.folder:
        labels["kubevirt-ui.io/folder"] = data.folder
    if data.environment:
        labels["kubevirt-ui.io/environment"] = data.environment

    # Build VPC spec
    vpc_spec: dict[str, Any] = {
        "namespaces": bind_namespaces,
    }
    if data.enable_nat_gateway and _vpc_nat_gateway_enabled():
        vpc_spec["enableExternal"] = True
    elif data.enable_nat_gateway:
        logger.info(
            "VPC %r asked for a NAT gateway; ignoring — egress goes through the "
            "shared egress gateway, and the subnet's single SNAT slot belongs "
            "to the control-plane transit path. Set VPC_NAT_GATEWAY_ENABLED=true "
            "to opt in on a topology where that is the right answer.",
            data.name,
        )
    if data.static_routes:
        vpc_spec["staticRoutes"] = [
            {"cidr": r.cidr, "nextHopIP": r.next_hop_ip, "policy": r.policy}
            for r in data.static_routes
        ]

    # B3: the VPC routes out through its own leg on the external VLAN, with no
    # SNAT there and no gateway pods in the path. Two lines, and both are
    # required:
    #
    #   * **both** `enableExternal` and the named attachment are required, and
    #     each alone does nothing. A fresh VPC with the flag and no `external`
    #     in the array had no external port; a VPC with the array and no flag
    #     had no ports at all — both measured, a day apart. The flag is the
    #     master switch that makes kube-ovn process the array; the array says
    #     which subnets. `attach_vpc_to_transit` sets the flag too, which is
    #     why an existing tenant VPC looked like the array alone sufficed.
    #   * the default route is what puts the VPC *on* B3. The announcement
    #     generator reads exactly this — a VPC is announced when its default
    #     route leads into the external subnet — so the datapath and the
    #     announcement cannot drift apart: they are the same fact.
    if b3_enabled():
        vpc_spec["enableExternal"] = True
        attachments = list(vpc_spec.get("extraExternalSubnets") or [])
        transit = transit_subnet_name()
        # The array is replaced wholesale by a merge patch, so it is built in
        # full here rather than appended to later.
        for name in (transit, external_subnet()):
            if name and name not in attachments:
                attachments.append(name)
        vpc_spec["extraExternalSubnets"] = attachments
        routes = list(vpc_spec.get("staticRoutes") or [])
        if not any(r.get("cidr") == "0.0.0.0/0" for r in routes):
            routes.append({
                "cidr": "0.0.0.0/0",
                "nextHopIP": vpc_gateway(),
                "policy": "policyDst",
            })
        vpc_spec["staticRoutes"] = routes

    vpc_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "Vpc",
        "metadata": {"name": data.name, "labels": labels},
        "spec": vpc_spec,
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            body=vpc_manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"VPC '{data.name}' already exists")
        raise k8s_error_to_http(e, "VPC operation")

    # Create default subnet
    default_subnet_name = f"{data.name}-default"

    # dhcpV4Options is what kube-ovn pushes to workloads via DHCP.
    # `dns_server` here is the cluster-wide VpcDns VIP (autodiscovered) —
    # the same VIP every per-VPC VpcDns instance listens on via the OVN
    # switch LB. Pods/VMs in the VPC ask DHCP, get this DNS server, and
    # resolve via the local VpcDns deployment which forwards to cluster
    # CoreDNS. Set at subnet creation time (avoids post-patch races with
    # the kube-ovn controller observing the new subnet).
    vpcdns_vip = _vpcdns_vip()
    dhcp_v4 = (
        f"lease_time=3600,router={gateway},"
        f"server_id={gateway},dns_server={vpcdns_vip}"
    )

    subnet_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "Subnet",
        "metadata": {"name": default_subnet_name, "labels": labels},
        "spec": {
            "protocol": "IPv4",
            "cidrBlock": cidr,
            "gateway": gateway,
            "vpc": data.name,
            # Marks this subnet as the VPC's default — kube-ovn-controller
            # routes any namespace joining the VPC (without an explicit
            # ovn.kubernetes.io/logical_switch annotation) to this subnet.
            # Without this, tenants bound to the VPC land on the cluster
            # default overlay (10.16.0.0/16) instead of the VPC subnet.
            # Only set on the create-with-VPC default subnet — secondary
            # subnets added later via /subnets endpoints remain non-default
            # unless an admin explicitly promotes them.
            "default": True,
            "enableDHCP": True,
            "dhcpV4Options": dhcp_v4,
            "natOutgoing": data.enable_nat_gateway,
            **({"namespaces": bind_namespaces} if bind_namespaces else {}),
            **({"acls": _acls_to_spec(isolation_acls)} if isolation_acls else {}),
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            body=subnet_manifest,
        )
    except ApiException as e:
        # Cleanup VPC on subnet failure
        logger.error(f"Failed to create default subnet for VPC {data.name}: {e}")
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
                name=data.name,
            )
        except ApiException:
            pass
        raise k8s_error_to_http(e, "VPC operation")

    # Auto-create per-VPC VpcDns. Best-effort: failures are logged but the
    # VPC create still succeeds — admin can manually create the VpcDns CR
    # later (workloads will lack VPC-internal DNS resolution until then).
    await _ensure_vpc_dns(k8s, data.name, default_subnet_name)

    # Shared networks: build the path, not just the permission.
    await _peer_shared_cidrs(k8s, data.name, data.shared_cidrs or [])

    # Per-VPC OVN NAT gateway. Off unless someone deliberately turns it on:
    # kube-ovn keeps one SNAT per logical IP, and on a hybrid VPC that slot
    # belongs to the control-plane transit path. Taking it gives the tenant
    # internet and no control plane — with every CR green, which is how it
    # went unnoticed. The code stays because the topology where this is the
    # right answer (an external subnet the upstream already NATs) is on the
    # roadmap; the default does not.
    if data.enable_nat_gateway and _vpc_nat_gateway_enabled():
        from app.api.v1.ovn_gateway import (
            _ensure_vpc_external_config,
            _label_nodes_external_gw,
            _patch_lsp_nat_options,
            _save_gateway_tracking,
            _gateway_labels,
            _delete_crd_ignore_404,
        )
        from app.api.v1.network import _find_infra_subnet
        try:
            # Try auto-detect infra subnet, skip NAT if not found
            infra = await _find_infra_subnet(k8s)
            if not infra:
                logger.warning(f"No infra subnet found — skipping OVN NAT for VPC {data.name}")
            else:
                infra_subnet_name = infra["metadata"]["name"]
                infra_gateway_ip = infra.get("spec", {}).get("gateway", "")

                # Patch VPC with external config + default route
                await _ensure_vpc_external_config(k8s, data.name, infra_subnet_name, infra_gateway_ip)

                # Label nodes
                await _label_nodes_external_gw(k8s, infra)

                # Create OvnEip (keyed by vpc_name)
                eip_name = f"eip-{data.name}"
                eip_manifest = {
                    "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
                    "kind": "OvnEip",
                    "metadata": {
                        "name": eip_name,
                        "labels": {
                            **_gateway_labels(data.name),
                            "kubevirt-ui.io/vpc": data.name,
                        },
                    },
                    "spec": {
                        "externalSubnet": infra_subnet_name,
                        "type": "nat",
                    },
                }
                try:
                    await k8s.custom_api.create_cluster_custom_object(
                        group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                        plural="ovn-eips", body=eip_manifest,
                    )
                except ApiException as e:
                    if e.status != 409:
                        raise

                # Create OvnSnatRule
                snat_name = f"snat-{data.name}"
                snat_manifest = {
                    "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
                    "kind": "OvnSnatRule",
                    "metadata": {
                        "name": snat_name,
                        "labels": _gateway_labels(data.name),
                    },
                    "spec": {
                        "ovnEip": eip_name,
                        "vpc": data.name,
                        "vpcSubnet": default_subnet_name,
                    },
                }
                try:
                    await k8s.custom_api.create_cluster_custom_object(
                        group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                        plural="ovn-snat-rules", body=snat_manifest,
                    )
                except ApiException as e:
                    if e.status != 409:
                        raise

                # Patch LSP options (retry up to 3 times)
                import asyncio
                lsp_patched = False
                for attempt in range(3):
                    if attempt > 0:
                        await asyncio.sleep(2)
                    lsp_patched = await _patch_lsp_nat_options(k8s, data.name, infra_subnet_name)
                    if lsp_patched:
                        break

                # Save tracking ConfigMap (keyed by vpc_name)
                await _save_gateway_tracking(k8s, data.name, {
                    "vpc_name": data.name,
                    "subnet_name": default_subnet_name,
                    "external_subnet": infra_subnet_name,
                    "eip_name": eip_name,
                    "shared_eip": "",
                    "lsp_patched": str(lsp_patched).lower(),
                    "auto_created": "true",
                }, "")

                logger.info(f"OVN NAT enabled for VPC {data.name} (eip={eip_name})")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Failed to set up OVN NAT for VPC {data.name}: {e}")

    # Older VPCs have never heard of this prefix; their drop rules are literal
    # matches, so re-scope every isolated VPC to the new peer set.
    #
    # Unconditionally — not only when the new VPC is itself isolated. Gating
    # on that punched a hole in every isolated VPC each time an un-isolated
    # one appeared: four VPCs created concurrently took 10.211–10.214 and
    # `acme-net`, isolated, went on dropping only 10.205/206/208/209.
    # Isolation is about who may reach *this* VPC; the other side's setting
    # has no say in it.
    try:
        n = await reconcile_isolation_acls(k8s)
        logger.info(f"Isolation ACLs re-scoped on {n} VPC subnet(s)")
    except Exception as e:
        logger.warning(f"Isolation reconcile after creating {data.name!r} failed: {e}")

    return VpcResponse(
        name=data.name,
        tenant=data.tenant,
        folder=data.folder,
        environment=data.environment,
        enable_nat_gateway=data.enable_nat_gateway,
        default_subnet=default_subnet_name,
        subnets=[VpcSubnetInfo(
            name=default_subnet_name, cidr_block=cidr, gateway=gateway,
        )],
        static_routes=[
            VpcStaticRoute(cidr=r.cidr, nextHopIP=r.next_hop_ip, policy=r.policy)
            for r in data.static_routes
        ],
        namespaces=bind_namespaces,
        # What was actually written, not what was asked for.
        isolated=bool(isolation_acls),
        ready=False,
    )


@router.get("/{name}", response_model=VpcResponse)
async def get_vpc(request: Request, name: str, user: User = Depends(require_auth)) -> VpcResponse:
    """Get VPC detail with subnets and peerings."""
    k8s = request.app.state.k8s_client

    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "VPC operation")

    vpc = _parse_vpc(item)
    vpc.subnets, vpc.isolated = await _get_vpc_subnets(k8s, name)
    vpc.peerings = await _get_vpc_peerings(k8s, name)
    return vpc


@router.patch("/{name}/scope", response_model=VpcResponse)
async def set_vpc_scope(
    request: Request, name: str, data: VpcScopeRequest,
    user: User = Depends(require_admin),
) -> VpcResponse:
    """Move a VPC to a different folder/environment.

    Scope was write-once: chosen in the create wizard — which did not show it
    on the Review step — and after that the only way to correct it was to
    delete the VPC and everything in it. The tenant wizard meanwhile tells
    people to "scope a VPC to this folder", which was advice with no
    corresponding action.

    Nothing here touches the dataplane. `kubevirt-ui.io/folder` and
    `kubevirt-ui.io/environment` are labels kube-ovn never reads; they decide
    who sees the VPC and which tenants may be placed in it. The default subnet
    carries the same pair — the tenant wizard filters on it — so both move
    together or the VPC becomes invisible to the very filter it was scoped for.
    """
    k8s = request.app.state.k8s_client

    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "VPC operation")

    if data.folder:
        folders = await load_folders(k8s)  # {folder_name: meta}
        if data.folder not in folders:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No folder named {data.folder!r}. Scoping a VPC to a folder "
                    f"that does not exist hides it from every view."
                ),
            )

    # An explicit null clears the label; kubernetes removes a label set to None
    # under a merge patch, which is exactly the semantics we want here.
    labels = {
        "kubevirt-ui.io/folder": data.folder or None,
        "kubevirt-ui.io/environment": data.environment or None,
    }
    patch = {"metadata": {"labels": labels}}

    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=name, body=patch,
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "VPC operation")

    # The wizard's filter reads the *subnet* labels, so a VPC whose subnets
    # kept the old scope would move for the VPC list and not for the thing the
    # scope exists to drive.
    subnets, _ = await _get_vpc_subnets(k8s, name)
    for subnet in subnets:
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                name=subnet.name, body=patch,
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            logger.warning("VPC %r: could not rescope subnet %r: %s", name, subnet.name, e)

    logger.info(
        "VPC %r rescoped to folder=%r environment=%r by %s",
        name, data.folder, data.environment, user.username,
    )
    # Keyword form on purpose: a direct call must hand the real user across, and
    # `user=` is what the guard test greps for. FastAPI only resolves
    # `Depends(require_auth)` for requests it routes.
    return await get_vpc(request, name, user=user)


async def _still_present(k8s_client: Any, plural: str, names: list[str]) -> list[str]:
    """Which of these cluster-scoped objects still exist."""
    left = []
    for n in names:
        try:
            await k8s_client.custom_api.get_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural=plural, name=n,
            )
            left.append(n)
        except ApiException as e:
            if e.status != 404:
                left.append(n)
    return left


# How long the delete request is willing to hold while kube-ovn finishes. A
# subnet normally goes in well under ten seconds; past this the caller is told
# to retry rather than kept waiting.
DELETE_DRAIN_TIMEOUT = 45.0


async def _await_dependents_gone(
    k8s_client: Any, subnets: list[str], nat: dict[str, list[str]],
    timeout: float | None = None,
) -> dict[str, list[str]]:
    """Block until the VPC's subnets and NAT objects are really gone.

    Every one of them is finalized by kube-ovn against the VPC's logical
    router, so removing the Vpc CR while they are still finalizing strands
    them permanently: the controller then loops on `not found logical router`
    and the finalizer never comes off. Measured on the lab cluster two hours
    after deleting `gamma-net` — `snat-gamma-net` and `eip-gamma-net` still
    terminating, and 10.198.190.206 still missing from an external pool with
    14 addresses left in it. `con3-default` hung the same way.

    Returns whatever is still there when the deadline passes, so the caller
    can refuse rather than create the stranded objects itself.
    """
    deadline = asyncio.get_running_loop().time() + (
        DELETE_DRAIN_TIMEOUT if timeout is None else timeout
    )
    while True:
        left = {"subnets": await _still_present(k8s_client, "subnets", subnets)}
        for plural, names in nat.items():
            remaining = await _still_present(k8s_client, plural, names)
            if remaining:
                left[plural] = remaining
        if not any(left.values()):
            return {}
        if asyncio.get_running_loop().time() >= deadline:
            return {k: v for k, v in left.items() if v}
        await asyncio.sleep(2)


@router.delete("/{name}")
async def delete_vpc(request: Request, name: str, user: User = Depends(require_admin)) -> dict:
    """Delete a VPC and cascade-delete its subnets and peerings."""
    k8s = request.app.state.k8s_client

    # Clean up OVN NAT gateway if exists (keyed by vpc_name)
    from app.api.v1.ovn_gateway import OVN_GW_LABEL, _cleanup_ovn_gateway, _get_gateway_tracking
    nat_names: dict[str, list[str]] = {}
    tracking, _ = await _get_gateway_tracking(k8s, name)
    if tracking:
        for plural in ("ovn-snat-rules", "ovn-fips", "ovn-dnat-rules", "ovn-eips"):
            try:
                found = await k8s.custom_api.list_cluster_custom_object(
                    group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural=plural,
                    label_selector=f"{OVN_GW_LABEL}={name}",
                )
                names = [i["metadata"]["name"] for i in found.get("items", [])]
                if names:
                    nat_names[plural] = names
            except ApiException as e:
                logger.warning(f"Could not list {plural} for VPC {name}: {e}")
        try:
            await _cleanup_ovn_gateway(k8s, name)
        except Exception as e:
            logger.warning(f"Failed to cleanup OVN NAT for VPC {name}: {e}")

    # Delete the per-VPC VpcDns CR (created at VPC create time). 404 is
    # tolerated (older VPCs predate the auto-create, or admin already
    # removed it).
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpc-dnses", name=f"{name}-dns",
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete VpcDns {name}-dns: {e}")

    # Delete the paired Kyverno ClusterPolicy.
    await _delete_vpc_dns_policy(k8s, name)

    # Peerings live in `Vpc.spec.vpcPeerings` on *both* routers — there is no
    # `vpc-peerings` object to delete, and deleting this VPC leaves the other
    # side pointing at a router that no longer exists.
    peerings = await _get_vpc_peerings(k8s, name)
    for peering in peerings:
        remote = peering.remote_vpc
        if not remote:
            continue
        try:
            await _remove_peering_side(k8s, remote, name)
        except Exception as e:
            logger.warning(f"Failed to remove peering {remote}→{name}: {e}")

    # Delete subnets.
    #
    # `_get_vpc_subnets` returns (subnets, isolated); iterating the tuple gave
    # the list itself as the first item —
    #
    #   AttributeError: 'list' object has no attribute 'name'
    #
    # so deleting a VPC answered 500 and left its subnet behind, which then
    # blocks kube-ovn from ever finishing the VPC's own deletion.
    subnets, _isolated = await _get_vpc_subnets(k8s, name)
    for s in subnets:
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                name=s.name,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete subnet {s.name}: {e}")

    # The router has to outlive everything finalized against it.
    stranded = await _await_dependents_gone(
        k8s, [s.name for s in subnets], nat_names,
    )
    if stranded:
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in stranded.items())
        raise HTTPException(
            status_code=409,
            detail=(
                f"VPC '{name}' still has resources being deleted ({detail}). "
                "Removing the VPC now would strand them permanently — kube-ovn "
                "finalizes them against this VPC's router. They are already "
                "marked for deletion; retry in a moment."
            ),
        )

    # Delete VPC
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "VPC operation")

    # The other VPCs' drop rules name this prefix literally; leave them and a
    # future VPC handed the same CIDR inherits a block nobody wrote for it.
    try:
        n = await reconcile_isolation_acls(k8s)
        logger.info(f"Isolation ACLs re-scoped on {n} VPC subnet(s) after deleting {name!r}")
    except Exception as e:
        logger.warning(f"Isolation reconcile after deleting {name!r} failed: {e}")

    return {
        "status": "deleted",
        "name": name,
        "subnets_deleted": len(subnets),
        "peerings_deleted": len(peerings),
    }


# ============================================================================
# VPC Peering
# ============================================================================

# A VpcEgressGateway installs a reroute policy at priority 29100 that catches
# *everything* leaving the VPC, so it wins over a peering static route and the
# traffic hairpins out to the upstream router anyway. The peering only takes
# effect once a higher-priority allow sits above it.
PEERING_POLICY_PRIORITY = 31001
_PEERING_PATCH_RETRIES = 5


async def _vpc_subnet_cidrs(k8s, vpc_name: str) -> list[str]:
    """CIDRs of every subnet in a VPC — what the peer has to route to."""
    subnets, _ = await _get_vpc_subnets(k8s, vpc_name)
    return [s.cidr_block for s in subnets if s.cidr_block]


def _peering_side_patch(
    spec: dict[str, Any],
    remote_vpc: str,
    local_connect_ip: str,
    link_cidr: str,
    remote_cidrs: list[str],
    remote_connect_ip: str,
) -> dict[str, Any]:
    """One side of a peering: link, routes to the peer, and the allow above
    the egress gateway's catch-all reroute."""
    prefix = link_cidr.split("/")[-1]

    peerings = [
        p for p in spec.get("vpcPeerings", []) or []
        if p.get("remoteVpc") != remote_vpc
    ]
    peerings.append({
        "remoteVpc": remote_vpc,
        "localConnectIP": f"{local_connect_ip}/{prefix}",
    })

    # Drop any earlier entries for these destinations so re-peering converges
    # instead of stacking duplicates.
    routes = [
        r for r in spec.get("staticRoutes", []) or []
        if r.get("cidr") not in remote_cidrs
    ]
    routes.extend({
        "cidr": cidr,
        "nextHopIP": remote_connect_ip,
        "policy": "policyDst",
    } for cidr in remote_cidrs)

    policies = [
        p for p in spec.get("policyRoutes", []) or []
        if p.get("match") not in {f"ip4.dst == {c}" for c in remote_cidrs}
    ]
    policies.extend({
        "priority": PEERING_POLICY_PRIORITY,
        "action": "allow",
        "match": f"ip4.dst == {cidr}",
    } for cidr in remote_cidrs)

    return {"vpcPeerings": peerings, "staticRoutes": routes, "policyRoutes": policies}


@router.post("/{name}/peerings", response_model=VpcPeeringInfo, status_code=201)
async def create_vpc_peering(
    request: Request, name: str, data: VpcPeeringCreateRequest,
    user: User = Depends(require_admin),
) -> VpcPeeringInfo:
    """Peer two VPCs directly. Path VPC is the local side.

    Without peering, a tenant reaching a shared VPC goes tenant -> its egress
    gateway -> upstream router -> the other egress gateway -> target: four
    extra hops, with every shared-service flow crossing the lab router. Peered,
    it is one link:

         1  10.198.224.1     t1 VPC router
         2  169.254.101.2    peering link
         3  10.198.192.3     shared service

    Both VPCs are patched: the link (`vpcPeerings`), static routes to the
    peer's subnets, and — the part that is easy to miss — a policy route above
    the egress gateway's 29100 catch-all, without which the traffic hairpins
    anyway and the peering looks broken for no visible reason.
    """
    k8s = request.app.state.k8s_client
    return await _create_peering_pair(k8s, name, data.remote_vpc, data.link_cidr)


async def _create_peering_pair(
    k8s, name: str, remote_vpc: str, link_cidr_override: str | None = None,
) -> VpcPeeringInfo:
    """Both sides of a peering, allocation included.

    Extracted from the endpoint so the VPC-create path can use it for shared
    networks: the allow-ACL lifts the isolation drop but writes no route, so a
    "shared" VPC stayed unreachable until someone added the peering by hand.
    """
    if name == remote_vpc:
        raise HTTPException(status_code=400, detail="A VPC cannot peer with itself")

    specs: dict[str, dict[str, Any]] = {}
    for vpc_name in [name, remote_vpc]:
        try:
            item = await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
                name=vpc_name,
            )
            specs[vpc_name] = item.get("spec", {}) or {}
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
            raise k8s_error_to_http(e, "VPC operation")

    local_cidrs = await _vpc_subnet_cidrs(k8s, name)
    remote_cidrs = await _vpc_subnet_cidrs(k8s, remote_vpc)
    if not local_cidrs or not remote_cidrs:
        raise HTTPException(
            status_code=400,
            detail=(
                "Both VPCs need at least one subnet before they can be peered "
                "— there would be nothing to route to."
            ),
        )

    if link_cidr_override:
        network = ipaddress.ip_network(link_cidr_override, strict=False)
        hosts = list(network.hosts())
        if len(hosts) < 2:
            raise HTTPException(
                status_code=400,
                detail=f"Link CIDR {link_cidr_override} has no room for two addresses",
            )
        local_ip, remote_ip, link_cidr = str(hosts[0]), str(hosts[1]), str(network)
    else:
        local_ip, remote_ip, link_cidr = await allocate_peering_link(k8s)

    patches = {
        name: _peering_side_patch(
            specs[name], remote_vpc, local_ip, link_cidr,
            remote_cidrs, remote_ip,
        ),
        remote_vpc: _peering_side_patch(
            specs[remote_vpc], name, remote_ip, link_cidr,
            local_cidrs, local_ip,
        ),
    }

    applied: list[str] = []
    for vpc_name, patch in patches.items():
        try:
            await _apply_peering_side(
                k8s, vpc_name,
                remote_vpc if vpc_name == name else name,
                local_ip if vpc_name == name else remote_ip,
                link_cidr,
                remote_cidrs if vpc_name == name else local_cidrs,
                remote_ip if vpc_name == name else local_ip,
            )
            applied.append(vpc_name)
        except ApiException as e:
            # A peering configured on one side only is a black hole: the other
            # VPC has no route back. Undo what landed before giving up.
            for done in applied:
                try:
                    await _remove_peering_side(k8s, done, _other(done, name, remote_vpc))
                except Exception as undo_err:
                    logger.warning(
                        f"Could not roll back half-applied peering on {done!r}: {undo_err}"
                    )
            raise k8s_error_to_http(e, f"peering VPC '{vpc_name}'")

    logger.info(
        f"Peered VPC {name!r} with {remote_vpc!r} over {link_cidr} "
        f"({local_ip} <-> {remote_ip})"
    )
    return VpcPeeringInfo(
        name=f"{name}-to-{remote_vpc}",
        local_vpc=name,
        remote_vpc=remote_vpc,
        link_cidr=link_cidr,
        local_connect_ip=local_ip,
        remote_connect_ip=remote_ip,
    )



async def _apply_peering_side(
    k8s, vpc_name: str, peer: str, connect_ip: str, link_cidr: str,
    peer_cidrs: list[str], next_hop: str,
) -> None:
    """Write one side of a peering, re-reading the spec on every attempt.

    `spec.vpcPeerings` is a list, and a merge patch replaces it wholesale. Two
    peerings touching the same VPC at the same time each computed their list
    from the same read, so the second patch dropped the first entry — both
    calls returned 201 and half the links quietly did not exist:

        peering cc1<->cc2, cc3<->cc4, cc1<->cc3, cc2<->cc4 created together
        left four entries on the cluster where there should have been eight.

    `metadata.resourceVersion` in the body makes the API server refuse a patch
    computed from a stale read, and the retry recomputes against the winner.
    """
    for attempt in range(_PEERING_PATCH_RETRIES):
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=vpc_name,
        )
        spec = item.get("spec", {}) or {}
        patch = _peering_side_patch(
            spec, peer, connect_ip, link_cidr, peer_cidrs, next_hop,
        )
        body = {
            "metadata": {"resourceVersion": item["metadata"]["resourceVersion"]},
            "spec": patch,
        }
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
                name=vpc_name, body=body,
                _content_type="application/merge-patch+json",
            )
            return
        except ApiException as e:
            if e.status == 409 and attempt < _PEERING_PATCH_RETRIES - 1:
                logger.info(
                    f"Peering patch on {vpc_name!r} raced another writer "
                    f"(attempt {attempt + 1}); recomputing"
                )
                await asyncio.sleep(0.1 * (2 ** attempt))
                continue
            raise


def _other(this: str, a: str, b: str) -> str:
    return b if this == a else a


async def _remove_peering_side(k8s, vpc_name: str, remote_vpc: str) -> None:
    """Strip a peer's link, routes and policy routes from one VPC."""
    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise
    spec = item.get("spec", {}) or {}

    remote_cidrs = set(await _vpc_subnet_cidrs(k8s, remote_vpc))
    next_hops = {
        p.get("localConnectIP", "").split("/")[0]
        for p in spec.get("vpcPeerings", []) or []
        if p.get("remoteVpc") == remote_vpc
    }

    patch = {
        "vpcPeerings": [
            p for p in spec.get("vpcPeerings", []) or []
            if p.get("remoteVpc") != remote_vpc
        ],
        # Match on destination rather than next hop: the peer's addresses are
        # what identifies these routes, and the link IP may already be gone.
        "staticRoutes": [
            r for r in spec.get("staticRoutes", []) or []
            if r.get("cidr") not in remote_cidrs
            and r.get("nextHopIP") not in next_hops
        ],
        "policyRoutes": [
            p for p in spec.get("policyRoutes", []) or []
            if p.get("match") not in {f"ip4.dst == {c}" for c in remote_cidrs}
        ],
    }
    await k8s.custom_api.patch_cluster_custom_object(
        group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
        name=vpc_name, body={"spec": patch},
        _content_type="application/merge-patch+json",
    )


@router.delete("/{name}/peerings/{remote_vpc}")
async def delete_vpc_peering(request: Request, name: str, remote_vpc: str, user: User = Depends(require_admin)) -> dict:
    """Tear a peering down from both sides.

    Both, always: a peering left configured on one side is worse than none —
    that VPC keeps routing to a peer that has no route back, so the traffic
    leaves and is silently dropped.
    """
    k8s = request.app.state.k8s_client

    removed: list[str] = []
    for local, remote in ((name, remote_vpc), (remote_vpc, name)):
        try:
            await _remove_peering_side(k8s, local, remote)
            removed.append(local)
        except ApiException as e:
            raise k8s_error_to_http(e, f"removing peering from VPC '{local}'")

    logger.info(f"Removed peering between {name!r} and {remote_vpc!r}")
    return {
        "status": "deleted",
        "peering": f"{name}-to-{remote_vpc}",
        "vpcs_updated": removed,
    }


# ============================================================================
# VPC Static Routes
# ============================================================================

@router.get("/{name}/routes", response_model=list[VpcStaticRoute])
async def get_vpc_routes(request: Request, name: str, user: User = Depends(require_auth)) -> list[VpcStaticRoute]:
    """Get static routes for a VPC."""
    k8s = request.app.state.k8s_client

    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "VPC operation")

    return [
        VpcStaticRoute(
            cidr=r.get("cidr", ""),
            nextHopIP=r.get("nextHopIP", ""),
            policy=r.get("policy", "policyDst"),
        )
        for r in item.get("spec", {}).get("staticRoutes", [])
    ]


@router.put("/{name}/routes", response_model=list[VpcStaticRoute])
async def update_vpc_routes(
    request: Request, name: str, data: VpcStaticRoutesUpdateRequest,
    user: User = Depends(require_admin),
) -> list[VpcStaticRoute]:
    """Replace all static routes on a VPC."""
    k8s = request.app.state.k8s_client

    patch_body = {
        "spec": {
            "staticRoutes": [
                {"cidr": r.cidr, "nextHopIP": r.next_hop_ip, "policy": r.policy}
                for r in data.static_routes
            ],
        },
    }

    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=name, body=patch_body,
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "VPC operation")

    return data.static_routes


# ============================================================================
# VpcDns (per-VPC DNS forwarder) — T6
# ============================================================================

def _parse_vpc_dns(item: dict[str, Any], coredns_vip: str) -> VpcDnsResponse:
    """Parse a VpcDns CR into VpcDnsResponse, deriving the status phase.

    Phase derivation:
      - ``ready``   — Active condition is True OR status.active > 0
      - ``error``   — any condition has type containing 'Failure'/'Error'
                       with status True, OR reason ~= 'Failed'/'Error'
      - ``pending`` — CR exists, has no terminal condition yet
    Surfaces the latest condition's message when present so the UI can
    show actionable detail (missing prereqs CM, NAD, etc.).
    """
    metadata = item.get("metadata", {})
    spec = item.get("spec", {}) or {}
    status_dict = item.get("status", {}) or {}

    active = int(status_dict.get("active") or 0)
    conditions = status_dict.get("conditions") or []
    latest_msg: str | None = None
    phase = "pending"
    error_seen = False
    for cond in conditions:
        ctype = (cond.get("type") or "")
        cstatus = (cond.get("status") or "")
        creason = (cond.get("reason") or "")
        cmsg = cond.get("message")
        if cmsg:
            latest_msg = cmsg
        if cstatus == "True" and (
            "Failure" in ctype or "Error" in ctype
            or "Failed" in creason or "Error" in creason
        ):
            error_seen = True
        if ctype == "Active" and cstatus == "True":
            phase = "ready"

    if error_seen and phase != "ready":
        phase = "error"
    if phase == "pending" and active > 0:
        phase = "ready"

    return VpcDnsResponse(
        name=metadata.get("name", ""),
        vpc=spec.get("vpc", ""),
        subnet=spec.get("subnet", ""),
        # kube-ovn VpcDns defaults to 1 replica when unset.
        replicas=int(spec.get("replicas") or 1),
        coredns_vip=coredns_vip,
        status=VpcDnsStatus(
            phase=phase,
            active_pods=active,
            message=latest_msg,
        ),
    )


async def _get_vpc_dns_cr(k8s, vpc_name: str) -> dict[str, Any]:
    """Read the VpcDns CR for a VPC; raise HTTPException(404) if absent.

    Centralized so the three CRUD endpoints handle the not-found case
    consistently. CR name convention `<vpc>-dns` matches what
    `_ensure_vpc_dns` writes at VPC create time (T2).
    """
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpc-dnses", name=f"{vpc_name}-dns",
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No VpcDns CR for VPC '{vpc_name}' (expected name "
                    f"'{vpc_name}-dns'). The VPC may pre-date the T2 auto-"
                    "create flow, or auto-create failed at VPC create time. "
                    "POST /vpcs/{vpc_name}/dns/recreate to provision one."
                ),
            ) from e
        raise k8s_error_to_http(e, "VpcDns lookup")


async def _vpc_exists(k8s, vpc_name: str) -> bool:
    """Quick existence check — used to fail-fast on a bad VPC path param."""
    try:
        await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpcs", name=vpc_name,
        )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise k8s_error_to_http(e, "VPC lookup")


@router.get("/{vpc_name}/dns", response_model=VpcDnsResponse)
async def get_vpc_dns(
    request: Request, vpc_name: str,
    user: User = Depends(require_admin),
) -> VpcDnsResponse:
    """Read the per-VPC DNS deployment config and reconciliation status.

    Returns 404 when the VPC exists but the VpcDns CR doesn't — caller
    can POST `/dns/recreate` to provision one. Returns 404 with a
    different detail when the VPC itself doesn't exist.
    """
    k8s = request.app.state.k8s_client
    await _ensure_cluster_config(k8s)

    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")

    cr = await _get_vpc_dns_cr(k8s, vpc_name)
    return _parse_vpc_dns(cr, coredns_vip=_vpcdns_vip())


@router.patch("/{vpc_name}/dns", response_model=VpcDnsResponse)
async def update_vpc_dns(
    request: Request, vpc_name: str, data: VpcDnsUpdateRequest,
    user: User = Depends(require_admin),
) -> VpcDnsResponse:
    """Patch the VpcDns spec.

    Idempotent merge-patch. Only fields explicitly provided are patched —
    omitting `subnet` leaves the current binding intact; omitting
    `replicas` leaves the current count intact. When `subnet` is given,
    it must exist and belong to the same VPC (validated against the
    Subnet CR's `spec.vpc`).
    """
    k8s = request.app.state.k8s_client
    await _ensure_cluster_config(k8s)

    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")

    # 404-validate the CR upfront so we don't half-validate `subnet` for a
    # VPC that hasn't even got a VpcDns yet.
    await _get_vpc_dns_cr(k8s, vpc_name)

    spec_patch: dict[str, Any] = {}
    if data.subnet is not None:
        # Validate subnet exists + belongs to this VPC.
        try:
            subnet_cr = await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                plural="subnets", name=data.subnet,
            )
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=400,
                    detail=f"Subnet '{data.subnet}' does not exist",
                ) from e
            raise k8s_error_to_http(e, "Subnet lookup")
        subnet_vpc = (subnet_cr.get("spec") or {}).get("vpc")
        if subnet_vpc != vpc_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Subnet '{data.subnet}' belongs to VPC {subnet_vpc!r}, "
                    f"not '{vpc_name}'"
                ),
            )
        spec_patch["subnet"] = data.subnet
    if data.replicas is not None:
        # Pydantic Field(ge=1) already enforces >= 1.
        spec_patch["replicas"] = data.replicas

    if spec_patch:
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                plural="vpc-dnses", name=f"{vpc_name}-dns",
                body={"spec": spec_patch},
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            raise k8s_error_to_http(e, "VpcDns patch")

    # Re-read so the response carries the post-patch spec + latest status.
    cr = await _get_vpc_dns_cr(k8s, vpc_name)
    return _parse_vpc_dns(cr, coredns_vip=_vpcdns_vip())


@router.post("/{vpc_name}/dns/recreate", response_model=VpcDnsResponse)
async def recreate_vpc_dns(
    request: Request, vpc_name: str,
    user: User = Depends(require_admin),
) -> VpcDnsResponse:
    """Recovery path: delete the VpcDns CR and recreate it bound to the
    VPC's default subnet.

    Returns the freshly-created `VpcDnsResponse` so the UI can re-render
    the DNS tab without an extra GET round-trip (matches the contract
    coded into the frontend's T7 DNS-tab implementation).

    Useful when:
      - VPC pre-dates the T2 auto-create (404 from GET /dns).
      - VpcDns reconciliation stuck in error and a clean re-create might
        recover (e.g. stale references after a subnet rename).
      - The service-network route is missing from the VpcDns Deployment —
        the usual case, because the kube-ovn controller creates that
        Deployment after the CR, so the create path could not annotate it yet.

    This endpoint used to exist mainly to refresh CoreDNS pod IPs pinned into
    the Corefile. It no longer pins any: the Corefile names the kube-dns
    ClusterIP, which does not move. What it now repairs is the route that
    makes that ClusterIP reachable.

    Idempotent in the sense that if the CR is already absent, we still
    call `_ensure_vpc_dns` to create it. Spec recreated with the VPC's
    default subnet (we look it up by `spec.vpc == vpc_name` +
    `spec.default == True`) — admin can re-bind to a different subnet
    via PATCH afterwards.
    """
    k8s = request.app.state.k8s_client
    await _ensure_cluster_config(k8s)

    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")

    # 1. Delete current CR (404 tolerated).
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpc-dnses", name=f"{vpc_name}-dns",
        )
        logger.info(f"Deleted VpcDns {vpc_name}-dns (recreate)")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete VpcDns {vpc_name}-dns: {e}")
            raise k8s_error_to_http(e, "VpcDns delete")

    # 2. Resolve the VPC's default subnet for re-binding.
    try:
        subnet_list = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="subnets",
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "Subnet list")
    default_subnets = [
        (s.get("metadata") or {}).get("name") or ""
        for s in subnet_list.get("items", [])
        if (s.get("spec") or {}).get("vpc") == vpc_name
        and (s.get("spec") or {}).get("default") is True
    ]
    default_subnets = [n for n in default_subnets if n]
    if not default_subnets:
        raise HTTPException(
            status_code=400,
            detail=(
                f"VPC '{vpc_name}' has no default subnet; cannot recreate "
                "VpcDns. Mark a subnet as `spec.default=true` first, or "
                "PATCH the VpcDns to point at a specific subnet."
            ),
        )
    if len(default_subnets) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"VPC '{vpc_name}' has multiple default subnets "
                f"({sorted(default_subnets)!r}); reconcile before recreate."
            ),
        )
    subnet_name = default_subnets[0]

    # 3. Recreate via the same helper used at VPC create time, but with
    #    `swallow=False` so non-409 ApiException PROPAGATES — the admin
    #    explicitly clicked Recreate, so we must not pretend success on a
    #    silent failure. The wrap below converts kube API errors to 502
    #    with the underlying error in the detail.
    try:
        await _ensure_vpc_dns(k8s, vpc_name, subnet_name, swallow=False)
    except ApiException as e:
        raise k8s_error_to_http(e, f"recreate VpcDns for VPC '{vpc_name}'")
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to recreate VpcDns for VPC '{vpc_name}': {e}",
        ) from e

    # 4. Read back the new CR and return so the UI can update its tab
    #    without an extra GET. If the read genuinely returns 404, that
    #    means the create silently failed AFTER our explicit recreate
    #    succeeded (shouldn't happen with swallow=False above, but if
    #    it does — e.g. an admission webhook stripped the CR after
    #    creation — let the 404 propagate so the admin sees a real
    #    error instead of a misleading "pending" placeholder.
    cr = await _get_vpc_dns_cr(k8s, vpc_name)
    return _parse_vpc_dns(cr, coredns_vip=_vpcdns_vip())


# ---------------------------------------------------------------------------
# Per-VPC Kyverno DNS-injection policy — view + edit endpoints.
# Each VPC owns one ClusterPolicy named `kubevirt-ui-vpc-dns-<vpc>`. The
# UI's VPC detail page renders it as JSON, allows in-place edit, and lets
# admin recreate the default body if a hand-edit goes wrong.
# ---------------------------------------------------------------------------


@router.get("/{vpc_name}/dns-policy")
async def get_vpc_dns_policy(
    request: Request, vpc_name: str,
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Fetch the per-VPC Kyverno ClusterPolicy as raw JSON.

    404 distinguishes three states:
      - VPC missing → "VPC '<name>' not found";
      - Kyverno CRDs not installed → 404 with kyverno-specific detail;
      - VPC present but policy never created (default VPC, or legacy
        VPC predating the auto-create) → 404 with recreate hint.
    """
    k8s = request.app.state.k8s_client
    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
    if vpc_name == SYSTEM_VPC_NAME:
        raise HTTPException(
            status_code=404,
            detail=(
                f"The default VPC ('{SYSTEM_VPC_NAME}') has no DNS-injection "
                "policy — its pods reach the standard cluster CoreDNS."
            ),
        )
    policy_name = _kyverno_policy_name(vpc_name)
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KYVERNO_API_GROUP, version=KYVERNO_API_VERSION,
            plural="clusterpolicies", name=policy_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No Kyverno ClusterPolicy '{policy_name}' for VPC "
                    f"'{vpc_name}'. Either Kyverno isn't installed (deploy "
                    "the `kyverno` bootstrap component) or this VPC "
                    "pre-dates the auto-create; POST "
                    f"/vpcs/{vpc_name}/dns-policy/recreate to provision it."
                ),
            ) from e
        raise k8s_error_to_http(e, "ClusterPolicy lookup")


@router.delete("/{vpc_name}/dns-policy", status_code=204)
async def disable_vpc_dns_policy(
    request: Request, vpc_name: str,
    user: User = Depends(require_admin),
) -> None:
    """Remove the per-VPC Kyverno DNS-injection ClusterPolicy.

    The policy rewrites the resolver of *pods* landing on this VPC's subnets.
    VMs no longer depend on it — `vms.build_vpc_dns_spec` writes the same
    dnsConfig straight into the VM, so a VM keeps working with the policy
    gone. Deleting it is therefore a normal choice on a cluster that runs no
    plain pods inside its VPCs, or one that would rather not run Kyverno at
    all; `PUT` (or recreating the VPC's default) brings it back.
    """
    k8s = request.app.state.k8s_client
    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
    await _delete_vpc_dns_policy(k8s, vpc_name)


@router.put("/{vpc_name}/dns-policy")
async def update_vpc_dns_policy(
    request: Request, vpc_name: str, body: dict[str, Any],
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Replace the per-VPC Kyverno ClusterPolicy with `body`.

    `body` must be a Kyverno ClusterPolicy with metadata.name matching
    the per-VPC convention (`kubevirt-ui-vpc-dns-<vpc>`). We reject any
    other name to avoid a typo silently creating a stray policy. Other
    fields (rules, mutate, context) are admin's to edit — including the
    VIP, search domains, exclusion list.
    """
    k8s = request.app.state.k8s_client
    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
    if vpc_name == SYSTEM_VPC_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to create a DNS-injection policy for the default VPC ('{SYSTEM_VPC_NAME}').",
        )
    expected_name = _kyverno_policy_name(vpc_name)
    body_name = (body.get("metadata") or {}).get("name")
    if body_name != expected_name:
        raise HTTPException(
            status_code=400,
            detail=(
                f"ClusterPolicy.metadata.name must be '{expected_name}' "
                f"(got {body_name!r}). The name is bound to the VPC; "
                "editing it would orphan the policy from the VPC lifecycle."
            ),
        )
    body.setdefault("apiVersion", f"{KYVERNO_API_GROUP}/{KYVERNO_API_VERSION}")
    body.setdefault("kind", "ClusterPolicy")
    try:
        return await k8s.custom_api.replace_cluster_custom_object(
            group=KYVERNO_API_GROUP, version=KYVERNO_API_VERSION,
            plural="clusterpolicies", name=expected_name, body=body,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "ClusterPolicy replace")


@router.post("/{vpc_name}/dns-policy/recreate")
async def recreate_vpc_dns_policy(
    request: Request, vpc_name: str,
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Discard the current policy and recreate the default body.

    Useful when a manual edit broke the policy. Delete first (404
    tolerated), then call `_ensure_vpc_dns_policy` which rebuilds the
    default body bound to the current cluster-wide VpcDns VIP.
    """
    k8s = request.app.state.k8s_client
    await _ensure_cluster_config(k8s)
    if not await _vpc_exists(k8s, vpc_name):
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
    if vpc_name == SYSTEM_VPC_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to create a DNS-injection policy for the default VPC ('{SYSTEM_VPC_NAME}').",
        )
    await _delete_vpc_dns_policy(k8s, vpc_name)
    await _ensure_vpc_dns_policy(k8s, vpc_name)
    policy_name = _kyverno_policy_name(vpc_name)
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KYVERNO_API_GROUP, version=KYVERNO_API_VERSION,
            plural="clusterpolicies", name=policy_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Recreated policy '{policy_name}' is not visible after "
                    "create — Kyverno CRDs may be absent. Install the `kyverno` "
                    "bootstrap component first."
                ),
            ) from e
        raise k8s_error_to_http(e, "ClusterPolicy lookup")
