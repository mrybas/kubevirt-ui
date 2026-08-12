"""Shared constants and helpers for tenant management."""

import ipaddress
import logging
import os
import time
from typing import Any

import yaml
from fastapi import HTTPException
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from app.core.constants import (
    CAPI_API_GROUP,
    CAPI_API_VERSION,
    KUBEOVN_API_GROUP,
    KUBEOVN_API_VERSION,
)
from app.models.tenant import AddonCatalog, AddonComponent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_NS_PREFIX = "tenant-"
CATALOG_CONFIGMAP = "tenant-addon-catalog"
CATALOG_NAMESPACE = "flux-system"

CAPI_GROUP = CAPI_API_GROUP
CAPI_VERSION = CAPI_API_VERSION
KAMAJI_CP_GROUP = "controlplane.cluster.x-k8s.io"
KAMAJI_CP_VERSION = "v1alpha1"
KUBEVIRT_INFRA_GROUP = "infrastructure.cluster.x-k8s.io"
KUBEVIRT_INFRA_VERSION = "v1alpha1"
FLUX_HELM_GROUP = "helm.toolkit.fluxcd.io"
FLUX_HELM_VERSION = "v2"

# VpcDns hardcoded fallbacks — used only when autodiscovery fails AND no env override.
# These match Talos defaults (service CIDR 10.96.0.0/12). Override per cluster via:
#   TENANTS_VPCDNS_FORWARD_DNS  (kube-dns ClusterIP)
#   TENANTS_VPCDNS_VIP          (free IP in service CIDR for VpcDns VIP)
_VPCDNS_VIP_FALLBACK = "10.96.0.200"
_VPCDNS_FORWARD_DNS_FALLBACK = "10.96.0.10"

# OIDC defaults (can be overridden by env)
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "kubevirt-ui")

# ---------------------------------------------------------------------------
# Cluster-discovered config (ingress IP/class + mgmt CIDR)
# Populated lazily via _ensure_cluster_config(k8s); env overrides win.
# ---------------------------------------------------------------------------

_CLUSTER_CONFIG_TTL_SEC = 300

# {"ingress_ip": str, "ingress_class": str, "ingress_controller": str,
#  "mgmt_cidr": str | None, "fetched_at": float}
_cluster_config: dict[str, Any] | None = None


