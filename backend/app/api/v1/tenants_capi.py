"""CAPI resource generators and creation for tenants.

Builds Cluster, KamajiControlPlane, KubevirtCluster, MachineDeployment,
KubevirtMachineTemplate, KubeadmConfigTemplate, and Ingress CRs.
"""

import asyncio
import logging
import os
from typing import Any

import yaml

from fastapi import HTTPException
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.core.tenant_transit import (
    attach_vpc_to_transit,
    ensure_tenant_snat,
    TransitSnatSlotTaken,
    ensure_transit_acls,
    remove_tenant_transit,
    transit_subnet_name,
)
from app.models.tenant import TenantCreateRequest

from app.api.v1.tenants_common import (
    CAPI_GROUP,
    CAPI_VERSION,
    KAMAJI_CP_GROUP,
    KAMAJI_CP_VERSION,
    KUBEVIRT_INFRA_GROUP,
    KUBEVIRT_INFRA_VERSION,
    OIDC_ISSUER,
    OIDC_CLIENT_ID,
    _tenant_ns,
    _endpoint_host,
    _ingress_class,
    _ingress_controller,
    _external_dns_target,
)
from app.api.v1.tenants_cp_ports import allocate_cp_ports
from app.api.v1.tenants_cp_vip import (
    TENANT_API_PORT,
    TENANT_KONN_PORT,
    TENANT_TRUSTD_PORT,
    acquire_tenant_vip,
    assert_pool_excluded_from_ovn,
    cp_metallb_pool,
    per_tenant_vip_enabled,
)
from app.api.v1.tenants_ntp import NTP_PORT, worker_time_servers
from app.api.v1.tenants_talos import (
    resolve_talos_release,
    TALOS_TRUSTD_PORT,
    build_talos_config_template,
    read_talos_secrets,
    build_talos_worker_config,
    read_talos_ca_cert,
    talos_service_name,
    await_tenant_k8s_ca_cert,
    talos_bootstrap_ref,
    talos_control_plane_additions,
)

# Image of the Talos CSR signer sidecar. Pinned by digest, not by tag:
# upstream publishes no versioned tags at all — `latest` is the only one that
# exists — so a tag here is a moving target on a single-vendor image with no
# alternative. This digest is the build the lab verified end to end.
#
# The cost of getting it wrong is unusually quiet: with no signer, Talos nodes
# still join and run workloads, but `apid` waits forever — the cluster is
# manageable through Kubernetes and unmanageable through Talos.
#
# Mirroring it into an internal registry is a deployment policy rather than a
# user choice, hence an env knob (TALOS_SIGNER_IMAGE) and not a request field.
TALOS_SIGNER_IMAGE_DEFAULT = (
    "ghcr.io/clastix/talos-csr-signer"
    "@sha256:827b62b5fc2859d66f06f5c1f8d2473ab7109d0600d551269d8ddb98e4a39a18"
)


def _talos_signer_image() -> str:
    return os.getenv("TALOS_SIGNER_IMAGE") or TALOS_SIGNER_IMAGE_DEFAULT

# Konnectivity image overrides — defaults match upstream Kamaji defaults.
# Override for air-gapped clusters via env (e.g. mirror.internal/kas-network-proxy/...).
_DEFAULT_KONNECTIVITY_PROXY_IMAGE = "registry.k8s.io/kas-network-proxy/proxy-server"
_DEFAULT_KONNECTIVITY_AGENT_IMAGE = "registry.k8s.io/kas-network-proxy/proxy-agent"


def _konnectivity_proxy_image() -> str:
    # `or` so an empty env value (e.g. `export TENANTS_KONNECTIVITY_PROXY_IMAGE=""`)
    # falls back to the default rather than producing an empty image string.
    return os.getenv("TENANTS_KONNECTIVITY_PROXY_IMAGE") or _DEFAULT_KONNECTIVITY_PROXY_IMAGE


def _konnectivity_agent_image() -> str:
    return os.getenv("TENANTS_KONNECTIVITY_AGENT_IMAGE") or _DEFAULT_KONNECTIVITY_AGENT_IMAGE


# --- VPC-tenant CP demux (advertiseAddress model) configuration ---------
# The shared MetalLB VIP that fronts every VPC tenant's apiserver +
# konnectivity, demultiplexed by the per-tenant ports from the allocator.
def _cp_demux_vip() -> str | None:
    return os.getenv("TENANTS_CP_DEMUX_VIP") or None


def _cp_metallb_pool() -> str:
    return os.getenv("TENANTS_CP_METALLB_POOL", "traefik")


def _cp_shared_ip_key() -> str:
    return os.getenv("TENANTS_CP_SHARED_IP_KEY", "cp-demux")

logger = logging.getLogger(__name__)

def _resolve_worker_container_disk_image(req: TenantCreateRequest) -> str:
    """Resolve the OCI image for the worker VM's root containerDisk.

    Priority:
      1. req.worker_image_url when source_type == 'registry' (per-tenant choice)
      2. TENANTS_DEFAULT_WORKER_IMAGE env (cluster-level default)
      3. raise — no silent default, no hardcoded private registry.
    """
    if req.worker_image_source_type == "registry" and req.worker_image_url:
        return req.worker_image_url
    env_default = os.getenv("TENANTS_DEFAULT_WORKER_IMAGE")
    if env_default:
        return env_default
    raise HTTPException(
        status_code=400,
        detail=(
            "No worker container image: set worker_image_source_type='registry' "
            "+ worker_image_url in the request, or TENANTS_DEFAULT_WORKER_IMAGE "
            "env on the backend."
        ),
    )


def _build_cluster_cr(
    req: TenantCreateRequest,
    cp_host: str | None = None,
    api_port: int | None = None,
    tenant_vip: str | None = None,
) -> dict[str, Any]:
    # Two worker-reachability models, keyed on api_port:
    #
    # default overlay (api_port=None): workers join the Kamaji TCP Service
    #   ClusterIP, natively routable on the default overlay (kube-proxy
    #   DNATs it). We bake a DNS placeholder + port 6443 here and
    #   `_create_capi_resources` PATCHes host→ClusterIP after the Service
    #   is ready. cluster-info matches the ClusterIP, no further work.
    #
    # VPC (api_port set): the advertiseAddress model. apiServerPort flows
    #   to NetworkProfile.Port → apiserver --secure-port AND the port
    #   advertised in cluster-info as <advertiseAddress>:<api_port>. That
    #   address is the shared MetalLB CP VIP, reachable from the isolated
    #   VPC, so kube-proxy DNATs 10.96.0.1 natively — no node-DNAT, no
    #   ClusterIP patch.
    #
    #   controlPlaneEndpoint used to be the external Traefik SNI host:443,
    #   on the assumption that cluster-info already carries the VIP so the
    #   worker would use it. It cannot: CAPI copies this field into the
    #   worker's `discovery.bootstrapToken.apiServerEndpoint`, and kubeadm
    #   has to *fetch* cluster-info before it can learn anything from it.
    #   So the worker dialled the ingress name, which an isolated VPC can
    #   neither resolve (its DNS is public and unreachable) nor route to:
    #
    #       error execution phase preflight: couldn't validate the identity
    #       of the API Server: Get "https://t1.<ingress>/.../cluster-info":
    #       lookup t1.<ingress> on 8.8.8.8:53: i/o timeout
    #
    #   Three lab runs read that as "CAPK will not bootstrap". It is simply
    #   the wrong address: the whole transit datapath — VIP, EIP, SNAT,
    #   guard ACLs — was never on the path the worker took.
    #
    #   Pointing this at the VIP costs nothing outside: `GET
    #   /tenants/{name}/kubeconfig` rewrites `server` to the ingress host
    #   for both admin and OIDC kubeconfigs, so kubectl-from-outside never
    #   reads this field.
    #   With a VIP per tenant the address is no longer a deployment constant:
    #   MetalLB assigns it and the caller passes it in. Falling back to the
    #   env VIP here would point every tenant's endpoint at the old shared
    #   address while its own Service holds a different one — green spec,
    #   unreachable control plane.
    vpc = api_port is not None
    host = cp_host or _endpoint_host(req.name)
    on_vip = False
    if vpc and not cp_host:
        vip = tenant_vip or _cp_demux_vip()
        if vip:
            host, on_vip = vip, True
    cluster_network: dict[str, Any] = {
        "pods": {"cidrBlocks": [req.pod_cidr]},
        "services": {"cidrBlocks": [req.service_cidr]},
    }
    if vpc:
        cluster_network["apiServerPort"] = api_port
    return {
        "apiVersion": f"{CAPI_GROUP}/{CAPI_VERSION}",
        "kind": "Cluster",
        "metadata": {
            "name": req.name,
            "namespace": _tenant_ns(req.name),
            "labels": {
                "kubevirt-ui.io/tenant": req.name,
                # Bind the tenant to its env namespace ({folder}-{environment})
                # so the UI can scope the tenant list by the global namespace
                # selector. Omitted for legacy/folderless tenants.
                **({"kubevirt-ui.io/folder": req.folder} if req.folder else {}),
                **({"kubevirt-ui.io/environment": req.environment} if req.environment else {}),
            },
            "annotations": {
                "kubevirt-ui.io/display-name": req.display_name,
                "kubevirt-ui.io/worker-type": req.worker_type,
                "kubevirt-ui.io/enable-oidc": "true" if req.enable_oidc else "false",
            },
        },
        "spec": {
            "controlPlaneEndpoint": {
                "host": host,
                # The VIP listens on the tenant's own api port; an ingress
                # name terminates SNI on 443.
                "port": api_port if on_vip else (443 if vpc else 6443),
            },
            "clusterNetwork": cluster_network,
            "controlPlaneRef": {
                # KamajiControlPlane lives in the same tenant namespace —
                # CAPI v1beta1 webhook `validation.cluster.cluster.x-k8s.io`
                # rejects cross-namespace controlPlaneRefs, so this MUST stay
                # same-ns as the Cluster CR.
                "apiVersion": f"{KAMAJI_CP_GROUP}/{KAMAJI_CP_VERSION}",
                "kind": "KamajiControlPlane",
                "name": req.name,
            },
            "infrastructureRef": {
                "apiVersion": f"{KUBEVIRT_INFRA_GROUP}/{KUBEVIRT_INFRA_VERSION}",
                "kind": "KubevirtCluster",
                "name": req.name,
            },
        },
    }


