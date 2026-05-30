"""CAPI resource generators and creation for tenants.

Builds Cluster, KamajiControlPlane, KubevirtCluster, MachineDeployment,
KubevirtMachineTemplate, KubeadmConfigTemplate, and Ingress CRs.
"""

import asyncio
import logging
import os
from typing import Any

from fastapi import HTTPException
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
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
)

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
) -> dict[str, Any]:
    # Workers do `kubeadm join <host>:<port>` reading these two fields, so
    # this is THE place that determines worker reachability. Tenant workers
    # live on the cluster default overlay (no VPC), which means the Kamaji
    # TCP Service ClusterIP is natively routable — kube-proxy DNAT'es it
    # to the apiserver pod. cluster-info also gets the ClusterIP from
    # Kamaji (NetworkProfile default), so the post-join kubelet.conf
    # rewrite matches and the worker stays reachable.
    #
    # We bake a DNS placeholder at create time; `_create_capi_resources`
    # PATCHes both `host` and `port` to the actual ClusterIP:6443 after
    # the Kamaji TCP Service is ready. cp_host (used by tests / future
    # external endpoint flows) overrides the placeholder only.
    host = cp_host or _endpoint_host(req.name)
    return {
        "apiVersion": f"{CAPI_GROUP}/{CAPI_VERSION}",
        "kind": "Cluster",
        "metadata": {
            "name": req.name,
            "namespace": _tenant_ns(req.name),
            "labels": {
                "kubevirt-ui.io/tenant": req.name,
            },
            "annotations": {
                "kubevirt-ui.io/display-name": req.display_name,
                "kubevirt-ui.io/worker-type": req.worker_type,
            },
        },
        "spec": {
            "controlPlaneEndpoint": {
                "host": host,
                "port": 6443,
            },
            "clusterNetwork": {
                "pods": {"cidrBlocks": [req.pod_cidr]},
                "services": {"cidrBlocks": [req.service_cidr]},
            },
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


def _build_kamaji_cp_cr(req: TenantCreateRequest) -> dict[str, Any]:
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
    if OIDC_ISSUER and OIDC_ISSUER.startswith("https://"):
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

    spec: dict[str, Any] = {
        "replicas": req.control_plane_replicas,
        "version": req.kubernetes_version,
        "dataStoreName": "default",
        "addons": {
            "coreDNS": {},
            "kubeProxy": {},
            "konnectivity": {
                "server": {
                    "port": 8132,
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
        spec["infraClusterSecretRef"] = {
            "name": storage_info["secret_name"],
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
                    "version": req.kubernetes_version,
                    "bootstrap": {
                        "configRef": {
                            "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                            "kind": "KubeadmConfigTemplate",
                            "name": f"{req.name}-workers",
                        },
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

    # Single NIC on the cluster default pod network (kube-ovn primary
    # plugin). VPC-overlay placement for tenant workers is deferred — see
    # the disabled VPC step in the create-tenant UI. Workers stay on the
    # cluster default overlay so the Kamaji TCP Service ClusterIP is
    # natively reachable for kubeadm join.
    nic_binding = "masquerade" if req.worker_network_binding == "masquerade" else "bridge"
    primary_iface: dict[str, Any] = {"name": "default", nic_binding: {}}
    networks: list[dict[str, Any]] = [{"name": "default", "pod": {}}]
    # bridge binding requires the kubevirt.io/allow-pod-bridge-network-live-migration
    # pod annotation to permit live migration on a pod-network bridge interface.
    if nic_binding == "bridge":
        pod_annotations["kubevirt.io/allow-pod-bridge-network-live-migration"] = "true"

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
                        "checkStrategy": "ssh",
                    },
                    "virtualMachineTemplate": {
                        "spec": {
                            "runStrategy": "Always",
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
                                        {
                                            "name": "root",
                                            "containerDisk": {
                                                "image": _resolve_worker_container_disk_image(req),
                                            },
                                        },
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
    pre_commands = [
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
                        # VpcDns reaches the worker guest two layers down:
                        # kube-ovn DHCP delivers the VpcDns VIP as `dns_server`
                        # via subnet.spec.dhcpV4Options, and Kyverno's per-VPC
                        # ClusterPolicy fixes pod-side DNS for virt-launcher /
                        # CDI / in-cluster pods. No cloud-init munging here.
                    ],
                    "preKubeadmCommands": pre_commands,
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


def _build_ingress_nginx(req: TenantCreateRequest, host: str) -> dict[str, Any]:
    # Ingress must live in the same namespace as the backend Service it points
    # at — Kamaji generates the TCP Service in the tenant ns alongside the
    # KamajiControlPlane CR, so the Ingress lives there too.
    #
    # Backend port = 6443: Kamaji's TCP Service exposes the apiserver on
    # the default NetworkProfile.Port. We deliberately don't override that
    # port (modern apiserver images can't bind <1024 as uid 65532), so the
    # Service stays on 6443 regardless of which external port `_endpoint_port`
    # advertises in `Cluster.spec.controlPlaneEndpoint`. Admin's Ingress
    # entry point should also be on 6443 for a clean passthrough.
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
                                        "port": {"number": 6443},
                                    },
                                },
                            }
                        ],
                    },
                }
            ],
        },
    }


