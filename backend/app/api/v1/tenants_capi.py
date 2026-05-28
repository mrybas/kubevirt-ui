"""CAPI resource generators and creation for tenants.

Builds Cluster, KamajiControlPlane, KubevirtCluster, MachineDeployment,
KubevirtMachineTemplate, KubeadmConfigTemplate, and Ingress CRs.
"""

import asyncio
import json
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
    _endpoint_port,
    _ingress_class,
    _ingress_controller,
    _vpcdns_vip,
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
    cp_port: int | None = None,
) -> dict[str, Any]:
    # Workers join the tenant kube-apiserver via the cluster Ingress (TLS
    # passthrough at the configured Ingress HTTPS port). The ClusterIP of the
    # Kamaji-managed TCP Service is NOT reachable from the tenant VPC, so we
    # bake the external Ingress endpoint into controlPlaneEndpoint from the
    # start; that matches Kamaji's certSANs and avoids the previous patch.
    host = cp_host or _endpoint_host(req.name)
    port = cp_port if cp_port is not None else _endpoint_port()
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
                "port": port,
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

    Worker VMs join the tenant VPC via a per-VM Multus NAD (see T2 — NAD
    targets the tenant VPC's default subnet via kube-ovn provider naming),
    while their launcher pods remain in the tenant ns / default overlay.

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

    # 2026.05.28 — SINGLE-INTERFACE design when the tenant is bound to a VPC.
    # The VM's only NIC attaches via Multus to the per-tenant NAD created in
    # `_setup_tenant_vpc_multus`. No pod-default network, no masquerade.
    # Outbound traffic from the worker (TCP apiserver via Ingress, external
    # internet) flows via the VPC's NAT gateway (admin must provision via
    # OvnEip + OvnSnatRule before workers can join).
    #
    # When `req.vpc_name` is None we keep the pre-existing pod+masquerade/
    # bridge shape so the legacy "default overlay" tenants still work.
    if req.vpc_name:
        nad_name = _tenant_vpc_nad_name(req.vpc_name)
        primary_iface: dict[str, Any] = {"name": "tenant-vpc", "bridge": {}}
        networks: list[dict[str, Any]] = [
            {
                "name": "tenant-vpc",
                "multus": {"networkName": f"{ns}/{nad_name}"},
            }
        ]
        # Same live-migration allowance as the pod-bridge case — the bridge
        # binding is the same shape under the hood.
        pod_annotations["kubevirt.io/allow-pod-bridge-network-live-migration"] = "true"
    else:
        # T6 — default 'bridge' so guests report their real OVN IP. Masquerade
        # hides it behind QEMU SLIRP's 10.0.2.x. worker_network_binding=masquerade
        # is the legacy escape hatch for CNIs where bridge live-migration is iffy.
        nic_binding = "masquerade" if req.worker_network_binding == "masquerade" else "bridge"
        primary_iface = {"name": "default", nic_binding: {}}
        networks = [{"name": "default", "pod": {}}]
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
        # DNS fix: set primary DNS to 8.8.8.8 (reachable via OVN SNAT), VpcDns VIP as fallback
        "sed -i 's/^#\\?DNS=.*/DNS=8.8.8.8/' /etc/systemd/resolved.conf",
        f"sed -i 's/^#\\?FallbackDNS=.*/FallbackDNS={_vpcdns_vip()}/' /etc/systemd/resolved.conf",
        "systemctl restart systemd-resolved",
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
# T2 — Per-tenant Multus NAD + tenant VPC default-subnet provider patch.
#
# Rationale (2026.05.28 redesign): the tenant ns lives in the cluster default
# overlay (CAPI webhook forbids cross-ns controlPlaneRef → TCP must live in
# the tenant ns → ns must reach Postgres/Ingress). To still give worker VMs
# tenant VPC isolation, each worker VM attaches a SINGLE NIC via Multus to a
# per-tenant NetworkAttachmentDefinition wired to the tenant VPC's default
# subnet.
#
# kube-ovn NAD binding mechanism: the NAD's `provider` field
# (`<nad>.<ns>.ovn` per `network.py:get_nad_provider`) must match a Subnet's
# `spec.provider`. We patch the tenant VPC's default subnet's provider to
# that value.
#
# CAVEAT (multi-tenant per VPC): patching the shared default subnet's
# `spec.provider` ties that subnet to ONE NAD. If two tenants share the same
# VPC, the second tenant's create will overwrite the provider and break the
# first tenant's NIC binding. The current label-based VPC selection design
# already encourages one VPC per tenant per (folder, env), but this is a
# soft assumption — admin shouldn't reuse a VPC across tenants until we
# switch to per-tenant subnet creation.
# ---------------------------------------------------------------------------

def _tenant_vpc_nad_name(vpc_name: str) -> str:
    """Deterministic NAD name for a tenant attached to ``vpc_name``."""
    return f"{vpc_name}-attach"


def _tenant_vpc_nad_provider(vpc_name: str, tenant_ns: str) -> str:
    """Kube-OVN provider string for the tenant VPC NAD.

    Matches the convention in ``network.py:get_nad_provider`` —
    ``<nad>.<ns>.ovn`` — so a Subnet whose ``spec.provider`` is set to this
    same value will bind to the NAD.
    """
    return f"{_tenant_vpc_nad_name(vpc_name)}.{tenant_ns}.ovn"


def _build_tenant_vpc_nad(req: TenantCreateRequest) -> dict[str, Any]:
    """Build the per-tenant NetworkAttachmentDefinition (Multus → kube-ovn).

    Lives in the tenant namespace; references the tenant VPC's default
    subnet by the matching ``provider`` string (which we also patch onto
    the subnet via ``_patch_subnet_provider``).
    """
    if not req.vpc_name:
        raise ValueError("_build_tenant_vpc_nad called without req.vpc_name")
    tenant_ns = _tenant_ns(req.name)
    nad_name = _tenant_vpc_nad_name(req.vpc_name)
    provider = _tenant_vpc_nad_provider(req.vpc_name, tenant_ns)
    config = {
        "cniVersion": "0.3.1",
        "type": "kube-ovn",
        "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
        "provider": provider,
    }
    return {
        "apiVersion": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {
            "name": nad_name,
            "namespace": tenant_ns,
            "labels": {
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/tenant": req.name,
            },
        },
        "spec": {"config": json.dumps(config)},
    }


async def _resolve_vpc_default_subnet(k8s, vpc_name: str) -> str:
    """Return the name of the VPC's default subnet.

    Raises HTTPException(400) when zero or multiple default subnets exist
    — admin must reconcile the VPC layout before tenant creation can
    succeed (no silent fallback to ovn-default would land workers on the
    wrong network).
    """
    try:
        subnet_list = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="subnets",
        )
    except ApiException as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to list subnets while resolving default for VPC "
                f"{vpc_name!r}: {exc.reason or exc}"
            ),
        ) from exc

    default_subnets = [
        (s.get("metadata") or {}).get("name") or ""
        for s in subnet_list.get("items", [])
        if (s.get("spec") or {}).get("vpc") == vpc_name
        and (s.get("spec") or {}).get("default") is True
    ]
    default_subnets = [n for n in default_subnets if n]

    if len(default_subnets) == 1:
        return default_subnets[0]
    if len(default_subnets) == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"VPC '{vpc_name}' has no default subnet (spec.default=true). "
                "Worker VMs need a default subnet to attach to via Multus. "
                "Admin should mark one subnet of the VPC as default and retry."
            ),
        )
    raise HTTPException(
        status_code=400,
        detail=(
            f"VPC '{vpc_name}' has multiple default subnets "
            f"({sorted(default_subnets)!r}); kube-ovn only allows one. "
            "Admin should reconcile this before tenant creation."
        ),
    )