def _build_cp_lb_service(
    req: TenantCreateRequest, vip: str, api_port: int, konn_port: int,
) -> dict[str, Any]:
    """Per-tenant MetalLB shared-IP Service fronting this tenant's CP pods.

    All VPC tenants share ONE VIP via metallb.universe.tf/allow-shared-ip;
    MetalLB demultiplexes by the per-tenant (api_port, konn_port) — which
    the allocator guarantees are globally non-overlapping. The selector
    `kamaji.clastix.io/name=<tenant>` targets exactly this tenant's Kamaji
    control-plane pods. `service.cilium.io/type: ClusterIP` opts the VIP
    out of Cilium's LB BPF DNAT (same fix as the ingress Services) so
    in-VPC pod traffic isn't rewritten before kube-ovn routing.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{req.name}-cp-lb",
            "namespace": _tenant_ns(req.name),
            "labels": {
                "kubevirt-ui.io/tenant": req.name,
                "kubevirt-ui.io/managed": "true",
            },
            "annotations": {
                "service.cilium.io/type": "ClusterIP",
                "metallb.universe.tf/address-pool": _cp_metallb_pool(),
                "metallb.universe.tf/loadBalancerIPs": vip,
                "metallb.universe.tf/allow-shared-ip": _cp_shared_ip_key(),
            },
        },
        "spec": {
            "type": "LoadBalancer",
            "selector": {"kamaji.clastix.io/name": req.name},
            "ports": [
                {"name": "api", "port": api_port, "targetPort": api_port, "protocol": "TCP"},
                {"name": "konn", "port": konn_port, "targetPort": konn_port, "protocol": "TCP"},
            ],
        },
    }


def _build_kamaji_cp_cr(
    req: TenantCreateRequest,
    advertise_vip: str | None = None,
    konn_port: int | None = None,
) -> dict[str, Any]:
    """Build KamajiControlPlane CR.

    TCP CR is placed in the tenant namespace so the CAPI v1beta1 webhook
    accepts the cross-ref from the Cluster CR (which rejects
    ``controlPlaneRef.namespace != metadata.namespace``). The tenant ns
    itself lives on the cluster default overlay (no VPC attach), so the
    Kamaji TCP pod can reach Postgres, the shared cluster Ingress, and
    other platform services in the default VPC out of the box.

    Worker VM launcher pods cross into the tenant VPC via a per-pod
    ``ovn.kubernetes.io/logical_switch`` annotation that targets the VPC
    default subnet (see ``_build_kubevirt_machine_template_cr``). This
    mirrors the solo-VM model in ``vms.py`` and lets kube-ovn CNI configure
    gateway + DNS via the primary plugin path — DHCP is delivered to the
    guest just like for a solo VPC VM.

    The TCP → worker path uses the konnectivity tunnel (agent dials out
    from each worker to the konnectivity-server sidecar on the TCP pod).
    The worker → TCP path uses the cluster Ingress (TLS passthrough to the
    TCP Service in the tenant ns), so cross-VPC reachability is not
    required. See decision memo: ``tcp-konnectivity-decision.md``.
    """
    apiserver_extra_args: list[str] = []
    # OIDC injection is gated by req.enable_oidc (per-tenant toggle) AND
    # a valid cluster-wide OIDC_ISSUER. When the tenant opts out, the
    # apiserver runs without any --oidc-* flags — no Dex round-trips at
    # all. Useful while the cluster's Dex isn't reachable from inside
    # the cluster (MetalLB-only routing) or its TLS cert isn't trusted
    # by the apiserver pod (self-signed Traefik).
    if req.enable_oidc and OIDC_ISSUER and OIDC_ISSUER.startswith("https://"):
        apiserver_extra_args += [
            f"--oidc-issuer-url={OIDC_ISSUER}",
            f"--oidc-client-id={OIDC_CLIENT_ID}",
            "--oidc-username-claim=email",
            "--oidc-groups-claim=groups",
        ]

    pod_labels = {
        "cluster.x-k8s.io/cluster-name": req.name,
        "cluster.x-k8s.io/role": "control-plane",
    }
    pod_additional_metadata: dict[str, Any] = {"labels": pod_labels}

    # VPC (advertiseAddress) model: per-tenant konnectivity port on the
    # shared CP VIP. Must avoid Kamaji's hardcoded admin=8133/health=8134
    # — guaranteed by the allocator. Default overlay uses 8132.
    konn_server_port = konn_port if konn_port is not None else 8132

    spec: dict[str, Any] = {
        "replicas": req.control_plane_replicas,
        "version": req.kubernetes_version,
        "dataStoreName": "default",
        "addons": {
            "coreDNS": {},
            "kubeProxy": {},
            "konnectivity": {
                "server": {
                    "port": konn_server_port,
                    "image": _konnectivity_proxy_image(),
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                    },
                },
                "agent": {
                    "image": _konnectivity_agent_image(),
                },
            },
        },
        "kubelet": {
            "cgroupfs": "systemd",
            "preferredAddressTypes": ["InternalIP", "ExternalIP"],
        },
        "network": {
            # Kamaji defaults: apiserver listens on 6443, Service exposes
            # 6443. We don't override `serviceAddress` (NodePort/VIP-only
            # field — useless under ClusterIP) or `advertiseAddress`
            # (companion port can't be 443 without crashing apiserver
            # 1.30+; admin patches kube-public/cluster-info manually per
            # tenant — see PLAN-REVERT-CP-PORTS.md choice α). The single
            # `certSANs` entry below makes the Ingress hostname valid in
            # the apiserver cert so workers don't trip TLS on join.
            "serviceType": "ClusterIP",
            "certSANs": [_endpoint_host(req.name)],
        },
        "deployment": {
            "podAdditionalMetadata": pod_additional_metadata,
        },
    }

    # Talos workers dial a CSR signer that runs beside the apiserver, so the
    # control plane grows a sidecar, its certificate volume, and the DNS names
    # the workers use — which must also be in certSANs or the join fails TLS
    # before trustd is ever reached.
    if req.worker_os == "talos":
        talos_add = talos_control_plane_additions(
            req.name, _tenant_ns(req.name), _talos_signer_image(),
            shared_vip=advertise_vip is not None,
        )
        # KamajiControlPlane names these `extraContainers`/`extraVolumes`;
        # TenantControlPlane calls the same things `additionalContainers`/
        # `additionalVolumes`. Writing the TCP names here is not an error the
        # API reports — unknown fields are pruned silently, so the object
        # applies cleanly, the tenant comes up Ready, and the signer sidecar
        # simply is not there. The worker then waits for a certificate that
        # nothing will ever issue.
        spec["deployment"]["extraContainers"] = talos_add["additionalContainers"]
        spec["deployment"]["extraVolumes"] = talos_add["additionalVolumes"]
        spec["network"]["certSANs"] = [
            *spec["network"]["certSANs"], *talos_add["certSANs"],
        ]
        if "additionalPorts" in talos_add:
            spec["network"]["additionalPorts"] = talos_add["additionalPorts"]
    # VPC (advertiseAddress) model: advertise the shared CP VIP so
    # cluster-info points workers at <vip>:<apiServerPort> (reachable from
    # the isolated VPC). serviceType stays ClusterIP. Default overlay omits
    # this and relies on the ClusterIP being natively routable.
    if advertise_vip is not None:
        spec["network"]["advertiseAddress"] = advertise_vip
    if apiserver_extra_args:
        spec["apiServer"] = {"extraArgs": apiserver_extra_args}

    return {
        "apiVersion": f"{KAMAJI_CP_GROUP}/{KAMAJI_CP_VERSION}",
        "kind": "KamajiControlPlane",
        "metadata": {
            "name": req.name,
            "namespace": _tenant_ns(req.name),
        },
        "spec": spec,
    }


def _build_kubevirt_cluster_cr(
    req: TenantCreateRequest,
    storage_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the KubevirtCluster CR.

    When `storage_info` is provided (tenant storage enabled), wires up
    `spec.infraClusterSecretRef` so CAPK / kubevirt-csi can authenticate
    against the host cluster.

    The CAPI Provider KubeVirt v1alpha1 `KubevirtClusterSpec` defines
    exactly four fields: `controlPlaneEndpoint`, `controlPlaneServiceTemplate`,
    `sshKeys`, `infraClusterSecretRef`. There is **no**
    `infraClusterStorageClassName` field on the spec — the apiserver
    silently drops unknown fields, so emitting it would be a no-op lie.
    Tenant storage class plumbing goes through the kubevirt-csi-driver
    chart values instead (`storageClasses[].infraStorageClassName`,
    wired via the `INFRA_STORAGE_CLASS_NAME` addon param in
    `create_tenant`). See:
    https://github.com/kubernetes-sigs/cluster-api-provider-kubevirt/blob/main/api/v1alpha1/kubevirtcluster_types.go
    """
    spec: dict[str, Any] = {}
    if storage_info:
        # `capk_secret_name` — NOT `secret_name`. Setting this field takes
        # CAPK off its own manager identity for every host-cluster call it
        # makes for this tenant, so it must reference the CAPK-scoped
        # credentials; the CSI secret is replicated into the tenant cluster
        # and is intentionally too narrow (and too exposed) for that job.
        # See tenants_storage._capk_role_rules.
        spec["infraClusterSecretRef"] = {
            "name": storage_info.get("capk_secret_name") or storage_info["secret_name"],
            "namespace": storage_info["secret_namespace"],
        }

    return {
        "apiVersion": f"{KUBEVIRT_INFRA_GROUP}/{KUBEVIRT_INFRA_VERSION}",
        "kind": "KubevirtCluster",
        "metadata": {
            "name": req.name,
            "namespace": _tenant_ns(req.name),
            "annotations": {
                # Tells CAPK that the control plane is externally managed by Kamaji.
                # This prevents CAPK from creating the {name}-lb Service (which has
                # wrong selectors) and lets the Kamaji CP provider manage the
                # KubevirtCluster control plane endpoint directly.
                "cluster.x-k8s.io/managed-by": "kamaji",
            },
        },
        "spec": spec,
    }