async def _ensure_cluster_config(k8s) -> dict[str, Any]:
    """Populate cluster-discovered config (ingress IP/class, mgmt CIDR).

    Idempotent + TTL-cached. Env overrides bypass discovery per-field.
    Must be awaited at the entry of any tenant handler before any sync
    getter (_endpoint_host, _ingress_class, _mgmt_cidr_drop) is called.
    """
    global _cluster_config
    now = time.monotonic()
    if _cluster_config and (now - _cluster_config["fetched_at"]) < _CLUSTER_CONFIG_TTL_SEC:
        return _cluster_config

    ip_override = os.getenv("TENANTS_INGRESS_IP")
    class_override = os.getenv("TENANTS_INGRESS_CLASS")
    domain_override = (os.getenv("TENANTS_INGRESS_DOMAIN") or "").strip()
    cidr_override = os.getenv("TENANTS_MGMT_CIDR")
    vpcdns_forward_override = os.getenv("TENANTS_VPCDNS_FORWARD_DNS")
    vpcdns_vip_override = os.getenv("TENANTS_VPCDNS_VIP")

    ingress_class = class_override
    ingress_controller = "unknown"
    if not ingress_class:
        networking_api = client.NetworkingV1Api(k8s._api_client)
        classes = await networking_api.list_ingress_class()
        default = next(
            (c for c in classes.items
             if (c.metadata.annotations or {}).get(
                 "ingressclass.kubernetes.io/is-default-class") == "true"),
            None,
        )
        if default is None:
            raise RuntimeError(
                "No default IngressClass on cluster; "
                "set TENANTS_INGRESS_CLASS env var"
            )
        ingress_class = default.metadata.name
        ingress_controller = default.spec.controller or "unknown"
    else:
        # Try to also resolve the controller string for the override class
        try:
            networking_api = client.NetworkingV1Api(k8s._api_client)
            ic = await networking_api.read_ingress_class(name=ingress_class)
            ingress_controller = ic.spec.controller or "unknown"
        except ApiException:
            pass

    ingress_ip = ip_override
    if not ingress_ip:
        svcs = await k8s.core_api.list_service_for_all_namespaces()
        candidates = [
            s for s in svcs.items
            if s.spec and s.spec.type == "LoadBalancer"
            and s.status and s.status.load_balancer
            and s.status.load_balancer.ingress
        ]
        # Heuristic: pick the Service whose name OR namespace mentions the
        # ingress class. Common conventions: ingress-nginx/ingress-nginx-controller,
        # traefik/traefik, projectcontour/envoy.
        match = next(
            (s for s in candidates
             if ingress_class in s.metadata.name
             or ingress_class in (s.metadata.namespace or "")
             or "ingress" in s.metadata.name),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"No LoadBalancer Service found for ingress class {ingress_class}; "
                "set TENANTS_INGRESS_IP env var"
            )
        lb = match.status.load_balancer.ingress[0]
        ingress_ip = lb.ip or lb.hostname
        if not ingress_ip:
            raise RuntimeError(
                f"Service {match.metadata.namespace}/{match.metadata.name} has no LB IP/hostname"
            )

    mgmt_cidr = cidr_override
    if not mgmt_cidr:
        try:
            nodes = await k8s.core_api.list_node()
            ips = []
            for n in nodes.items:
                for addr in (n.status.addresses or []):
                    if addr.type == "InternalIP" and addr.address:
                        ips.append(addr.address)
            if ips:
                octets = ips[0].split(".")
                if len(octets) == 4:
                    mgmt_cidr = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
        except ApiException as e:
            logger.warning(f"Failed to autodiscover mgmt CIDR from Nodes: {e}")
            mgmt_cidr = None

    vpcdns_forward_dns = vpcdns_forward_override
    if not vpcdns_forward_dns:
        vpcdns_forward_dns = await _discover_kube_dns_clusterip(k8s)
    if not vpcdns_forward_dns:
        logger.warning(
            "VpcDns forward DNS could not be autodiscovered (kube-dns/coredns Service "
            f"not found in kube-system); falling back to {_VPCDNS_FORWARD_DNS_FALLBACK}. "
            "Override via TENANTS_VPCDNS_FORWARD_DNS env var."
        )
        vpcdns_forward_dns = _VPCDNS_FORWARD_DNS_FALLBACK

    # Discovered unconditionally (not only when deriving the VIP): tenant
    # create validates its own service CIDR against this one, and a tenant
    # that overlaps the host's is a silent, delayed failure — see
    # `assert_tenant_cidrs_free`.
    host_service_cidr = await _discover_service_cidr(k8s)

    vpcdns_vip = vpcdns_vip_override
    if not vpcdns_vip:
        service_cidr = host_service_cidr
        if service_cidr:
            try:
                net = ipaddress.ip_network(service_cidr, strict=False)
                # Replace the last octet of the network address with 200.
                # For IPv4 service CIDRs like 10.96.0.0/12 → 10.96.0.200.
                octets = str(net.network_address).split(".")
                if len(octets) == 4:
                    vpcdns_vip = f"{octets[0]}.{octets[1]}.{octets[2]}.200"
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse service CIDR {service_cidr!r}: {e}")
    if not vpcdns_vip:
        logger.warning(
            "VpcDns VIP could not be autodiscovered (service CIDR not found via "
            f"kubeadm-config or kube-apiserver pod args); falling back to {_VPCDNS_VIP_FALLBACK}. "
            "Override via TENANTS_VPCDNS_VIP env var."
        )
        vpcdns_vip = _VPCDNS_VIP_FALLBACK

    ingress_domain = domain_override or f"{ingress_ip}.nip.io"

    _cluster_config = {
        "ingress_ip": ingress_ip,
        "ingress_domain": ingress_domain,
        "ingress_class": ingress_class,
        "ingress_controller": ingress_controller,
        "mgmt_cidr": mgmt_cidr,
        "vpcdns_forward_dns": vpcdns_forward_dns,
        "vpcdns_vip": vpcdns_vip,
        "host_service_cidr": host_service_cidr,
        "fetched_at": now,
    }
    logger.info(
        f"Cluster config: ingress_ip={ingress_ip} ingress_domain={ingress_domain} "
        f"class={ingress_class} controller={ingress_controller} mgmt_cidr={mgmt_cidr} "
        f"vpcdns_forward_dns={vpcdns_forward_dns} vpcdns_vip={vpcdns_vip}"
    )
    return _cluster_config


async def _discover_kube_dns_clusterip(k8s) -> str | None:
    """Look up cluster DNS ClusterIP, trying kube-dns then coredns."""
    for svc_name in ("kube-dns", "coredns"):
        try:
            svc = await k8s.core_api.read_namespaced_service(
                name=svc_name, namespace="kube-system",
            )
            ip = svc.spec.cluster_ip if svc.spec else None
            if ip and ip not in ("None", ""):
                return ip
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Error reading kube-system/{svc_name} Service: {e}")
    return None


