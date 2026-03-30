"""CAPI resource generators and creation for tenants.

Builds Cluster, KamajiControlPlane, KubevirtCluster, MachineDeployment,
KubevirtMachineTemplate, KubeadmConfigTemplate, and Ingress CRs.
"""

import asyncio
import json
import logging
from typing import Any

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
    VPCDNS_VIP,
    OIDC_ISSUER,
    OIDC_CLIENT_ID,
    _tenant_ns,
    _endpoint_host,
)

logger = logging.getLogger(__name__)

INGRESS_CLASS = "nginx"
CONTAINER_DISK_IMAGE = "git.nas.ssh.org.ua/dev/ubuntu-container-disk:v1.30.2"


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


def _build_kamaji_cp_cr(
    req: TenantCreateRequest,
    vpc_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build KamajiControlPlane CR.

    Args:
        req: Tenant creation request.
        vpc_info: If network_isolation is enabled, dict with nad_name etc.
                  Used to add Multus annotation for dual-NIC (eth0=mgmt, net1=VPC).
    """
    apiserver_extra_args: list[str] = []
    if OIDC_ISSUER and OIDC_ISSUER.startswith("https://"):
        apiserver_extra_args += [
            f"--oidc-issuer-url={OIDC_ISSUER}",
            f"--oidc-client-id={OIDC_CLIENT_ID}",
            "--oidc-username-claim=email",
            "--oidc-groups-claim=groups",
        ]
    if vpc_info:
        # Advertise VPC IP so kubelet.conf, cluster-info, etc. all use it.
        # Without this, Kamaji advertises the ClusterIP which is unreachable
        # from workers in the isolated VPC subnet.
        apiserver_extra_args.append(f"--advertise-address={vpc_info['fixed_ip']}")

    ns = _tenant_ns(req.name)

    # Pod metadata: labels always, annotations when VPC isolation is enabled
    pod_labels = {
        "cluster.x-k8s.io/cluster-name": req.name,
        "cluster.x-k8s.io/role": "control-plane",
    }
    pod_annotations: dict[str, str] = {}
    if vpc_info:
        # Multus dual-NIC: attach TCP pod to tenant VPC subnet via NAD
        nad_name = vpc_info["nad_name"]
        provider = vpc_info["provider"]
        fixed_ip = vpc_info["fixed_ip"]
        pod_annotations["k8s.v1.cni.cncf.io/networks"] = json.dumps([
            {"name": nad_name, "namespace": ns}
        ])
        # Pin net1 IP via Kube-OVN annotation — workers connect to this IP
        pod_annotations[f"{provider}.kubernetes.io/ip_address"] = fixed_ip

    pod_additional_metadata: dict[str, Any] = {"labels": pod_labels}
    if pod_annotations:
        pod_additional_metadata["annotations"] = pod_annotations

    # VPC mode: single TCP replica (fixed VPC IP can't be shared across replicas)
    cp_replicas = 1 if vpc_info else req.control_plane_replicas

    spec: dict[str, Any] = {
        "replicas": cp_replicas,
        "version": req.kubernetes_version,
        "dataStoreName": "default",
        "addons": {
            "coreDNS": {},
            "kubeProxy": {},
            "konnectivity": {
                "server": {
                    "port": 8132,
                    "image": "registry.k8s.io/kas-network-proxy/proxy-server",
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                    },
                },
                "agent": {
                    "image": "registry.k8s.io/kas-network-proxy/proxy-agent",
                },
            },
        },
        "kubelet": {
            "cgroupfs": "systemd",
            "preferredAddressTypes": ["InternalIP", "ExternalIP"],
        },
        "network": {
            "serviceType": "ClusterIP",
            "certSANs": [_endpoint_host(req.name)] + ([vpc_info["fixed_ip"]] if vpc_info else []),
        },
        "deployment": {
            "podAdditionalMetadata": pod_additional_metadata,
            # Recreate strategy required when VPC IP is pinned — RollingUpdate
            # causes deadlock (new pod can't get pinned IP while old pod has it)
            **({"strategy": {"type": "Recreate"}} if vpc_info else {}),
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


def _build_kubevirt_cluster_cr(req: TenantCreateRequest) -> dict[str, Any]:
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
        "spec": {},
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

    # When network isolation is enabled, annotate the VM pod to land in the
    # tenant's VPC subnet instead of the default ovn-default subnet.
    pod_annotations: dict[str, str] = {}
    if req.network_isolation:
        vpc_name = f"vpc-{req.name}"
        subnet_name = f"{vpc_name}-default"
        pod_annotations["ovn.kubernetes.io/logical_switch"] = subnet_name

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
                                    **({"annotations": pod_annotations} if pod_annotations else {}),
                                },
                                "spec": {
                                    # VPC mode: override DNS since kube-dns ClusterIP
                                    # is unreachable from the VPC subnet
                                    **({"dnsPolicy": "None", "dnsConfig": {"nameservers": [VPCDNS_VIP]}} if req.network_isolation else {}),
                                    "domain": {
                                        "cpu": {"cores": req.worker_vcpu},
                                        "memory": {"guest": req.worker_memory},
                                        "devices": {
                                            "networkInterfaceMultiqueue": True,
                                            "interfaces": [
                                                {
                                                    "name": "default",
                                                    "masquerade": {},
                                                }
                                            ],
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
                                                "image": CONTAINER_DISK_IMAGE,
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
    dnat_cluster_ip: str = "",
    dnat_vpc_ip: str = "",
) -> dict[str, Any]:
    """Build KubeadmConfigTemplate CR.

    Container disk has all packages pre-baked (containerd, kubelet, kubeadm, kubectl,
    CNI plugins). Only need: DNS fix + kubeadm join config.

    When VPC isolation is enabled, adds iptables DNAT rule so workers can reach
    the apiserver. Kamaji advertises the ClusterIP (unreachable from VPC), so
    we redirect it to the TCP pod's fixed net1 (VPC) IP before kubeadm runs.
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
        f"sed -i 's/^#\\?FallbackDNS=.*/FallbackDNS={VPCDNS_VIP}/' /etc/systemd/resolved.conf",
        "systemctl restart systemd-resolved",
    ]
    if dnat_cluster_ip and dnat_vpc_ip:

        # DNAT: redirect apiserver ClusterIP → TCP pod's VPC IP
        # Kamaji sets kube-apiserver --advertise-address to ClusterIP, so kubeadm
        # on the worker will try to reach it after initial bootstrap token discovery.
        pre_commands.append(
            f"iptables -t nat -A OUTPUT -d {dnat_cluster_ip}/32 -p tcp --dport 6443"
            f" -j DNAT --to-destination {dnat_vpc_ip}:6443"
        )

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
                                + (
                                    f"# Ensure kubelet kubeconfig points to VPC IP (not ClusterIP)\n"
                                    f"if [ -f /etc/kubernetes/kubelet.conf ]; then\n"
                                    f"  sed -i 's|https://{dnat_cluster_ip}:6443|https://{dnat_vpc_ip}:6443|g' /etc/kubernetes/kubelet.conf\n"
                                    f"  # Pre-create Node object to prevent CSI plugin FATAL race condition.\n"
                                    f"  # In K8s 1.30, CSI initialization has a ~1.5s timeout to find the Node.\n"
                                    f"  # If kubelet hasn't registered the node yet, CSI kills the process.\n"
                                    f"  NODENAME=$(hostname)\n"
                                    f"  if ! kubectl --kubeconfig=/etc/kubernetes/kubelet.conf get node $NODENAME > /dev/null 2>&1; then\n"
                                    f"    cat <<NODEEOF | kubectl --kubeconfig=/etc/kubernetes/kubelet.conf create -f - 2>/dev/null || true\n"
                                    f"apiVersion: v1\n"
                                    f"kind: Node\n"
                                    f"metadata:\n"
                                    f"  name: $NODENAME\n"
                                    f"  labels:\n"
                                    f"    kubernetes.io/hostname: $NODENAME\n"
                                    f"    kubernetes.io/os: linux\n"
                                    f"    kubernetes.io/arch: amd64\n"
                                    f"NODEEOF\n"
                                    f"  fi\n"
                                    f"fi\n"
                                    if dnat_cluster_ip and dnat_vpc_ip else ""
                                )
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