# How long a worker may be NotReady before it is replaced.
#
# Derived, not chosen. A Talos worker rebooted through the UI came back in
# about **3 minutes** (VMI recreated, node rejoined, tenant Ready 2/2 —
# measured on t3fix). The previous 5m left ninety seconds of margin, and the
# MHC did fire `DetectedUnhealthy` during that reboot: it started its clock
# and only failed to remediate because the node beat it. A slower boot — a
# larger image, a busy node, a loaded Ceph — crosses that line, and the
# operation that crosses it is an ordinary reboot.
#
# Three times the measured return is the rule, so the number moves when the
# measurement does instead of being re-guessed. The cost is stated rather
# than hidden: a genuinely dead worker is now detected ~5 minutes later. That
# trade is deliberate for persistent nodes — a false replacement re-clones a
# 20Gi root and churns the node's identity, which is dearer than waiting.
#
# `nodeStartupTimeout` is left at 20m: it governs a node that has never
# joined, was verified sufficient, and is not what this is about.
WORKER_RETURN_TIME_OBSERVED = 3          # minutes, measured on t3fix
WORKER_UNHEALTHY_TIMEOUT = f"{WORKER_RETURN_TIME_OBSERVED * 3}m"


def _build_machine_health_check_cr(req: Any) -> dict[str, Any]:
    """Replace a worker whose node stops being Ready.

    Without this nothing notices. Killing a worker's VMI on the cluster
    brought the VM straight back — same name, fresh container disk, no
    kubelet configuration on it — and the tenant kept a node that never
    returned:

        tci-workers-dhnn8-96ww7   NotReady   (8+ minutes, no remediation)
        Machine: Ready=True InfrastructureReady=True NodeHealthy=Unknown

    CAPI is content because the infrastructure VM exists; only the *node*
    is gone. A MachineHealthCheck is the piece that turns that into a
    replacement.

    `maxUnhealthy: 100%` on purpose: a one-worker tenant is the common case
    here, and the usual 40% guard would refuse to remediate the only worker —
    exactly when the tenant is fully down and most needs it.
    """
    ns = _tenant_ns(req.name)
    return {
        "apiVersion": f"{CAPI_GROUP}/{CAPI_VERSION}",
        "kind": "MachineHealthCheck",
        "metadata": {
            "name": f"{req.name}-workers",
            "namespace": ns,
            "labels": {"kubevirt-ui.io/tenant": req.name},
        },
        "spec": {
            "clusterName": req.name,
            "maxUnhealthy": "100%",
            "nodeStartupTimeout": "20m",
            "selector": {"matchLabels": {
                "cluster.x-k8s.io/deployment-name": f"{req.name}-workers",
            }},
            "unhealthyConditions": [
                {"type": "Ready", "status": "False", "timeout": WORKER_UNHEALTHY_TIMEOUT},
                {"type": "Ready", "status": "Unknown", "timeout": WORKER_UNHEALTHY_TIMEOUT},
            ],
        },
    }


def _build_machine_deployment_cr(req: TenantCreateRequest) -> dict[str, Any]:
    return {
        "apiVersion": f"{CAPI_GROUP}/{CAPI_VERSION}",
        "kind": "MachineDeployment",
        "metadata": {
            "name": f"{req.name}-workers",
            "namespace": _tenant_ns(req.name),
        },
        "spec": {
            "clusterName": req.name,
            "replicas": req.worker_count,
            "selector": {
                "matchLabels": {},
            },
            "template": {
                "spec": {
                    "clusterName": req.name,
                    # Remediating a dead worker means deleting its Machine,
                    # and CAPI drains the node first. A node that is gone
                    # cannot be drained: on the cluster it stopped at
                    #
                    #   DrainingSucceeded=False Draining: cannot evict pod as
                    #   it would violate the pod's disruption budget. The
                    #   disruption budget calico-typha needs 0 healthy pods
                    #   and has 0 currently
                    #
                    # and with no timeout CAPI retried that forever — the
                    # replacement worker never arrived and the tenant kept a
                    # NotReady node indefinitely.
                    "nodeDrainTimeout": "5m",
                    "version": req.kubernetes_version,
                    # Talos nodes are not bootstrapped by kubeadm — they get a
                    # machine config and ask a trustd signer for a certificate,
                    # which is a different provider entirely.
                    "bootstrap": {
                        "configRef": (
                            talos_bootstrap_ref(req.name)
                            if req.worker_os == "talos"
                            else {
                                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                                "kind": "KubeadmConfigTemplate",
                                "name": f"{req.name}-workers",
                            }
                        ),
                    },
                    "infrastructureRef": {
                        "apiVersion": f"{KUBEVIRT_INFRA_GROUP}/{KUBEVIRT_INFRA_VERSION}",
                        "kind": "KubevirtMachineTemplate",
                        "name": f"{req.name}-workers",
                    },
                },
            },
        },
    }


def talos_golden_pvc_name(req: TenantCreateRequest) -> str:
    """PVC the Talos workers clone their root disk from."""
    return f"{req.name}-talos-golden"


# Name of the per-worker root template. CAPK rewrites
# `dataVolumeTemplates[].metadata.name` to `<vm-name>-<template-name>` and
# fixes `volumes[].dataVolume.name` to match — proved by experiment on a
# two-worker MachineDeployment, two separate DVs each with its own controller
# owner (TARGET-LAB-T3-EXPERIMENT-LOG.md). So the literal here is a suffix,
# not the DV's actual name.
WORKER_ROOT_TEMPLATE = "root"


def _build_worker_root_volume(req: TenantCreateRequest) -> dict[str, Any]:
    """Root volume for a worker VM.

    cloud-init workers boot a CAPK containerDisk — unchanged. Talos cannot:
    it boots a disk image, and its machine config arrives through the
    cloud-init disk KubeVirt attaches (which is what the `nocloud` image
    variant reads). So its root comes from a clone of the imported golden
    image.

    A *clone*, per worker. This used to point every worker straight at the
    golden PVC by name, and the consequences were measured rather than
    feared:

      * with one worker the golden image is no longer golden — the node
        writes into it, so the next tenant clones a used disk;
      * with two it is two writers on one raw block device, and RWX means
        nothing even complains;
      * and the sharp one: the DV takes the first VM as its owner with
        `blockOwnerDeletion`, so deleting worker A — a rolling update, an MHC
        replacement, a scale-down — deletes the DV and re-imports it. Worker
        B's root disk is wiped by a routine operation, silently.
    """
    if req.worker_os == "talos":
        return {
            "name": "root",
            "dataVolume": {"name": WORKER_ROOT_TEMPLATE},
        }
    return {
        "name": "root",
        "containerDisk": {"image": _resolve_worker_container_disk_image(req)},
    }


def _build_worker_data_volume_templates(req: TenantCreateRequest) -> list[dict[str, Any]]:
    """One root disk per worker, cloned from the tenant's golden image.

    `source.pvc` + `storage` (not `pvc`) is the shape the VM-create path
    already uses: CDI then takes the clone strategy from the *target* storage
    profile instead of being told one that the backend may not support.

    Size follows the golden image unless the tenant asked for more — a clone
    smaller than its source is rejected by CDI at admission, which would fail
    every worker with an error nobody would connect to a disk-size field.
    """
    if req.worker_os != "talos":
        return []

    storage: dict[str, Any] = {
        "resources": {"requests": {"storage": _worker_root_size(req)}},
    }
    if req.storage_class:
        storage["storageClassName"] = req.storage_class

    from app.api.v1.tenants_talos import golden_name, golden_namespace

    release = resolve_talos_release(req.kubernetes_version, req.talos_version)

    return [{
        "metadata": {
            "name": WORKER_ROOT_TEMPLATE,
            "labels": {
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/tenant": req.name,
                "kubevirt-ui.io/worker-root": "true",
            },
        },
        "spec": {
            # The shared golden, not a per-tenant copy. Crossing the
            # namespace boundary is what CDI gates on `datavolumes/source`
            # (measured: without it the webhook refuses the clone), and it is
            # the whole point of T2 — one import per Talos version instead of
            # one per tenant.
            "source": {
                "pvc": {
                    "name": golden_name(release.talos),
                    "namespace": golden_namespace(),
                },
            },
            "storage": storage,
        },
    }]


def _worker_root_size(req: TenantCreateRequest) -> str:
    """At least the golden image, and never silently smaller than asked."""
    from app.api.v1.tenants_talos import DEFAULT_TALOS_GOLDEN_SIZE

    return _larger_size(req.worker_disk, DEFAULT_TALOS_GOLDEN_SIZE)


def _larger_size(a: str, b: str) -> str:
    """The bigger of two Kubernetes quantities, falling back to `b` if either
    cannot be parsed — the golden size is the one that must not be undercut."""
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
             "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}

    def _bytes(q: str) -> int | None:
        q = (q or "").strip()
        for suffix, mult in units.items():
            if q.endswith(suffix):
                try:
                    return int(float(q[: -len(suffix)]) * mult)
                except ValueError:
                    return None
        try:
            return int(q)
        except ValueError:
            return None

    av, bv = _bytes(a), _bytes(b)
    if av is None or bv is None:
        return b
    return a if av >= bv else b