async def _discover_service_cidr(k8s) -> str | None:
    """Discover service CIDR from kubeadm-config ConfigMap, then kube-apiserver Pod args."""
    # Try 1: kube-system/kubeadm-config ConfigMap
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name="kubeadm-config", namespace="kube-system",
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Error reading kube-system/kubeadm-config ConfigMap: {e}")
    else:
        # Parse separately so YAML/shape errors fall through to Try 2 instead
        # of being swallowed by the outer ApiException-only handler.
        try:
            cluster_cfg_raw = (cm.data or {}).get("ClusterConfiguration", "")
            if cluster_cfg_raw:
                cluster_cfg = yaml.safe_load(cluster_cfg_raw)
                if isinstance(cluster_cfg, dict):
                    subnet = (cluster_cfg.get("networking") or {}).get("serviceSubnet")
                    if subnet:
                        return subnet
                else:
                    logger.warning(
                        "kubeadm-config ClusterConfiguration parsed to "
                        f"{type(cluster_cfg).__name__}, expected dict; falling through"
                    )
        except (yaml.YAMLError, AttributeError, TypeError) as e:
            logger.warning(f"kubeadm-config malformed, falling through: {e}")

    # Try 2: any kube-apiserver Pod's --service-cluster-ip-range= arg.
    # Try multiple label selectors — distros disagree on conventions:
    #   kubeadm:    component=kube-apiserver
    #   Talos/k0s:  k8s-app=kube-apiserver
    #   some CP:    tier=control-plane,k8s-app=kube-apiserver
    selectors = (
        "component=kube-apiserver",
        "k8s-app=kube-apiserver",
        "tier=control-plane,k8s-app=kube-apiserver",
    )
    for selector in selectors:
        try:
            pods = await k8s.core_api.list_namespaced_pod(
                namespace="kube-system",
                label_selector=selector,
            )
        except ApiException as e:
            logger.warning(f"Error listing kube-apiserver pods (selector={selector!r}): {e}")
            continue
        if not pods.items:
            continue
        for pod in pods.items:
            for container in (pod.spec.containers or []) if pod.spec else []:
                # Args may live in `command` or `args` depending on manifest
                tokens = list(container.command or []) + list(container.args or [])
                for tok in tokens:
                    if tok.startswith("--service-cluster-ip-range="):
                        return tok.split("=", 1)[1]

    return None


def _require_cluster_config() -> dict[str, Any]:
    if _cluster_config is None:
        raise RuntimeError(
            "_ensure_cluster_config(k8s) must be awaited before this call"
        )
    return _cluster_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_ns(name: str) -> str:
    return f"{TENANT_NS_PREFIX}{name}"


def _endpoint_host(name: str) -> str:
    """External hostname for the tenant kube-apiserver.

    Defaults to ``<name>.<ingress_ip>.nip.io`` (zero-config, public wildcard
    DNS). Override via ``TENANTS_INGRESS_DOMAIN`` env var to use your own
    domain — e.g. set to ``clusters.example.com`` and tenants become
    ``<name>.clusters.example.com``.
    """
    return f"{name}.{_require_cluster_config()['ingress_domain']}"


def _external_dns_target() -> str:
    """IP external-dns should publish for tenant API hostnames.

    external-dns can't infer a target from an IngressRouteTCP (no
    `.status.loadBalancer` — the VIP belongs to the shared Traefik LB
    Service, not the IRT), so we stamp it explicitly via the
    `external-dns.alpha.kubernetes.io/target` annotation. Defaults to the
    cluster ingress VIP (where the ingress controller listens); override
    via ``TENANTS_EXTERNAL_DNS_TARGET``. Empty when no ingress_ip is known
    — callers then skip the annotation.
    """
    return (os.getenv("TENANTS_EXTERNAL_DNS_TARGET") or "").strip() \
        or _require_cluster_config().get("ingress_ip") or ""


def _ingress_class() -> str:
    return _require_cluster_config()["ingress_class"]


def _ingress_controller() -> str:
    return _require_cluster_config()["ingress_controller"]


def _mgmt_cidr_drop() -> str | None:
    return _require_cluster_config()["mgmt_cidr"]


def _vpcdns_forward_dns() -> str:
    return _require_cluster_config()["vpcdns_forward_dns"]


def _vpcdns_vip() -> str:
    return _require_cluster_config()["vpcdns_vip"]


def _host_service_cidr() -> str | None:
    return _require_cluster_config().get("host_service_cidr")