def _build_ingress_haproxy(req: TenantCreateRequest, host: str) -> dict[str, Any]:
    body = _build_ingress_nginx(req, host)
    # Same Ingress shape, different annotation key.
    body["metadata"]["annotations"] = {
        "haproxy.org/ssl-passthrough": "true",
        "haproxy.org/backend-protocol": "h2",
    }
    return body


def _build_ingressroutetcp_traefik(req: TenantCreateRequest, host: str) -> dict[str, Any]:
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
        },
        "spec": {
            "entryPoints": [entry_point],
            "routes": [{
                "match": f"HostSNI(`{host}`)",
                "services": [{
                    "name": req.name,
                    # Backend port 6443 = Kamaji TCP Service port (default
                    # NetworkProfile.Port; we don't override it because doing
                    # so would also change kube-apiserver --secure-port and
                    # break privileged-port binding on 1.30+).
                    "port": 6443,
                }],
            }],
            "tls": {"passthrough": True},
        },
    }


def _build_httpproxy_contour(req: TenantCreateRequest, host: str) -> dict[str, Any]:
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
        },
        "spec": {
            "virtualhost": {
                "fqdn": host,
                "tls": {"passthrough": True},
            },
            "tcpproxy": {
                "services": [{
                    "name": req.name,
                    # Backend port 6443 = Kamaji TCP Service port (see note
                    # in `_build_ingressroutetcp_traefik`).
                    "port": 6443,
                }],
            },
        },
    }