def _build_kubevirt_machine_template_cr(req: TenantCreateRequest) -> dict[str, Any]:
    ns = _tenant_ns(req.name)

    pod_annotations: dict[str, str] = {}

    # T1 — stamp tenant label on every KubevirtMachine so a future
    # "Tenant > Workers" tab can filter VMs by tenant. Inherited by the
    # underlying VirtualMachine via KubevirtMachineTemplate.
    # T2 — also stamp folder + environment so worker VMs match the same
    # label-selector filters as project/env-scoped VMs in the /vms list.
    pod_labels: dict[str, str] = {
        "kubevirt-ui.io/tenant": req.name,
        "kubevirt-ui.io/folder": req.folder,
        "kubevirt-ui.io/environment": req.environment,
    }

    # Single NIC on the kube-ovn primary plugin. Placement depends on
    # req.vpc_name:
    #   None      → cluster default overlay; Kamaji TCP ClusterIP natively
    #               reachable for kubeadm join (default-overlay model).
    #   <vpc>     → the VPC's default subnet via the per-pod
    #               ovn.kubernetes.io/logical_switch annotation; worker→CP
    #               reachability is the advertiseAddress model (shared VIP).
    nic_binding = "masquerade" if req.worker_network_binding == "masquerade" else "bridge"
    primary_iface: dict[str, Any] = {"name": "default", nic_binding: {}}
    networks: list[dict[str, Any]] = [{"name": "default", "pod": {}}]
    # bridge binding requires the kubevirt.io/allow-pod-bridge-network-live-migration
    # pod annotation to permit live migration on a pod-network bridge interface.
    if nic_binding == "bridge":
        pod_annotations["kubevirt.io/allow-pod-bridge-network-live-migration"] = "true"

    # VPC workers: pin the launcher pod to the VPC default subnet. CAPK
    # (in the default overlay) then can't SSH into the isolated worker, so
    # the default `ssh` bootstrap check would fail forever and the
    # MachineDeployment would read 0-ready even though nodes are fine —
    # `none` skips only that verification (kubeadm/cloud-init still run).
    if req.vpc_name:
        pod_annotations["ovn.kubernetes.io/logical_switch"] = f"{req.vpc_name}-default"
        check_strategy = "none"
    else:
        check_strategy = "ssh"

    return {
        "apiVersion": f"{KUBEVIRT_INFRA_GROUP}/{KUBEVIRT_INFRA_VERSION}",
        "kind": "KubevirtMachineTemplate",
        "metadata": {
            "name": f"{req.name}-workers",
            "namespace": ns,
        },
        "spec": {
            "template": {
                "spec": {
                    "virtualMachineBootstrapCheck": {
                        "checkStrategy": check_strategy,
                    },
                    "virtualMachineTemplate": {
                        "spec": {
                            "runStrategy": "Always",
                            **(
                                {"dataVolumeTemplates": _build_worker_data_volume_templates(req)}
                                if req.worker_os == "talos" else {}
                            ),
                            "template": {
                                "metadata": {
                                    "labels": pod_labels,
                                    **({"annotations": pod_annotations} if pod_annotations else {}),
                                },
                                "spec": {
                                    # Forward imagePullSecrets to kubelet so the
                                    # worker containerDisk can be pulled from a
                                    # private registry. Secrets themselves must
                                    # already exist in the tenant namespace.
                                    **(
                                        {"imagePullSecrets": [{"name": s} for s in req.worker_image_pull_secrets]}
                                        if req.worker_image_pull_secrets else {}
                                    ),
                                    "domain": {
                                        "cpu": {"cores": req.worker_vcpu},
                                        "memory": {"guest": req.worker_memory},
                                        "devices": {
                                            "networkInterfaceMultiqueue": True,
                                            "interfaces": [primary_iface],
                                            "disks": [
                                                {
                                                    "name": "root",
                                                    "disk": {"bus": "virtio"},
                                                },
                                                {
                                                    "name": "data",
                                                    "disk": {"bus": "virtio"},
                                                },
                                            ],
                                        },
                                    },
                                    "networks": networks,
                                    "evictionStrategy": "External",
                                    "volumes": [
                                        # Talos boots a disk image, not a
                                        # containerDisk: the golden image is a
                                        # nocloud raw.xz that CDI imports and
                                        # each worker clones. cloud-init
                                        # workers keep the containerDisk they
                                        # have always used.
                                        _build_worker_root_volume(req),
                                        {
                                            "name": "data",
                                            "emptyDisk": {
                                                "capacity": req.worker_disk,
                                            },
                                        },
                                    ],
                                },
                            },
                        },
                    },
                },
            },
        },
    }


_TENANT_WORKER_PRIMARY_DNS = "1.1.1.1"
_TENANT_WORKER_FALLBACK_DNS = "8.8.8.8"


def _talos_worker_nameservers(req: TenantCreateRequest) -> list[str]:
    """Resolvers for a Talos worker, which has no cloud-init to fix them up.

    A VPC worker must use the VpcDns VIP: the public resolvers below are only
    reachable once the VPC has internet egress, and a tenant is normally
    created before any gateway is attached to it. The VIP resolves public
    names too (it forwards to the cluster's CoreDNS), so it covers image
    pulls as well as anything in-fabric.

    Outside a VPC the guest is on the default overlay and the ordinary list
    applies.
    """
    from app.api.v1.tenants_common import _vpcdns_vip

    public = _resolve_worker_dns(req)
    if not req.vpc_name:
        return public
    vip = _vpcdns_vip()
    return [vip, *public] if vip else public


def _resolve_worker_dns(req: TenantCreateRequest) -> list[str]:
    """Compute the DNS server list to write into worker guest /etc/resolv.conf.

    These are the servers the worker VM's guest OS uses to resolve internet
    hostnames (image pulls, package installs, cloud-init payloads). NOT the
    same as kubelet `--cluster-dns` which kubeadm sets to the tenant's own
    CoreDNS ClusterIP for in-cluster service resolution by pods running on
    this worker.

    Default primary is a public resolver (Cloudflare 1.1.1.1). The host or
    tenant cluster CoreDNS would be useless here — host's only knows host's
    `cluster.local`, tenant's resolves through its own apiserver which the
    worker can't reach until after kubeadm join.

    Modes:
      - "append":   primary + dns_servers + public_fallback (when enabled)
      - "override": dns_servers only (primary used as safety net if empty)
    """
    custom = [s.strip() for s in (req.dns_servers or []) if s.strip()]

    if req.dns_mode == "override":
        return custom or [_TENANT_WORKER_PRIMARY_DNS]
    # append mode
    out = [_TENANT_WORKER_PRIMARY_DNS] + custom
    if req.dns_include_public_fallback:
        out.append(_TENANT_WORKER_FALLBACK_DNS)
    return out


def _build_worker_resolv_conf(req: TenantCreateRequest) -> str:
    """Render `/etc/resolv.conf` content for tenant worker guest VMs."""
    dns_list = _resolve_worker_dns(req)
    return "\n".join(f"nameserver {ip}" for ip in dns_list) + "\n"