# Label Kamaji stamps on the control-plane pods of a TenantControlPlane.
KAMAJI_CP_POD_LABEL = "kamaji.clastix.io/name"


async def restart_control_plane_pods(k8s, tenant_name: str) -> int:
    """Restart a tenant's control-plane pods by deleting them.

    Deliberately NOT `rollout restart`. Kamaji owns the Deployment and reverts
    the pod-template annotation a rollout writes, so the rollout reports
    success while the pods keep their original start time:

        $ kubectl -n <ns> rollout restart deployment <tcp>
        deployment "<tcp>" successfully rolled out
        $ kubectl -n <ns> get pods -l kamaji.clastix.io/name=<tcp> \\
            -o custom-columns=START:.status.startTime
        2026-08-12T13:52:46Z        # unchanged

    Deleting the pods lets the Deployment recreate them, which is the only way
    to make a control-plane container pick up new material on disk. It matters
    for anything that reads a file once at startup and never watches it — the
    Talos CSR signer being the case that found this: after a certificate
    change it keeps serving the old one, and the symptom is misleading,
    because the kubelet still joins fine (that is the apiserver's certificate,
    which Kamaji reloads itself) while Talos waits on `apid` forever.

    Returns the number of pods deleted; 0 means nothing matched, which is not
    an error (the control plane may not be up yet).
    """
    ns = _tenant_ns(tenant_name)
    selector = f"{KAMAJI_CP_POD_LABEL}={tenant_name}"
    try:
        pods = await k8s.core_api.list_namespaced_pod(
            namespace=ns, label_selector=selector,
        )
    except ApiException as e:
        logger.warning(
            f"Could not list control-plane pods for tenant {tenant_name!r}: {e}"
        )
        raise

    deleted = 0
    for pod in pods.items:
        name = pod.metadata.name
        try:
            await k8s.core_api.delete_namespaced_pod(name=name, namespace=ns)
            deleted += 1
        except ApiException as e:
            if e.status == 404:
                continue  # raced with something else deleting it
            logger.warning(f"Could not delete control-plane pod {ns}/{name}: {e}")
            raise

    logger.info(
        f"Restarted {deleted} control-plane pod(s) for tenant {tenant_name!r} "
        f"(selector {selector})"
    )
    return deleted


def find_tenant_cidr_conflicts(
    service_cidr: str,
    pod_cidr: str,
    host_service_cidr: str | None,
    host_dns_ip: str | None,
) -> list[str]:
    """Reasons the tenant's own address plan collides with the host's.

    The expensive one is DNS. A tenant whose service CIDR contains the HOST
    cluster's CoreDNS address makes its own kube-proxy hijack that address to
    the tenant's CoreDNS — which has no endpoints until a CNI is running, and
    the CNI image cannot be pulled because name resolution is now dead:

        failed to resolve reference "ghcr.io/flannel-io/flannel-cni-plugin:…":
          dial tcp: lookup ghcr.io on 127.0.0.53:53: server misbehaving

    The trap is the timing — kube-proxy only starts after the node has joined,
    so this surfaces long after create and reads as anything but DNS.

    Checked in both directions and for both ranges: a tenant pod CIDR
    overlapping the host's service network breaks the same way, and neither
    is Talos-specific — any tenant whose nodes resolve through the host is
    exposed. Returns human-readable reasons; empty means no conflict.
    """
    reasons: list[str] = []

    def _net(cidr: str):
        try:
            return ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return None

    tenant_service = _net(service_cidr)
    tenant_pod = _net(pod_cidr)

    # The DNS address is the specific failure the lab hit, so name it
    # explicitly rather than leaving the operator to infer it from a range.
    if host_dns_ip:
        try:
            dns_addr = ipaddress.ip_address(host_dns_ip)
        except ValueError:
            dns_addr = None
        if dns_addr is not None:
            if tenant_service is not None and dns_addr in tenant_service:
                reasons.append(
                    f"tenant service CIDR {service_cidr} contains the host "
                    f"cluster's DNS address {host_dns_ip} — the tenant's "
                    "kube-proxy would hijack it and its nodes would lose name "
                    "resolution once kube-proxy starts"
                )
            if tenant_pod is not None and dns_addr in tenant_pod:
                reasons.append(
                    f"tenant pod CIDR {pod_cidr} contains the host cluster's "
                    f"DNS address {host_dns_ip}"
                )

    if host_service_cidr:
        host_net = _net(host_service_cidr)
        if host_net is not None:
            for label, cidr, net in (
                ("service", service_cidr, tenant_service),
                ("pod", pod_cidr, tenant_pod),
            ):
                if (
                    net is not None
                    and net.version == host_net.version
                    and net.overlaps(host_net)
                    # Already reported above, with a better explanation.
                    and not any(cidr in r for r in reasons)
                ):
                    reasons.append(
                        f"tenant {label} CIDR {cidr} overlaps the host "
                        f"cluster's service CIDR {host_service_cidr}"
                    )

    return reasons