# Subnet provider values kube-ovn treats as "unbound / default" — safe to
# overwrite with a tenant's NAD provider. Anything else means the subnet is
# already serving traffic for some other consumer (egress gateway, another
# tenant, admin custom wiring) and we must NOT clobber it.
_DEFAULT_SUBNET_PROVIDERS = frozenset({None, "", "ovn"})


async def _patch_subnet_provider(
    k8s, subnet_name: str, provider: str, vpc_name: str,
) -> None:
    """Set the Subnet's ``spec.provider`` to ``provider`` (idempotent + guarded).

    M2 (T5) — enforce "one tenant per VPC":
      - ``current == provider`` → no-op (idempotent re-apply).
      - ``current`` in ``_DEFAULT_SUBNET_PROVIDERS`` (unbound / default) →
        patch.
      - otherwise → raise ``HTTPException(409)``. The subnet is already
        bound to some other tenant (or admin-custom provider); silently
        clobbering would corrupt the other tenant's NIC binding and is
        the failure mode this guard exists to prevent.

    Uses merge-patch — concurrent unrelated patches to other spec fields
    won't be affected.
    """
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="subnets", name=subnet_name,
        )
    except ApiException as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not read subnet {subnet_name!r} for provider patch: "
                f"{exc.reason or exc}"
            ),
        ) from exc

    current = (subnet.get("spec") or {}).get("provider")
    if current == provider:
        logger.info(
            f"Subnet {subnet_name!r} provider already {provider!r}; skipping patch"
        )
        return

    if current not in _DEFAULT_SUBNET_PROVIDERS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"VPC '{vpc_name}' default subnet {subnet_name!r} is already "
                f"bound to another consumer (provider={current!r}). Only one "
                "tenant can attach per VPC; pick a different VPC or have admin "
                "reset the subnet's spec.provider."
            ),
        )

    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="subnets", name=subnet_name,
            body={"spec": {"provider": provider}},
            _content_type="application/merge-patch+json",
        )
        logger.info(
            f"Patched subnet {subnet_name!r} provider {current!r} → {provider!r}"
        )
    except ApiException as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to patch subnet {subnet_name!r} provider to "
                f"{provider!r}: {exc.reason or exc}"
            ),
        ) from exc