def _build_kubeadm_config_template_cr(
    req: TenantCreateRequest,
) -> dict[str, Any]:
    """Build KubeadmConfigTemplate CR.

    Container disk has all packages pre-baked (containerd, kubelet, kubeadm,
    kubectl, CNI plugins). Only need: storage prep + DNS fix + kubeadm join.

    T4: previously this function injected an iptables DNAT rule to redirect
    the apiserver ClusterIP → TCP pod's fixed VPC IP when isolation was on.
    Tenant ns is now bound to the folder VPC directly (TCP + workers share
    one subnet), so ClusterIP traffic just works and the DNAT hack is gone.
    """
    # Compute /etc/resolv.conf content once so we can write it with a
    # heredoc that survives DHCP / systemd-resolved rewrites.
    resolv_lines = [f"nameserver {ip}" for ip in _resolve_worker_dns(req)]
    resolv_heredoc = "\\n".join(resolv_lines)

    pre_commands = [
        # --- DNS: bulletproof override ----------------------------------
        # CAPK container-disk images may have /etc/resolv.conf as a symlink
        # to /run/systemd/resolve/stub-resolv.conf, in which case chattr +i
        # on the symlink doesn't make the target immutable. Remove the
        # symlink, write a regular file with our nameservers, then lock it.
        # systemd-resolved / netplan / DHCP renewals subsequently fail with
        # EPERM and our list stays put.
        "rm -f /etc/resolv.conf",
        f"printf '{resolv_heredoc}\\n' > /etc/resolv.conf",
        "chattr +i /etc/resolv.conf",
        # --- Storage: mount emptyDisk (/dev/vdb) for containerd + kubelet ---
        # ContainerDisk overlay reports 0 capacity → kubelet InvalidDiskCapacity.
        "systemctl mask kubelet",
        "systemctl stop kubelet || true",
        "systemctl stop containerd || true",
        "mkfs.ext4 -F /dev/vdb",
        "mkdir -p /mnt/data",
        "mount /dev/vdb /mnt/data",
        # Copy ALL of /var/lib to real disk (containerd, kubelet, etc.)
        "cp -a /var/lib/. /mnt/data/",
        "umount /mnt/data",
        # Mount /dev/vdb over /var/lib — gives real disk capacity to cadvisor
        "mount /dev/vdb /var/lib",
        "systemctl start containerd",
        "systemctl unmask kubelet",
        # --- Kubelet config fix: strip fields from newer K8s versions ---
        # Kamaji control plane generates KubeletConfiguration with 1.32+ fields
        # (crashLoopBackOff, failCgroupV1, etc.) that crash kubelet 1.30.
        # Install a systemd drop-in that strips unknown fields before kubelet starts.
        # systemd daemon-reload to pick up kubelet config fix drop-in (written via files)
        "systemctl daemon-reload",
    ]

    return {
        "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
        "kind": "KubeadmConfigTemplate",
        "metadata": {
            "name": f"{req.name}-workers",
            "namespace": _tenant_ns(req.name),
        },
        "spec": {
            "template": {
                "spec": {
                    "files": [
                        {
                            # Written as a file, not an inline command: the
                            # inline shape cost a whole 15-minute tenant cycle
                            # to shell quoting instead of the bug. A script is
                            # quoted once, here, where it can be read.
                            "path": "/usr/local/bin/kubelet-postmortem.sh",
                            "owner": "root:root",
                            "permissions": "0755",
                            "content": (
                                "#!/bin/sh\n"
                                "exec > /dev/ttyS0 2>&1\n"
                                "for i in 1 2 3 4; do\n"
                                "  sleep 60\n"
                                "  echo \"===== postmortem $i =====\"\n"
                                "  echo '--- kubeadm-flags.env:'\n"
                                "  cat /var/lib/kubelet/kubeadm-flags.env\n"
                                "  echo '--- config.yaml, registration-related:'\n"
                                "  grep -iE 'registerNode|staticPodPath|providerID|failSwapOn'"
                                " /var/lib/kubelet/config.yaml\n"
                                "  echo '--- times it tried to register:'\n"
                                "  grep -c 'Attempting to register node'"
                                " /var/log/kubelet-stderr.log\n"
                                "  echo '--- last words:'\n"
                                "  tail -6 /var/log/kubelet-stderr.log\n"
                                "done\n"
                            ),
                        },
                        {
                            # klog writes to stderr, and so does whatever kills
                            # the process. Sending stderr to a file that
                            # systemd truncates on every start means the file
                            # holds exactly one kubelet lifetime — and its tail
                            # is the death. That is the one thing the console
                            # could never give us: the console rate-limits
                            # under the ~10 lines/s this kubelet produces, so
                            # the fatal line was always among the dropped.
                            "path": "/etc/systemd/system/kubelet.service.d/99-stderr-to-file.conf",
                            "owner": "root:root",
                            "permissions": "0644",
                            "content": (
                                "[Service]\n"
                                "StandardError=file:/var/log/kubelet-stderr.log\n"
                            ),
                        },
                        {
                            "path": "/usr/local/bin/fix-kubelet-config.sh",
                            "owner": "root:root",
                            "permissions": "0755",
                            "content": (
                                "#!/bin/bash\n"
                                "# Strip Kamaji-generated kubelet config fields that don't exist in K8s 1.30\n"
                                "if [ -f /var/lib/kubelet/config.yaml ]; then\n"
                                "  grep -v '^crashLoopBackOff:\\|^  maxContainerRestartPeriod:\\|^failCgroupV1:\\|^imagePullCredentialsVerificationPolicy:\\|^mergeDefaultEvictionSettings:' "
                                "/var/lib/kubelet/config.yaml > /tmp/kubelet-config-clean.yaml\n"
                                "  mv /tmp/kubelet-config-clean.yaml /var/lib/kubelet/config.yaml\n"
                                "fi\n"
                            ),
                        },
                        {
                            "path": "/etc/systemd/system/kubelet.service.d/10-fix-config.conf",
                            "owner": "root:root",
                            "permissions": "0644",
                            "content": (
                                "[Service]\n"
                                "ExecStartPre=/usr/local/bin/fix-kubelet-config.sh\n"
                            ),
                        },
                        # DNS injection: CAPK container-disk images don't
                        # reliably honour DHCP-supplied DNS, so we write the
                        # resolv.conf directly and lock it (chattr +i in
                        # preKubeadmCommands). Cloud-init's own resolv-conf
                        # module is disabled so it doesn't try to merge.
                        {
                            "path": "/etc/resolv.conf",
                            "owner": "root:root",
                            "permissions": "0644",
                            "content": _build_worker_resolv_conf(req),
                        },
                        {
                            "path": "/etc/cloud/cloud.cfg.d/99-disable-resolv-conf.cfg",
                            "owner": "root:root",
                            "permissions": "0644",
                            "content": "manage_resolv_conf: false\n",
                        },
                    ],
                    "preKubeadmCommands": pre_commands,
                    # A worker that fails to join is otherwise a black box: the
                    # serial console carries only systemd's own output, the
                    # image ships no SSH key, and the qemu guest agent is not
                    # connected (`virsh qemu-agent-command` → "Guest agent is
                    # not responding"). Three lab runs had to infer kubelet
                    # failures from the outside — by creating a Node by hand,
                    # by reading `involvedObject.uid` on its events — instead
                    # of reading the error.
                    #
                    # Forwarding journald to the console costs nothing, adds
                    # nothing to the image, and lands in the same
                    # `guest-console-log` container we already read.
                    # Streaming the whole journal to the console does not work:
                    # a kubelet that cannot find its own Node logs that at
                    # ~10/s, the console rate-limiter drops lines, and the few
                    # that matter are the ones lost. Measured on the lab —
                    # "Attempting to register node" never appeared while
                    # "dropped/suppressed" did. So dump a filtered slice
                    # instead, a few times, spaced out.
                    # A worker that fails to join is otherwise a black box:
                    # the console carries only systemd, the image ships no SSH
                    # key, and the qemu guest agent is not connected. This dumps
                    # what is needed to answer "why did the kubelet die" without
                    # logging in. Kept as a script file, not an inline command —
                    # the inline shape cost a whole tenant cycle to quoting.
                    "postKubeadmCommands": [
                        "/usr/local/bin/kubelet-postmortem.sh &",
                    ],
                    "joinConfiguration": {
                        "nodeRegistration": {
                            "kubeletExtraArgs": {
                                "eviction-hard": "imagefs.available<0%,nodefs.available<0%",
                                "image-gc-high-threshold": "100",
                            },
                        },
                    },
                },
            },
        },
    }


def _build_ingress_nginx(req: TenantCreateRequest, host: str, backend_port: int = 6443, dns_target: str = "") -> dict[str, Any]:
    # Ingress must live in the same namespace as the backend Service it points
    # at — Kamaji generates the TCP Service in the tenant ns alongside the
    # KamajiControlPlane CR, so the Ingress lives there too.
    #
    # backend_port = the Kamaji TCP Service port = NetworkProfile.Port.
    # Default-overlay tenants leave it at 6443; VPC tenants set
    # apiServerPort=<allocated api port>, so the Service listens on that
    # port and the ingress MUST target it (pointing at 6443 would 502 →
    # Traefik/nginx serves its default cert).
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": f"{req.name}-api",
            "namespace": _tenant_ns(req.name),
            "labels": {
                "kubevirt-ui.io/tenant": req.name,
            },
            "annotations": {
                "nginx.ingress.kubernetes.io/ssl-passthrough": "true",
                "nginx.ingress.kubernetes.io/backend-protocol": "HTTPS",
                # Tell external-dns which IP to publish for this host.
                **({"external-dns.alpha.kubernetes.io/target": dns_target} if dns_target else {}),
            },
        },
        "spec": {
            "ingressClassName": _ingress_class(),
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": req.name,
                                        "port": {"number": backend_port},
                                    },
                                },
                            }
                        ],
                    },
                }
            ],
        },
    }


def _build_ingress_haproxy(req: TenantCreateRequest, host: str, backend_port: int = 6443, dns_target: str = "") -> dict[str, Any]:
    body = _build_ingress_nginx(req, host, backend_port, dns_target)
    # Same Ingress shape, different annotation keys (keep the external-dns
    # target from the nginx builder).
    body["metadata"]["annotations"] = {
        "haproxy.org/ssl-passthrough": "true",
        "haproxy.org/backend-protocol": "h2",
        **({"external-dns.alpha.kubernetes.io/target": dns_target} if dns_target else {}),
    }
    return body


def _build_ingressroutetcp_traefik(req: TenantCreateRequest, host: str, backend_port: int = 6443, dns_target: str = "") -> dict[str, Any]:
    # Traefik entryPoint name varies by deployment; default to "websecure"
    # (Traefik's standard HTTPS entry point). Override via env if needed.
    # Workers reach the tenant apiserver via the cluster's standard 443
    # Traefik entry point with TLS/SNI passthrough — same listener every
    # other HTTPS ingress uses. The env var stays as an override hatch
    # for clusters whose Traefik install names this entry point
    # differently.
    entry_point = os.getenv("TENANTS_TRAEFIK_ENTRYPOINT", "websecure")
    # Same-ns as the Kamaji-generated TCP Service in the tenant ns.
    return {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRouteTCP",
        "metadata": {
            "name": f"{req.name}-api",
            "namespace": _tenant_ns(req.name),
            "labels": {
                "kubevirt-ui.io/tenant": req.name,
            },
            # external-dns can't infer a target from an IngressRouteTCP
            # (no LB status), so publish the ingress VIP explicitly. The
            # hostname is taken from the HostSNI match below.
            **({"annotations": {"external-dns.alpha.kubernetes.io/target": dns_target}} if dns_target else {}),
        },
        "spec": {
            "entryPoints": [entry_point],
            "routes": [{
                "match": f"HostSNI(`{host}`)",
                "services": [{
                    "name": req.name,
                    # = Kamaji TCP Service port = NetworkProfile.Port.
                    # 6443 for default-overlay tenants; the allocated api
                    # port for VPC tenants (apiServerPort).
                    "port": backend_port,
                }],
            }],
            "tls": {"passthrough": True},
        },
    }


def _build_httpproxy_contour(req: TenantCreateRequest, host: str, backend_port: int = 6443, dns_target: str = "") -> dict[str, Any]:
    # Same-ns as the Kamaji-generated TCP Service in the tenant ns.
    return {
        "apiVersion": "projectcontour.io/v1",
        "kind": "HTTPProxy",
        "metadata": {
            "name": f"{req.name}-api",
            "namespace": _tenant_ns(req.name),
            "labels": {
                "kubevirt-ui.io/tenant": req.name,
            },
            **({"annotations": {"external-dns.alpha.kubernetes.io/target": dns_target}} if dns_target else {}),
        },
        "spec": {
            "virtualhost": {
                "fqdn": host,
                "tls": {"passthrough": True},
            },
            "tcpproxy": {
                "services": [{
                    "name": req.name,
                    # = Kamaji TCP Service port (see note in
                    # `_build_ingressroutetcp_traefik`).
                    "port": backend_port,
                }],
            },
        },
    }


async def _create_tenant_apiserver_ingress(
    k8s, req: TenantCreateRequest, api_port: int | None = None,
) -> None:
    """Expose the tenant kube-apiserver (Kamaji TCP) externally with TLS passthrough.

    Dispatches to the right resource shape per ingress controller. All current
    targets require end-to-end TLS passthrough — the tenant apiserver cert is
    served by Kamaji directly and ingress must not terminate.

    `api_port` is the Kamaji TCP Service port: None (default overlay) → 6443;
    VPC tenants pass their allocated apiServerPort, which the Service actually
    listens on (pointing the ingress at 6443 instead would 502 and the
    controller would serve its own default cert).

    The Ingress/IngressRouteTCP/HTTPProxy resource is created in the tenant
    namespace (alongside the Kamaji-generated TCP Service) because ingress
    backends must be same-ns as the ingress object for all four supported
    controllers (nginx, haproxy, traefik, contour). Cleanup is handled by
    the tenant ns cascade on delete — no dedicated cleanup helper needed.
    """
    await _create_tenant_ingress(
        k8s, req,
        host=_endpoint_host(req.name),
        backend_port=api_port if api_port is not None else 6443,
        name_suffix="api",
    )


