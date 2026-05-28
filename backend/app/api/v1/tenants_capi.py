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
    cp_port: int = 6443,
) -> dict[str, Any]:
    host = cp_host or f"{req.name}.{_tenant_ns(req.name)}.svc.cluster.local"
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
                "port": cp_port,
            },
            "clusterNetwork": {
                "pods": {"cidrBlocks": [req.pod_cidr]},
                "services": {"cidrBlocks": [req.service_cidr]},
            },
            "controlPlaneRef": {
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

    T4 simplification: tenant ns is attached to the folder's existing VPC
    (if any) by patching the VPC's `spec.namespaces` — so the TCP pod and
    workers both land on the same VPC subnet without per-pod Multus / fixed
    IPs / advertise-address / DNAT plumbing. The previous dual-NIC dance is
    gone; everything is plain ClusterIP intra-VPC traffic.
    """
    apiserver_extra_args: list[str] = []
    if OIDC_ISSUER and OIDC_ISSUER.startswith("https://"):
        apiserver_extra_args += [
            f"--oidc-issuer-url={OIDC_ISSUER}",
            f"--oidc-client-id={OIDC_CLIENT_ID}",
            "--oidc-username-claim=email",
            "--oidc-groups-claim=groups",
        ]

    ns = _tenant_ns(req.name)

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
            "namespace": ns,
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

    # T6 — default 'bridge' so guests report their real OVN IP. Masquerade
    # hides it behind QEMU SLIRP's 10.0.2.x. worker_network_binding=masquerade
    # is the legacy escape hatch for CNIs where bridge live-migration is iffy.
    nic_binding = "masquerade" if req.worker_network_binding == "masquerade" else "bridge"
    primary_iface: dict[str, Any] = {"name": "default", nic_binding: {}}
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
                                    "networks": [
                                        {
                                            "name": "default",
                                            "pod": {},
                                        }
                                    ],
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
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": f"{req.name}-api",
            "namespace": _tenant_ns(req.name),
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
    return {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRouteTCP",
        "metadata": {
            "name": f"{req.name}-api",
            "namespace": _tenant_ns(req.name),
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
    return {
        "apiVersion": "projectcontour.io/v1",
        "kind": "HTTPProxy",
        "metadata": {
            "name": f"{req.name}-api",
            "namespace": _tenant_ns(req.name),
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
    """Create CAPI + Ingress resources in tenant namespace.

    Order: KamajiControlPlane + KubevirtCluster first → wait for TCP service
    ClusterIP → create Cluster with ClusterIP as controlPlaneEndpoint.

    T4: VPC binding for the tenant ns is handled out-of-band (caller patches
    the folder VPC's `spec.namespaces` before this is called), so TCP + workers
    share the same VPC subnet. No more dual-NIC / fixed-IP / DNAT plumbing.

    When tenant storage is enabled (storage_info provided):
      - KubevirtCluster gets `infraClusterSecretRef` so CAPK can speak
        to the host cluster. The storage class is NOT set on the CR
        (CAPK v1alpha1 has no such field); it is plumbed through the
        kubevirt-csi-driver addon chart values instead.
    """
    custom = k8s.custom_api
    ns = _tenant_ns(req.name)

    # 1. Create infrastructure + control plane providers first
    pre_resources = [
        (KAMAJI_CP_GROUP, KAMAJI_CP_VERSION, "kamajicontrolplanes", _build_kamaji_cp_cr(req)),
        (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtclusters", _build_kubevirt_cluster_cr(req, storage_info)),
    ]
    for group, version, plural, body in pre_resources:
        await custom.create_namespaced_custom_object(
            group=group, version=version, namespace=ns, plural=plural, body=body,
        )

    # 2. Create CAPI Cluster (needed for TCP to start reconciling)
    #    Use service DNS initially — will be patched with ClusterIP once available
    cluster_cr = _build_cluster_cr(req)
    await custom.create_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", body=cluster_cr,
    )

    # 3. Wait for TCP service to get ClusterIP (used by CAPI/external access).
    #    Workers reach apiserver via this ClusterIP — they're in the same VPC
    #    as the TCP pod (tenant ns is bound to the folder VPC) so ClusterIP
    #    just works.
    tcp_ip = await _wait_for_tcp_service_ip(k8s, req.name, ns)

    # 4. Patch Cluster controlPlaneEndpoint with the worker-reachable IP
    patch = {
        "spec": {
            "controlPlaneEndpoint": {
                "host": tcp_ip,
                "port": 6443,
            },
        },
    }
    await custom.patch_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", name=req.name, body=patch,
        _content_type="application/merge-patch+json",
    )
    logger.info(f"Patched Cluster {req.name} controlPlaneEndpoint to {tcp_ip}:6443")

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