def _build_ingress(req: TenantCreateRequest) -> dict[str, Any]:
    host = _endpoint_host(req.name)
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
            "ingressClassName": INGRESS_CLASS,
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
    vpc_info: dict[str, str] | None = None,
) -> None:
    """Create CAPI + Ingress resources in tenant namespace.

    Order: KamajiControlPlane + KubevirtCluster first → wait for TCP service
    ClusterIP → create Cluster with ClusterIP as controlPlaneEndpoint.

    When VPC isolation is enabled (vpc_info provided):
      - TCP pod gets Multus dual-NIC (eth0=mgmt, net1=VPC)
      - Workers use TCP's net1 (VPC) IP as controlPlaneEndpoint
      - External/CAPI access still uses ClusterIP service
    """
    custom = k8s.custom_api
    ns = _tenant_ns(req.name)

    # 1. Create infrastructure + control plane providers first
    pre_resources = [
        (KAMAJI_CP_GROUP, KAMAJI_CP_VERSION, "kamajicontrolplanes", _build_kamaji_cp_cr(req, vpc_info)),
        (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtclusters", _build_kubevirt_cluster_cr(req)),
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

    # 3. Wait for TCP service to get ClusterIP (used by CAPI/external access)
    tcp_ip = await _wait_for_tcp_service_ip(k8s, req.name, ns)

    # 4. Determine controlPlaneEndpoint for workers
    #    - Without VPC: workers use ClusterIP (same ovn-default network)
    #    - With VPC: workers use TCP pod's fixed net1 (VPC) IP
    #      The fixed IP is pre-allocated and pinned via Kube-OVN annotation,
    #      and certSANs already include it from KCP creation.
    if vpc_info:
        worker_endpoint_ip = vpc_info["fixed_ip"]
        logger.info(f"VPC mode: workers will use fixed net1 IP {worker_endpoint_ip} for {req.name}")
    else:
        worker_endpoint_ip = tcp_ip

    # 5. Patch Cluster controlPlaneEndpoint with the worker-reachable IP
    patch = {
        "spec": {
            "controlPlaneEndpoint": {
                "host": worker_endpoint_ip,
                "port": 6443,
            },
        },
    }
    await custom.patch_namespaced_custom_object(
        group=CAPI_GROUP, version=CAPI_VERSION, namespace=ns,
        plural="clusters", name=req.name, body=patch,
        _content_type="application/merge-patch+json",
    )
    logger.info(f"Patched Cluster {req.name} controlPlaneEndpoint to {worker_endpoint_ip}:6443")

    # 6. Create VM worker resources (skip for bare_metal)
    #    When VPC is enabled, pass ClusterIP for DNAT rule in preKubeadmCommands:
    #    Kamaji advertises ClusterIP as apiserver address, but workers on VPC
    #    can't reach it — DNAT redirects ClusterIP:6443 → VPC_IP:6443.
    if req.worker_type == "vm":
        kubeadm_cr = _build_kubeadm_config_template_cr(
            req,
            dnat_cluster_ip=tcp_ip if vpc_info else "",
            dnat_vpc_ip=worker_endpoint_ip if vpc_info else "",
        )
        vm_resources = [
            (CAPI_GROUP, CAPI_VERSION, "machinedeployments", _build_machine_deployment_cr(req)),
            (KUBEVIRT_INFRA_GROUP, KUBEVIRT_INFRA_VERSION, "kubevirtmachinetemplates", _build_kubevirt_machine_template_cr(req)),
            ("bootstrap.cluster.x-k8s.io", "v1beta1", "kubeadmconfigtemplates", kubeadm_cr),
        ]
        for group, version, plural, body in vm_resources:
            await custom.create_namespaced_custom_object(
                group=group, version=version, namespace=ns, plural=plural, body=body,
            )

    # 7. Ingress for external access (UI, kubectl from outside)
    networking_api = client.NetworkingV1Api(k8s._api_client)
    ingress_body = _build_ingress(req)
    await networking_api.create_namespaced_ingress(
        namespace=ns,
        body=ingress_body,
    )