async def _create_tenant_trustd_ingress(k8s, req: TenantCreateRequest) -> None:
    """Expose the Talos CSR signer (trustd) the same way as the apiserver.

    Talos dials trustd on a fixed port 50001 at whatever host is in
    `cluster.controlPlane.endpoint`, and puts that host in the TLS SNI. That
    is exactly the shape the tenant apiserver already uses — TLS passthrough
    matched on HostSNI — so it is one more per-tenant route, not a component:
    it is created and deleted with the tenant, the controller picks it up, and
    nothing shared has to be edited or restarted.

    The one platform prerequisite is a listener on 50001 (for Traefik, an
    entryPoint; the name is overridable via TENANTS_TRAEFIK_TRUSTD_ENTRYPOINT).
    A shared listener configured once beats owning a router forever.

    Not needed when each tenant has its own VIP — then 50001 simply goes on
    that tenant's own control-plane Service.
    """
    await _create_tenant_ingress(
        k8s, req,
        host=f"{req.name}.{_tenant_ns(req.name)}.svc",
        backend_port=TALOS_TRUSTD_PORT,
        name_suffix="trustd",
        entry_point=os.getenv("TENANTS_TRAEFIK_TRUSTD_ENTRYPOINT", "trustd"),
    )


async def _create_tenant_ingress(
    k8s,
    req: TenantCreateRequest,
    *,
    host: str,
    backend_port: int,
    name_suffix: str,
    entry_point: str | None = None,
) -> None:
    """Create one TLS-passthrough route to a port on the tenant's TCP Service.

    Shared by the apiserver and trustd routes: same four controllers, same
    passthrough, only the host, port and object name differ.
    """
    ns = _tenant_ns(req.name)
    controller = _ingress_controller()
    dns_target = _external_dns_target()

    def _named(body: dict[str, Any]) -> dict[str, Any]:
        body["metadata"]["name"] = f"{req.name}-{name_suffix}"
        if entry_point and body.get("kind") == "IngressRouteTCP":
            body["spec"]["entryPoints"] = [entry_point]
        return body

    if "ingress-nginx" in controller:
        body = _named(_build_ingress_nginx(req, host, backend_port, dns_target))
        networking_api = client.NetworkingV1Api(k8s._api_client)
        await networking_api.create_namespaced_ingress(namespace=ns, body=body)
        logger.info(f"Created nginx Ingress {ns}/{req.name}-{name_suffix} host={host} → :{backend_port}")
        return

    if "haproxy" in controller:
        body = _named(_build_ingress_haproxy(req, host, backend_port, dns_target))
        networking_api = client.NetworkingV1Api(k8s._api_client)
        await networking_api.create_namespaced_ingress(namespace=ns, body=body)
        logger.info(f"Created haproxy Ingress {ns}/{req.name}-{name_suffix} host={host} → :{backend_port}")
        return

    if "traefik" in controller:
        body = _named(_build_ingressroutetcp_traefik(req, host, backend_port, dns_target))
        await k8s.custom_api.create_namespaced_custom_object(
            group="traefik.io", version="v1alpha1", namespace=ns,
            plural="ingressroutetcps", body=body,
        )
        logger.info(f"Created Traefik IngressRouteTCP {ns}/{req.name}-{name_suffix} host={host} → :{backend_port}")
        return

    if "contour" in controller:
        body = _named(_build_httpproxy_contour(req, host, backend_port, dns_target))
        await k8s.custom_api.create_namespaced_custom_object(
            group="projectcontour.io", version="v1", namespace=ns,
            plural="httpproxies", body=body,
        )
        logger.info(f"Created Contour HTTPProxy {ns}/{req.name}-{name_suffix} host={host}")
        return

    raise HTTPException(
        status_code=501,
        detail=(
            f"Ingress controller {controller!r} not supported for tenant "
            "kube-apiserver exposure. Supported controllers (matched by substring): "
            "ingress-nginx, traefik, contour, haproxy. Tenant kube-apiserver "
            "needs end-to-end TLS passthrough, which requires per-controller logic."
        ),
    )


# ---------------------------------------------------------------------------
# 2026.05.29 — Cross-namespace VPC subnet placement for worker VMs.
#
# Worker VM launcher pods carry the annotation
# `ovn.kubernetes.io/logical_switch=<vpc>-default`, stamped on the pod
# template in `_build_kubevirt_machine_template_cr`. kube-ovn honors that
# annotation only when the target subnet's `spec.namespaces` includes the
# pod's namespace. The tenant ns lives on the cluster default overlay
# (Kamaji TCP needs Postgres + shared Ingress reachability), so we APPEND
# the tenant ns to the VPC default subnet's `spec.namespaces` here.
#
# Multi-tenant per VPC: APPEND (not replace) means several tenants can
# share one VPC. The detach helper removes only the matching entry.
# ---------------------------------------------------------------------------



# ============================================================================
# Control-plane reachability from inside the VPC
# ============================================================================

SWITCH_LB_RULE_PLURAL = "switch-lb-rules"


def _cp_lb_rule_name(tenant_name: str) -> str:
    return f"{tenant_name}-cp"


async def tenant_vpc_name(k8s, tenant_name: str) -> str:
    """Which VPC a tenant's workers sit in, read from where it is recorded.

    The Cluster CR carries folder/environment/tenant labels but not the VPC;
    the binding lives on the machine template as
    `ovn.kubernetes.io/logical_switch: <vpc>-default`, which is also what
    actually places the worker pods.
    """
    ns = _tenant_ns(tenant_name)
    try:
        found = await k8s.custom_api.list_namespaced_custom_object(
            group=KUBEVIRT_INFRA_GROUP, version=KUBEVIRT_INFRA_VERSION,
            namespace=ns, plural="kubevirtmachinetemplates",
        )
    except ApiException:
        return ""
    for item in found.get("items", []):
        try:
            annotations = (
                item["spec"]["template"]["spec"]["virtualMachineTemplate"]
                ["spec"]["template"]["metadata"].get("annotations") or {}
            )
        except (KeyError, TypeError):
            continue
        switch = annotations.get("ovn.kubernetes.io/logical_switch", "")
        if switch.endswith("-default"):
            return switch[: -len("-default")]
    return ""


async def _wire_tenant_to_transit(
    k8s, tenant_name: str, vpc_name: str, vip: str, ports: list[int],
    udp_ports: list[int] | None = None,
) -> None:
    """Build the tenant's path to the shared control-plane VIP.

    Best-effort by design: a tenant whose transit wiring failed is still a
    created tenant, and the reconcile on the next read repairs it. Failing the
    whole creation here would leave a half-built cluster behind for a problem
    that is usually a missing configuration value.
    """
    transit = transit_subnet_name()
    if not transit:
        logger.warning(
            "Tenant %r is in VPC %r but TENANTS_CP_TRANSIT_SUBNET is unset — "
            "its workers will not reach the control-plane VIP until the transit "
            "subnet is configured", tenant_name, vpc_name,
        )
        return

    try:
        await attach_vpc_to_transit(k8s, vpc_name, transit)
        eip = await ensure_tenant_snat(
            k8s, tenant_name, vpc_name, f"{vpc_name}-default", transit,
        )
        if eip:
            await ensure_transit_acls(k8s, transit, eip, vip, ports, udp_ports)
        else:
            logger.info(
                "Tenant %r: transit EIP has no address yet; ACLs deferred to the "
                "next reconcile", tenant_name,
            )
    except TransitSnatSlotTaken as e:
        # Not transient and not self-healing: another rule owns the one SNAT
        # slot this subnet has, on a network the control-plane path cannot use.
        # Leave a mark the tenant list can show, because nothing else will —
        # the cluster comes up looking fine and only the control plane is dead.
        logger.error("Tenant %r: %s", tenant_name, e)
        try:
            await k8s.custom_api.patch_namespaced_custom_object(
                group=CAPI_GROUP, version=CAPI_VERSION,
                namespace=_tenant_ns(tenant_name), plural="clusters",
                name=tenant_name,
                body={"metadata": {"annotations": {
                    "kubevirt-ui.io/transit-conflict": str(e),
                }}},
                _content_type="application/merge-patch+json",
            )
        except ApiException:
            logger.warning("Tenant %r: could not record the transit conflict",
                           tenant_name)
    except (ApiException, LookupError) as e:
        logger.error("Tenant %r: transit wiring incomplete: %s", tenant_name, e)