async def _create_tenant_apiserver_ingress(k8s, req: TenantCreateRequest) -> None:
    """Expose the tenant kube-apiserver (Kamaji TCP) externally with TLS passthrough.

    Dispatches to the right resource shape per ingress controller. All current
    targets require end-to-end TLS passthrough — the tenant apiserver cert is
    served by Kamaji directly and ingress must not terminate.

    The Ingress/IngressRouteTCP/HTTPProxy resource is created in the tenant
    namespace (alongside the Kamaji-generated TCP Service) because ingress
    backends must be same-ns as the ingress object for all four supported
    controllers (nginx, haproxy, traefik, contour). Cleanup is handled by
    the tenant ns cascade on delete — no dedicated cleanup helper needed.
    """
    host = _endpoint_host(req.name)
    ns = _tenant_ns(req.name)
    controller = _ingress_controller()

    if "ingress-nginx" in controller:
        body = _build_ingress_nginx(req, host)
        networking_api = client.NetworkingV1Api(k8s._api_client)
        await networking_api.create_namespaced_ingress(namespace=ns, body=body)
        logger.info(f"Created nginx Ingress {ns}/{req.name}-api host={host}")
        return

    if "haproxy" in controller:
        body = _build_ingress_haproxy(req, host)
        networking_api = client.NetworkingV1Api(k8s._api_client)
        await networking_api.create_namespaced_ingress(namespace=ns, body=body)
        logger.info(f"Created haproxy Ingress {ns}/{req.name}-api host={host}")
        return

    if "traefik" in controller:
        body = _build_ingressroutetcp_traefik(req, host)
        await k8s.custom_api.create_namespaced_custom_object(
            group="traefik.io", version="v1alpha1", namespace=ns,
            plural="ingressroutetcps", body=body,
        )
        logger.info(f"Created Traefik IngressRouteTCP {ns}/{req.name}-api host={host}")
        return

    if "contour" in controller:
        body = _build_httpproxy_contour(req, host)
        await k8s.custom_api.create_namespaced_custom_object(
            group="projectcontour.io", version="v1", namespace=ns,
            plural="httpproxies", body=body,
        )
        logger.info(f"Created Contour HTTPProxy {ns}/{req.name}-api host={host}")
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

    # 0. When the tenant is bound to a VPC, append the tenant ns to the VPC
    # 1. Create infrastructure + control plane providers first — both in the
    #    tenant ns (CAPI webhook forbids cross-ns controlPlaneRef).
    pre_resources = [
        (KAMAJI_CP_GROUP, KAMAJI_CP_VERSION, "kamajicontrolplanes", _build_kamaji_cp_cr(req)),
        (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtclusters", _build_kubevirt_cluster_cr(req, storage_info)),
    ]
    for group, version, plural, body in pre_resources:
        target_ns = body["metadata"]["namespace"]
        await custom.create_namespaced_custom_object(
            group=group, version=version, namespace=target_ns, plural=plural, body=body,
        )

    # 2. Create CAPI Cluster with a DNS placeholder endpoint; will be
    #    PATCHed to the TCP ClusterIP after the Kamaji TCP Service is ready.
    cluster_cr = _build_cluster_cr(req)
    await custom.create_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", body=cluster_cr,
    )

    # 3. Wait for the Kamaji TCP Service to get a ClusterIP, then PATCH
    #    Cluster.spec.controlPlaneEndpoint to that ClusterIP:6443. Workers
    #    join via the ClusterIP (kube-proxy DNATs it to the apiserver pod
    #    on the same default overlay) and the post-join cluster-info
    #    rewrite matches — no manual ConfigMap patching needed.
    cluster_ip = await _wait_for_tcp_service_ip(k8s, req.name, ns)
    endpoint_patch = {
        "spec": {
            "controlPlaneEndpoint": {
                "host": cluster_ip,
                "port": 6443,
            },
        },
    }
    await custom.patch_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", name=req.name, body=endpoint_patch,
        _content_type="application/merge-patch+json",
    )
    logger.info(
        f"Patched Cluster {req.name} controlPlaneEndpoint → {cluster_ip}:6443"
    )

    # 5. Create VM worker resources (skip for bare_metal)
    if req.worker_type == "vm":
        kubeadm_cr = _build_kubeadm_config_template_cr(req)
        vm_resources = [
            (CAPI_GROUP, CAPI_VERSION, "machinedeployments", _build_machine_deployment_cr(req)),
            (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtmachinetemplates", _build_kubevirt_machine_template_cr(req)),
            ("bootstrap.cluster.x-k8s.io", "v1beta1", "kubeadmconfigtemplates", kubeadm_cr),
        ]
        for group, version, plural, body in vm_resources:
            await custom.create_namespaced_custom_object(
                group=group, version=version, namespace=ns, plural=plural, body=body,
            )

    # 6. Expose tenant kube-apiserver externally with TLS passthrough.
    #    Resource shape depends on the ingress controller (nginx Ingress vs
    #    Traefik IngressRouteTCP vs Contour HTTPProxy).
    await _create_tenant_apiserver_ingress(k8s, req)