async def _setup_tenant_vpc_multus(k8s, req: TenantCreateRequest) -> None:
    """Create the per-tenant NAD and patch the VPC default subnet's provider.

    Must run BEFORE CAPK spawns KubevirtMachines that reference the NAD
    (otherwise virt-launcher pods fail to schedule on "NAD not found"). The
    NAD lives in the tenant namespace; cleanup is handled by the tenant ns
    cascade on delete.

    No-op when ``req.vpc_name`` is None — the tenant runs on the cluster
    default overlay with the legacy pod+masquerade interface shape.
    """
    if not req.vpc_name:
        return

    tenant_ns = _tenant_ns(req.name)
    default_subnet = await _resolve_vpc_default_subnet(k8s, req.vpc_name)
    provider = _tenant_vpc_nad_provider(req.vpc_name, tenant_ns)

    # Patch first so the subnet's provider matches the NAD by the time CAPK
    # starts placing VMs. Order is symmetric (NAD-then-patch also works),
    # but patching first lets us bail out cleanly on subnet errors before
    # creating any objects in the tenant ns.
    await _patch_subnet_provider(k8s, default_subnet, provider, req.vpc_name)

    nad_body = _build_tenant_vpc_nad(req)
    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group="k8s.cni.cncf.io", version="v1",
            namespace=tenant_ns,
            plural="network-attachment-definitions",
            body=nad_body,
        )
        logger.info(
            f"Created tenant VPC NAD {tenant_ns}/{nad_body['metadata']['name']} "
            f"(subnet={default_subnet!r}, provider={provider!r})"
        )
    except ApiException as exc:
        if exc.status == 409:
            logger.info(
                f"Tenant VPC NAD {tenant_ns}/{nad_body['metadata']['name']} "
                "already exists; reusing"
            )
            return
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to create tenant VPC NAD: {exc.reason or exc}"
            ),
        ) from exc