async def _ensure_cp_reachable_in_vpc(k8s, tenant_name: str, vpc_name: str) -> bool:
    """Publish the tenant's control-plane ClusterIP inside its own VPC.

    Measured on the lab cluster: a worker in a VPC reaches
    `apiServerEndpoint: <ClusterIP>:6443` by leaving the VPC on its **default
    route**, being SNAT'd to the VPC's EIP, and landing on `ext-sub`, where
    the host cluster's OVN load balancer happens to be attached. `ovn-trace`
    ends in `output("localnet.ext-sub")`, and a tcpdump on the lab router sees
    nothing — the DNAT happens on the way out.

    That makes the node's lifeline a property of the VPC's default route, and
    anything that touches that route cuts it: attaching an egress gateway adds
    a second `0.0.0.0/0`, and the tenant then sat with zero registered nodes
    for eight minutes (`tigera-operator` Pending, "no nodes available") until
    the gateway was detached.

    A SwitchLBRule puts the same VIP on the VPC's own load balancer — the
    mechanism VpcDns already uses for `10.96.0.200`. The trace then ends at
    the control-plane pod itself, over the `ovn-cluster` peering:

        eth.src = <acme-net-ovn-cluster>   ← peering port
        output("tstor4-…-2k48t.tenant-tstor4")

    The endpoint string does not change, so nothing has to be re-issued: only
    where the DNAT happens does.

    NOT WIRED UP — kube-ovn refuses this VIP. Measured on the cluster: the
    rule is accepted, kube-ovn creates its `slr-<name>` Service and resolves
    the endpoints correctly, and then the controller stops at

        service.go:568 Service tenant-tstor4/slr-tstor4-cp
                       external IP belongs to subnet: false

    A SwitchLBRule's VIP has to belong to a subnet kube-ovn manages; the
    tenant's control-plane address is a ClusterIP of the *host* cluster
    (10.96.0.0/12), which it does not. Programming it by hand
    (`ovn-nbctl lb-add`) does work and proves the datapath — the trace then
    ends at the control-plane pod over the peering — but that bypasses the
    controller and it would fight us back.

    Making this real needs the control-plane endpoint itself to move to an
    address inside the VPC subnet (a `Vip` CR), which also means adding that
    address to the apiserver certificate SANs. That is a design change, not a
    wiring change, so this stays uncalled until it is decided.

    Returns True when the rule is in place. Returns False (without raising)
    when the Kamaji Service does not exist yet.
    """
    ns = _tenant_ns(tenant_name)
    try:
        svc = await k8s.core_api.read_namespaced_service(name=tenant_name, namespace=ns)
    except ApiException as e:
        if e.status == 404:
            logger.info(
                f"Tenant {tenant_name!r}: control-plane Service not created yet; "
                "VPC publication deferred",
            )
            return False
        raise

    vip = svc.spec.cluster_ip
    if not vip or vip == "None":
        logger.warning(f"Tenant {tenant_name!r}: control-plane Service has no ClusterIP")
        return False

    ports = [
        {
            "name": p.name or f"port-{p.port}",
            "port": p.port,
            "targetPort": p.target_port if isinstance(p.target_port, int) else p.port,
            "protocol": (p.protocol or "TCP").upper(),
        }
        for p in (svc.spec.ports or [])
        if (p.protocol or "TCP").upper() == "TCP"
    ]
    if not ports:
        return False

    manifest = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "SwitchLBRule",
        "metadata": {
            "name": _cp_lb_rule_name(tenant_name),
            "labels": {
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/tenant": tenant_name,
            },
        },
        "spec": {
            "vip": vip,
            "namespace": ns,
            # Kamaji stamps this on the control-plane Deployment's pods.
            "selector": [f"kamaji.clastix.io/name:{tenant_name}"],
            "ports": ports,
            "sessionAffinity": "",
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=SWITCH_LB_RULE_PLURAL, body=manifest,
        )
        logger.info(
            f"Tenant {tenant_name!r}: control plane {vip} published inside VPC {vpc_name!r}",
        )
    except ApiException as e:
        if e.status != 409:
            logger.warning(f"Tenant {tenant_name!r}: SwitchLBRule create failed: {e}")
            return False
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=SWITCH_LB_RULE_PLURAL, name=_cp_lb_rule_name(tenant_name),
            body={"spec": manifest["spec"]},
            _content_type="application/merge-patch+json",
        )
    return True


async def _remove_cp_vpc_publication(k8s, tenant_name: str) -> None:
    """Drop the SwitchLBRule with the tenant. Cluster-scoped, so the namespace
    teardown does not take it — exactly the kind of leftover that has bitten
    this codebase three times already."""
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural=SWITCH_LB_RULE_PLURAL, name=_cp_lb_rule_name(tenant_name),
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Tenant {tenant_name!r}: SwitchLBRule delete failed: {e}")

async def _detach_tenant_ns_from_vpc_subnet(k8s, tenant_name: str) -> None:
    """Remove ``tenant-<name>`` from any subnet's ``spec.namespaces``.

    Kept around for legacy cleanup: tenants created before the VPC-for-
    tenants revert may still have entries in their VPC's default subnet
    ``spec.namespaces``. Called from delete_tenant to leave no stale
    entries behind. New tenants don't attach in the first place, so this
    is a no-op for them. Scans every Subnet for
    one whose `spec.namespaces` contains the tenant ns and removes the
    matching entry. We don't persist ``vpc_name`` on the Cluster CR, so the
    lookup is a full-list scan; in practice a tenant has at most one VPC
    binding so the loop fires once.

    Race safety: JSON-patch test+remove at the matching index, with a small
    retry on 409/422 (index shifted by a concurrent attach). Sister tenants
    sharing the VPC are unaffected because we only remove the entry whose
    value matches the tenant ns.

    Best-effort: every failure is logged but never raised — delete must
    complete even if the patch fails (e.g. kube-ovn CRDs uninstalled).
    """
    tenant_ns = _tenant_ns(tenant_name)

    try:
        subnet_list = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="subnets",
        )
    except ApiException as exc:
        if exc.status == 404:
            return  # kube-ovn CRDs absent — nothing to detach
        logger.warning(
            f"Failed to list subnets while detaching tenant {tenant_name!r} "
            f"from VPC subnet: {exc}"
        )
        return

    for subnet in subnet_list.get("items", []):
        spec_ns = (subnet.get("spec") or {}).get("namespaces") or []
        if tenant_ns not in spec_ns:
            continue
        subnet_name = (subnet.get("metadata") or {}).get("name") or ""
        if not subnet_name:
            continue
        await _detach_subnet_ns_with_retry(
            k8s, subnet_name, tenant_ns, initial_spec_ns=list(spec_ns),
        )


async def _detach_subnet_ns_with_retry(
    k8s, subnet_name: str, tenant_ns: str,
    initial_spec_ns: list[str],
    max_attempts: int = 5,
) -> None:
    """JSON-patch test+remove the tenant ns from ``spec.namespaces``.

    Each iteration: locate ``tenant_ns`` in the array, emit a
    ``test+remove`` JSON-patch at that index. If a concurrent attach
    shifted the index, the apiserver rejects (422) and we re-GET and retry.
    Bounded by ``max_attempts`` to avoid unbounded loops in pathological
    cases. Best-effort: failures are logged, never raised.
    """
    spec_ns = initial_spec_ns
    for attempt in range(max_attempts):
        try:
            idx = spec_ns.index(tenant_ns)
        except ValueError:
            return  # already gone — concurrent detach won the race
        patch_ops = [
            {"op": "test", "path": f"/spec/namespaces/{idx}", "value": tenant_ns},
            {"op": "remove", "path": f"/spec/namespaces/{idx}"},
        ]
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="subnets", name=subnet_name,
                body=patch_ops,
                _content_type="application/json-patch+json",
            )
            logger.info(
                f"Removed tenant ns {tenant_ns!r} from subnet "
                f"{subnet_name!r}.spec.namespaces"
            )
            return
        except ApiException as exc:
            # 422 → test op failed (index shifted); 409 → resourceVersion stale.
            # Both retryable by re-reading.
            if exc.status not in (409, 422):
                logger.warning(
                    f"Failed to detach tenant ns {tenant_ns!r} from subnet "
                    f"{subnet_name!r}: {exc}"
                )
                return
            try:
                subnet = await k8s.custom_api.get_cluster_custom_object(
                    group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                    plural="subnets", name=subnet_name,
                )
                spec_ns = (subnet.get("spec") or {}).get("namespaces") or []
            except ApiException as ge:
                if ge.status == 404:
                    return  # subnet vanished — nothing to detach
                logger.warning(
                    f"Failed to re-read subnet {subnet_name!r} during detach "
                    f"retry: {ge}"
                )
                return
    logger.warning(
        f"Gave up removing {tenant_ns!r} from subnet {subnet_name!r}"
        f".spec.namespaces after {max_attempts} retries — manual cleanup "
        "may be required"
    )


async def _wait_for_service_ip(
    k8s, name: str, namespace: str, attempts: int = 30,
) -> str:
    """The ClusterIP of a Service, once the API has assigned one."""
    import asyncio

    for _ in range(attempts):
        try:
            svc = await k8s.core_api.read_namespaced_service(
                name=name, namespace=namespace,
            )
        except ApiException:
            svc = None
        ip = getattr(getattr(svc, "spec", None), "cluster_ip", None)
        if ip and ip != "None":
            return ip
        await asyncio.sleep(2)

    logger.warning(f"Service {namespace}/{name} never got a ClusterIP")
    return ""