def assert_tenant_cidrs_free(service_cidr: str, pod_cidr: str) -> None:
    """Raise 422 when the tenant's address plan collides with the host's.

    Deliberately a refusal rather than an auto-shift: silently rewriting a
    caller's address plan is worse than declining it, because the tenant then
    works while its kubeconfig, firewall rules and documentation describe a
    different network. Skipped entirely when the host's own ranges could not
    be discovered — a failed lookup must not block tenant creation.
    """
    reasons = find_tenant_cidr_conflicts(
        service_cidr=service_cidr,
        pod_cidr=pod_cidr,
        host_service_cidr=_host_service_cidr(),
        host_dns_ip=_vpcdns_forward_dns(),
    )
    if reasons:
        raise HTTPException(
            status_code=422,
            detail=(
                "Tenant network overlaps the host cluster: "
                + "; ".join(reasons)
                + ". Pick ranges outside the host's service network."
            ),
        )



async def _get_addon_catalog(k8s) -> AddonCatalog:
    """Read addon catalog from ConfigMap."""
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=CATALOG_CONFIGMAP, namespace=CATALOG_NAMESPACE,
        )
        raw = yaml.safe_load(cm.data.get("catalog.yaml", "{}")) if cm.data else {}
        return AddonCatalog(
            git_repository_ref=raw.get("gitRepositoryRef", {}),
            base_path=raw.get("basePath", "base"),
            components=[AddonComponent(**c) for c in raw.get("components", [])],
        )
    except ApiException as e:
        if e.status == 404:
            logger.warning("Addon catalog ConfigMap not found")
            return AddonCatalog()
        raise


async def _namespace_exists(k8s, ns: str) -> bool:
    try:
        await k8s.core_api.read_namespace(name=ns)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


async def _create_namespace(
    k8s,
    ns: str,
    tenant_name: str,
    worker_type: str = "vm",
    folder: str | None = None,
    environment: str | None = None,
    logical_switch: str | None = None,
) -> None:
    """Create a tenant namespace.

    `folder` + `environment`, when provided, are stamped as
    `kubevirt-ui.io/folder` and `kubevirt-ui.io/environment` labels — wires
    the tenant into Phase 2 folder-level authz (resolve_env, is_env_*).

    `kubevirt-ui.io/enabled=true` is also stamped so the namespace shows up
    in `get_user_namespaces` — this is what makes worker VMs in
    `tenant-<name>` visible in the main /vms list for users who have
    folder/env access (T2).

    `logical_switch`, when provided, is stamped as the
    `ovn.kubernetes.io/logical_switch` annotation BEFORE the namespace POST.
    This pre-stamp is race-resistant: it gets the annotation in place before
    kube-ovn-controller has a chance to observe the new namespace and
    default-claim it to ovn-default. Without this, pods/VMs born in the ns
    land on the cluster default overlay (10.16.0.0/16) even when the tenant
    is attached to a VPC. When `None` (e.g. vpc_name=None, or VPC has no
    default subnet), no annotation is stamped → tenant falls back to overlay.
    """
    labels: dict[str, str] = {
        "kubevirt-ui.io/tenant": tenant_name,
        "kubevirt-ui.io/managed": "true",
        # Enables the namespace to be enumerated by get_user_namespaces (T2)
        # — without this, worker VMs are invisible to non-admin users in /vms.
        "kubevirt-ui.io/enabled": "true",
        "kubevirt-ui.io/worker-type": worker_type,
        # Kamaji control-plane pods + KubeVirt VMs need elevated privileges
        "pod-security.kubernetes.io/enforce": "privileged",
        "pod-security.kubernetes.io/enforce-version": "latest",
        "pod-security.kubernetes.io/warn": "privileged",
        "pod-security.kubernetes.io/audit": "privileged",
    }
    if folder:
        labels["kubevirt-ui.io/folder"] = folder
    if environment:
        labels["kubevirt-ui.io/environment"] = environment
    annotations: dict[str, str] = {}
    if logical_switch:
        annotations["ovn.kubernetes.io/logical_switch"] = logical_switch
    body = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=ns,
            labels=labels,
            annotations=annotations or None,
        ),
    )
    await k8s.core_api.create_namespace(body=body)