async def _restore_tenant_vpc_subnet_provider(k8s, tenant_name: str) -> None:
    """Reset the tenant-bound VPC subnet's ``spec.provider`` back to ``"ovn"``.

    M1 (T5) — on tenant delete, before the ns cascade drops the NAD, find
    any Subnet whose ``spec.provider`` matches THIS tenant's expected NAD
    provider (``<vpc>-attach.<tenant-ns>.ovn``) and patch the provider back
    to kube-ovn's default (``"ovn"``). Without this, the VPC's default
    subnet stays "tenant-bound" after the tenant is gone — generic pods can
    no longer bind to it, and a same-VPC re-create would hit M2's 409
    guard with a stale provider value.

    We don't persist ``vpc_name`` on the Cluster CR, so the lookup is by
    provider-suffix match across all subnets. Any subnet whose provider
    ends with ``.<tenant-ns>.ovn`` and looks like ``*-attach.*`` belongs
    to this tenant. (If admin manually pointed a non-tenant subnet at a
    matching provider string, the restore would also reset it — that's a
    pathological case we accept; the provider naming convention is
    backend-owned.)

    Best-effort: every failure is logged but never raised — delete must
    complete even if the patch fails (e.g. kube-ovn CRDs uninstalled).
    """
    tenant_ns = _tenant_ns(tenant_name)
    expected_suffix = f".{tenant_ns}.ovn"

    try:
        subnet_list = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
            plural="subnets",
        )
    except ApiException as exc:
        if exc.status == 404:
            return  # kube-ovn CRDs absent — nothing to restore
        logger.warning(
            f"Failed to list subnets while restoring tenant {tenant_name!r} "
            f"VPC provider: {exc}"
        )
        return

    matched: list[tuple[str, str]] = []
    for subnet in subnet_list.get("items", []):
        provider = (subnet.get("spec") or {}).get("provider") or ""
        if not provider.endswith(expected_suffix):
            continue
        # Defensive: the convention is "<vpc>-attach.<tenant-ns>.ovn".
        # Don't restore providers that don't look like our NAD shape, to
        # avoid clobbering admin-custom wiring that happens to share a
        # tenant-ns suffix.
        nad_part = provider[: -len(expected_suffix)]
        if not nad_part.endswith("-attach"):
            continue
        subnet_name = (subnet.get("metadata") or {}).get("name") or ""
        if subnet_name:
            matched.append((subnet_name, provider))

    if not matched:
        logger.info(
            f"No VPC subnet bound to tenant {tenant_name!r} provider; "
            "nothing to restore"
        )
        return

    for subnet_name, current_provider in matched:
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="subnets", name=subnet_name,
                body={"spec": {"provider": "ovn"}},
                _content_type="application/merge-patch+json",
            )
            logger.info(
                f"Restored subnet {subnet_name!r} provider "
                f"{current_provider!r} → 'ovn' (tenant {tenant_name!r} delete)"
            )
        except ApiException as exc:
            logger.warning(
                f"Failed to restore subnet {subnet_name!r} provider after "
                f"tenant {tenant_name!r} delete: {exc}"
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

    The tenant ns itself stays on the cluster default overlay (no VPC attach),
    which gives the Kamaji TCP pod direct reachability to Postgres + shared
    cluster Ingress. Worker VMs join the tenant VPC via per-VM Multus NAD
    (see T2 / ``_build_kubevirt_machine_template_cr``).

    Order: KamajiControlPlane + KubevirtCluster first → wait for the
    Kamaji-created TCP Service → create Cluster with the TCP ClusterIP as
    controlPlaneEndpoint.

    When tenant storage is enabled (storage_info provided):
      - KubevirtCluster gets ``infraClusterSecretRef`` so CAPK can speak
        to the host cluster. The storage class is NOT set on the CR
        (CAPK v1alpha1 has no such field); it is plumbed through the
        kubevirt-csi-driver addon chart values instead.
    """
    custom = k8s.custom_api
    ns = _tenant_ns(req.name)

    # 0. T2 — when the tenant is bound to a VPC, set up the per-tenant Multus
    #    NAD + patch the VPC default subnet's provider BEFORE CAPK reconciles
    #    KubevirtMachineTemplate (the VM spec references the NAD by name; if
    #    it doesn't exist yet, virt-launcher pods fail to schedule).
    await _setup_tenant_vpc_multus(k8s, req)

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

    # 2. Create CAPI Cluster with the external Ingress endpoint baked in.
    #    Workers join via cluster Ingress (TLS passthrough), so
    #    controlPlaneEndpoint already points at the nip.io hostname:443 —
    #    no later patch with the unreachable TCP ClusterIP needed.
    cluster_cr = _build_cluster_cr(req)
    await custom.create_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", body=cluster_cr,
    )

    # 3. Wait for TCP Service to be created by Kamaji (smoke check that the
    #    KamajiControlPlane controller reconciled our CR). Return value is
    #    unused — the Cluster CR already carries the correct external
    #    endpoint and certSANs cover the same hostname.
    await _wait_for_tcp_service_ip(k8s, req.name, ns)

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