async def _wait_for_tcp_service_ip(
    k8s, name: str, namespace: str, timeout: int = 120,
) -> str:
    """Wait for Kamaji TCP ClusterIP service to appear and return its IP."""
    core_api = k8s.core_api
    for _ in range(timeout // 2):
        try:
            svc = await core_api.read_namespaced_service(name=name, namespace=namespace)
            cluster_ip = svc.spec.cluster_ip
            if cluster_ip and cluster_ip != "None":
                logger.info(f"TCP service {namespace}/{name} ClusterIP: {cluster_ip}")
                return cluster_ip
        except ApiException:
            pass
        await asyncio.sleep(2)
    raise RuntimeError(f"TCP service {namespace}/{name} did not get ClusterIP within {timeout}s")


async def _create_capi_resources(
    k8s, req: TenantCreateRequest,
    storage_info: dict[str, str] | None = None,
) -> None:
    """Create CAPI + Ingress resources.

    All cluster-level CRs (KamajiControlPlane, CAPI Cluster, KubevirtCluster,
    MachineDeployment, KubevirtMachineTemplate, KubeadmConfigTemplate, Ingress)
    live in the tenant namespace. CAPI v1beta1's webhook
    ``validation.cluster.cluster.x-k8s.io`` rejects
    ``Cluster.spec.controlPlaneRef.namespace != metadata.namespace``, so the
    TCP CR must be same-ns as the Cluster CR.

    The tenant ns and its worker VMs live on the cluster default overlay
    — VPC placement for tenant workloads is deferred. This keeps worker
    join simple (Kamaji TCP Service ClusterIP is natively routable) and
    avoids the cluster-info / kubeadm-config mismatch that the previous
    Ingress-based path required.

    Order: KamajiControlPlane + KubevirtCluster first → create Cluster
    with a placeholder endpoint → wait for the Kamaji-created TCP Service
    → PATCH Cluster.spec.controlPlaneEndpoint to the TCP ClusterIP:6443.
    External admin access still gets an Ingress (TLS passthrough) on the
    standard 443 entry point.

    When tenant storage is enabled (storage_info provided):
      - KubevirtCluster gets ``infraClusterSecretRef`` so CAPK can speak
        to the host cluster. The storage class is NOT set on the CR
        (CAPK v1alpha1 has no such field); it is plumbed through the
        kubevirt-csi-driver addon chart values instead.
    """
    custom = k8s.custom_api
    ns = _tenant_ns(req.name)

    # 0. VPC tenant? Settle the control-plane address before anything is built
    #    from it. Default-overlay tenant (vpc_name=None) → all the *_port/vip
    #    stay None and every builder takes its unchanged default-overlay branch.
    vpc = bool(req.vpc_name)
    vip = api_port = konn_port = None
    own_vip = False
    if vpc and per_tenant_vip_enabled():
        # An address each, standard ports. The Service is created FIRST and
        # without an address: MetalLB assigns one and we read the fact back,
        # so there is exactly one owner of the pool. See tenants_cp_vip.
        await assert_pool_excluded_from_ovn(
            k8s, cp_metallb_pool(), transit_subnet_name() or "",
        )
        vip = await acquire_tenant_vip(
            k8s, req.name, ns, worker_os=req.worker_os,
        )
        api_port, konn_port = TENANT_API_PORT, TENANT_KONN_PORT
        own_vip = True
        logger.info(
            f"VPC tenant {req.name!r}: own VIP {vip} "
            f"(api={api_port} konn={konn_port}"
            f"{' trustd=' + str(TENANT_TRUSTD_PORT) if req.worker_os == 'talos' else ''})"
        )
    elif vpc:
        # Legacy shared-VIP model, kept behind TENANTS_CP_PER_TENANT_VIP=false
        # for the migration window. Talos cannot use it: trustd's fixed :50001
        # can belong to only one tenant on a shared address.
        vip = _cp_demux_vip()
        if not vip:
            raise HTTPException(
                status_code=422,
                detail=(
                    "VPC tenant requested but no shared CP demux VIP is "
                    "configured — set TENANTS_CP_DEMUX_VIP (a MetalLB IP) "
                    "or create the tenant without a VPC."
                ),
            )
        if req.worker_os == "talos":
            raise HTTPException(
                status_code=422,
                detail=(
                    "Talos workers in a VPC need their own control-plane "
                    "address: Talos dials trustd on a fixed :50001 of the "
                    "endpoint host, and a shared VIP can give that port to "
                    "only one tenant. Unset TENANTS_CP_PER_TENANT_VIP=false "
                    "to use the per-tenant VIP model."
                ),
            )
        api_port, konn_port = await allocate_cp_ports(k8s, req.name)
        logger.info(
            f"VPC tenant {req.name!r}: CP ports api={api_port} konn={konn_port} "
            f"on shared VIP {vip}"
        )

    # 1. Create infrastructure + control plane providers first — both in the
    #    tenant ns (CAPI webhook forbids cross-ns controlPlaneRef).
    pre_resources = [
        (KAMAJI_CP_GROUP, KAMAJI_CP_VERSION, "kamajicontrolplanes",
         _build_kamaji_cp_cr(req, advertise_vip=vip, konn_port=konn_port)),
        (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtclusters",
         _build_kubevirt_cluster_cr(req, storage_info)),
    ]
    for group, version, plural, body in pre_resources:
        target_ns = body["metadata"]["namespace"]
        await custom.create_namespaced_custom_object(
            group=group, version=version, namespace=target_ns, plural=plural, body=body,
        )

    # 2. Create CAPI Cluster.
    cluster_cr = _build_cluster_cr(req, api_port=api_port, tenant_vip=vip)
    await custom.create_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", body=cluster_cr,
    )

    if vpc:
        # 3-VPC. advertiseAddress model: cluster-info is already correct
        #   (<vip>:<api_port> baked by KCP), so NO endpoint patch.
        if own_vip:
            # The Service already exists — it is what produced `vip` in step 0.
            logger.info(
                f"{req.name}-cp-lb holds {vip}:{api_port}(api)/{konn_port}(konn)"
            )
        else:
            # Legacy: the shared-VIP Service is created here, after the ports
            # were allocated for it.
            cp_lb = _build_cp_lb_service(req, vip, api_port, konn_port)
            await k8s.core_api.create_namespaced_service(namespace=ns, body=cp_lb)
            logger.info(
                f"Created {req.name}-cp-lb on {vip}:{api_port}(api)/{konn_port}(konn)"
            )
        cp_address = vip

        # The Service alone is not reachable from inside the VPC: the tenant's
        # router has no port on the transit plane, no source address there and
        # no policy keeping control-plane traffic off an egress gateway's
        # catch-all reroute. Build that path now — it used to be five manual
        # `kubectl` steps between "tenant created" and "a worker can join".
        #
        # Talos also needs trustd's fixed :50001 through the same guard, or the
        # worker's CSR never reaches a signer and the node never appears.
        transit_ports = [api_port, konn_port]
        transit_udp_ports: list[int] = []
        if own_vip and req.worker_os == "talos":
            transit_ports.append(TENANT_TRUSTD_PORT)
            # T22. The clock is on the join path: Talos holds the kubelet on
            # "waiting for time sync", and a guard that passed only the TCP
            # ports would drop the time request. The failure is a node that
            # never appears, with nothing naming the clock.
            transit_udp_ports.append(NTP_PORT)
        await _wire_tenant_to_transit(
            k8s, req.name, req.vpc_name, vip, transit_ports, transit_udp_ports,
        )
    else:
        # 3-default. Wait for the Kamaji TCP Service ClusterIP, then PATCH
        #   controlPlaneEndpoint to it. Workers join via the ClusterIP
        #   (natively routable on the default overlay); cluster-info matches.
        cluster_ip = await _wait_for_tcp_service_ip(k8s, req.name, ns)
        endpoint_patch = {
            "spec": {"controlPlaneEndpoint": {"host": cluster_ip, "port": 6443}},
        }
        await custom.patch_namespaced_custom_object(
            group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
            plural="clusters", name=req.name, body=endpoint_patch,
            _content_type="application/merge-patch+json",
        )
        logger.info(
            f"Patched Cluster {req.name} controlPlaneEndpoint → {cluster_ip}:6443"
        )
        cp_address = cluster_ip

    # 5. Create VM worker resources (skip for bare_metal)
    if req.worker_type == "vm":
        vm_resources = [
            (CAPI_GROUP, CAPI_VERSION, "machinedeployments", _build_machine_deployment_cr(req)),
            (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtmachinetemplates", _build_kubevirt_machine_template_cr(req)),
            (CAPI_GROUP, CAPI_VERSION, "machinehealthchecks", _build_machine_health_check_cr(req)),
        ]
        if req.worker_os == "talos":
            talos_secrets = await read_talos_secrets(k8s, req.name, ns)
            talos_ca = await read_talos_ca_cert(k8s, req.name, ns)
            # `extraHostEntries` pins the endpoint name inside the node, and
            # it has to name the Service that answers on BOTH ports. Pointing
            # it at Kamaji's Service resolves 6443 and blackholes 50001, so
            # apid waits for a certificate forever while the kubelet looks
            # perfectly healthy.
            talos_svc_ip = await _wait_for_service_ip(
                k8s, talos_service_name(req.name), ns,
            ) or cp_address
            k8s_ca = await await_tenant_k8s_ca_cert(k8s, req.name, ns)
            # The machine config is rendered here rather than generated by
            # CABPT: the endpoint name, the host entry and the token all have
            # to match objects created elsewhere in this flow exactly, and two
            # generators for one set of strings is one too many.
            machine_config = yaml.safe_dump(
                build_talos_worker_config(
                    req.name, ns,
                    api_port=api_port or 6443,
                    control_plane_vip=talos_svc_ip,
                    machine_token=talos_secrets["machine.token"],
                    cluster_id=talos_secrets["cluster.id"],
                    cluster_secret=talos_secrets["cluster.secret"],
                    pod_cidr=req.pod_cidr,
                    service_cidr=req.service_cidr,
                    ca_cert_b64=talos_ca,
                    k8s_ca_cert_b64=k8s_ca,
                    kubernetes_version=req.kubernetes_version,
                    # With an address of its own, the worker goes straight
                    # there: <vip>:6443 for the apiserver and <vip>:50001 for
                    # trustd. The *.svc name is unreachable from a VPC.
                    own_vip=vip if own_vip else None,
                    nameservers=_talos_worker_nameservers(req),
                    # The transit address first: a worker that cannot get the
                    # time cannot start its kubelet, and reaching a public
                    # pool needs the egress this join must not depend on.
                    time_servers=worker_time_servers(vip if own_vip else None),
                ),
                default_flow_style=False, sort_keys=False,
            )
            if k8s_ca:
                vm_resources.append((
                    "bootstrap.cluster.x-k8s.io", "v1alpha3",
                    "talosconfigtemplates",
                    build_talos_config_template(req.name, ns, machine_config),
                ))
            else:
                # Deliberately write nothing. A TalosConfigTemplate is
                # immutable, so a template without `cluster.ca` is a tenant
                # bricked for good — its workers boot, fail to start the
                # kubelet, and no product path can repair them. A tenant with
                # no workers and an honest reason recovers on the next
                # reconcile pass, which builds the template once the CA is
                # there.
                logger.error(
                    f"Tenant {req.name!r}: no Kubernetes CA yet, so no worker "
                    f"bootstrap template was written. The control plane is "
                    f"unaffected; workers appear once the reconciler sees the "
                    f"CA."
                )
        else:
            vm_resources.append((
                "bootstrap.cluster.x-k8s.io", "v1beta1", "kubeadmconfigtemplates",
                _build_kubeadm_config_template_cr(req),
            ))
        for group, version, plural, body in vm_resources:
            await custom.create_namespaced_custom_object(
                group=group, version=version, namespace=ns, plural=plural, body=body,
            )

    # 6. Expose tenant kube-apiserver externally with TLS passthrough.
    #    Resource shape depends on the ingress controller (nginx Ingress vs
    #    Traefik IngressRouteTCP vs Contour HTTPProxy). For VPC tenants the
    #    backend Service port is the allocated api_port, not 6443.
    await _create_tenant_apiserver_ingress(k8s, req, api_port=api_port)

    # 7. Talos workers reach the CSR signer the same way — one more
    #    passthrough route, on 50001 instead of the apiserver port. Only
    #    needed when tenants share a VIP; with a VIP per tenant the port is
    #    already published on that tenant's own control-plane Service.
    if req.worker_os == "talos" and vip is not None:
        await _create_tenant_trustd_ingress(k8s, req)
