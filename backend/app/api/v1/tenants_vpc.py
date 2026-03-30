"""VPC lifecycle management for tenants.

Creates and deletes isolated VPCs with Kube-OVN, including subnets,
NADs, NetworkPolicies, egress gateway attachment, and VpcDns.
"""

import json
import logging
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

from app.api.v1.tenants_common import (
    TENANT_NS_PREFIX,
    VPCDNS_VIP,
    VPCDNS_FORWARD_DNS,
)

logger = logging.getLogger(__name__)


async def _ensure_vpcdns_prerequisites(k8s, kubeovn_ns: str) -> None:
    """Ensure shared VpcDns prerequisites exist (idempotent).

    Creates ServiceAccount, ClusterRole, ClusterRoleBinding, CoreDNS ConfigMap,
    NAD in default namespace, and vpc-dns-config ConfigMap.
    These are shared across all tenants — created once, never deleted per-tenant.
    """
    rbac_api = client.RbacAuthorizationV1Api(k8s._api_client)

    # 1. ServiceAccount vpc-dns in kube-ovn namespace
    try:
        await k8s.core_api.read_namespaced_service_account(name="vpc-dns", namespace=kubeovn_ns)
    except ApiException as e:
        if e.status == 404:
            sa = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="vpc-dns", namespace=kubeovn_ns),
            )
            await k8s.core_api.create_namespaced_service_account(namespace=kubeovn_ns, body=sa)
            logger.info(f"Created ServiceAccount vpc-dns in {kubeovn_ns}")
        else:
            raise

    # 2. ClusterRole system:vpc-dns
    try:
        await rbac_api.read_cluster_role(name="system:vpc-dns")
    except ApiException as e:
        if e.status == 404:
            cr = client.V1ClusterRole(
                metadata=client.V1ObjectMeta(
                    name="system:vpc-dns",
                    labels={"kubernetes.io/bootstrapping": "rbac-defaults"},
                ),
                rules=[
                    client.V1PolicyRule(
                        api_groups=[""],
                        resources=["endpoints", "services", "pods", "namespaces"],
                        verbs=["list", "watch"],
                    ),
                    client.V1PolicyRule(
                        api_groups=["discovery.k8s.io"],
                        resources=["endpointslices"],
                        verbs=["list", "watch"],
                    ),
                ],
            )
            await rbac_api.create_cluster_role(body=cr)
            logger.info("Created ClusterRole system:vpc-dns")
        else:
            raise

    # 3. ClusterRoleBinding vpc-dns
    try:
        await rbac_api.read_cluster_role_binding(name="vpc-dns")
    except ApiException as e:
        if e.status == 404:
            crb = client.V1ClusterRoleBinding(
                metadata=client.V1ObjectMeta(
                    name="vpc-dns",
                    labels={"kubernetes.io/bootstrapping": "rbac-defaults"},
                    annotations={"rbac.authorization.kubernetes.io/autoupdate": "true"},
                ),
                role_ref=client.V1RoleRef(
                    api_group="rbac.authorization.k8s.io",
                    kind="ClusterRole",
                    name="system:vpc-dns",
                ),
                subjects=[
                    client.RbacV1Subject(
                        kind="ServiceAccount",
                        name="vpc-dns",
                        namespace=kubeovn_ns,
                    ),
                ],
            )
            await rbac_api.create_cluster_role_binding(body=crb)
            logger.info("Created ClusterRoleBinding vpc-dns")
        else:
            raise

    # 4. ConfigMap vpc-dns-corefile in kube-ovn namespace
    try:
        await k8s.core_api.read_namespaced_config_map(name="vpc-dns-corefile", namespace=kubeovn_ns)
    except ApiException as e:
        if e.status == 404:
            corefile = (
                ".:53 {\n"
                "    errors\n"
                "    health {\n"
                "      lameduck 5s\n"
                "    }\n"
                "    ready\n"
                "    kubernetes cluster.local in-addr.arpa ip6.arpa {\n"
                "      pods insecure\n"
                "      fallthrough in-addr.arpa ip6.arpa\n"
                "    }\n"
                "    prometheus :9153\n"
                f"    forward . {VPCDNS_FORWARD_DNS} {{\n"
                "      prefer_udp\n"
                "    }\n"
                "    cache 30\n"
                "    loop\n"
                "    reload\n"
                "    loadbalance\n"
                "}\n"
            )
            cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name="vpc-dns-corefile", namespace=kubeovn_ns),
                data={"Corefile": corefile},
            )
            await k8s.core_api.create_namespaced_config_map(namespace=kubeovn_ns, body=cm)
            logger.info(f"Created ConfigMap vpc-dns-corefile in {kubeovn_ns}")
        else:
            raise

    # 5. NAD ovn-nad in default namespace
    try:
        await k8s.custom_api.get_namespaced_custom_object(
            group="k8s.cni.cncf.io", version="v1",
            namespace="default", plural="network-attachment-definitions",
            name="ovn-nad",
        )
    except ApiException as e:
        if e.status == 404:
            nad = {
                "apiVersion": "k8s.cni.cncf.io/v1",
                "kind": "NetworkAttachmentDefinition",
                "metadata": {"name": "ovn-nad", "namespace": "default"},
                "spec": {
                    "config": json.dumps({
                        "cniVersion": "0.3.0",
                        "type": "kube-ovn",
                        "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
                        "provider": "ovn-nad.default.ovn",
                    }),
                },
            }
            await k8s.custom_api.create_namespaced_custom_object(
                group="k8s.cni.cncf.io", version="v1",
                namespace="default", plural="network-attachment-definitions",
                body=nad,
            )
            logger.info("Created NAD ovn-nad in default namespace")
        else:
            raise

    # 6. ConfigMap vpc-dns-config in kube-ovn namespace
    try:
        await k8s.core_api.read_namespaced_config_map(name="vpc-dns-config", namespace=kubeovn_ns)
    except ApiException as e:
        if e.status == 404:
            cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name="vpc-dns-config", namespace=kubeovn_ns),
                data={
                    "enable-vpc-dns": "true",
                    "coredns-vip": VPCDNS_VIP,
                    "nad-name": "ovn-nad",
                    "nad-provider": "ovn-nad.default.ovn",
                },
            )
            await k8s.core_api.create_namespaced_config_map(namespace=kubeovn_ns, body=cm)
            logger.info(f"Created ConfigMap vpc-dns-config in {kubeovn_ns}")
        else:
            raise

    logger.info("VpcDns prerequisites verified/created")


async def _create_vpcdns_for_tenant(k8s, tenant_name: str, vpc_name: str, subnet_name: str) -> None:
    """Create a VpcDns CR for a tenant VPC."""
    vpcdns_name = f"vpc-dns-{tenant_name}"
    vpcdns_manifest = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "VpcDns",
        "metadata": {
            "name": vpcdns_name,
            "labels": {
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/tenant": tenant_name,
            },
        },
        "spec": {
            "vpc": vpc_name,
            "subnet": subnet_name,
            "replicas": 2,
        },
    }
    await k8s.custom_api.create_cluster_custom_object(
        group=KUBEOVN_API_GROUP,
        version=KUBEOVN_API_VERSION,
        plural="vpc-dnses",
        body=vpcdns_manifest,
    )
    logger.info(f"Created VpcDns {vpcdns_name} for VPC {vpc_name}")


async def _delete_vpcdns_for_tenant(k8s, tenant_name: str) -> None:
    """Delete VpcDns CR for a tenant (shared resources are NOT deleted)."""
    vpcdns_name = f"vpc-dns-{tenant_name}"
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpc-dnses",
            name=vpcdns_name,
        )
        logger.info(f"Deleted VpcDns {vpcdns_name}")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete VpcDns {vpcdns_name}: {e}")


async def _create_tenant_vpc(k8s, tenant_name: str) -> dict[str, str]:
    """Create an isolated VPC for tenant networking (Architecture C: dual-NIC).

    Architecture C: TCP pod gets dual-NIC via Multus:
      - eth0: ovn-default (management — reaches CoreDNS, CNPG, CAPI)
      - net1: tenant VPC subnet (workers connect here)

    Creates: VPC → Subnet → NAD → NetworkPolicy → attach to egress gateway.
    Egress gateway provides SNAT via macvlan + VPC peering (hub-and-spoke).
    Returns dict with vpc_name, subnet_name, nad_name, cidr for caller use.
    """
    from app.api.v1.network import create_nad_for_subnet, get_nad_provider
    from app.api.v1.egress_gateway import attach_tenant_to_gateway
    from app.core.allocators import allocate_vpc_cidr

    cidr, gateway = await allocate_vpc_cidr(k8s)
    vpc_name = f"vpc-{tenant_name}"
    subnet_name = f"{vpc_name}-default"
    tenant_ns = f"{TENANT_NS_PREFIX}{tenant_name}"
    nad_name = subnet_name  # NAD name matches subnet for clarity

    labels = {
        "kubevirt-ui.io/managed": "true",
        "kubevirt-ui.io/tenant": tenant_name,
    }

    # 1. Create VPC (routes will be added by egress gateway attach)
    vpc_spec: dict[str, Any] = {}

    vpc_manifest = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Vpc",
        "metadata": {"name": vpc_name, "labels": labels},
        "spec": vpc_spec,
    }
    await k8s.custom_api.create_cluster_custom_object(
        group=KUBEOVN_API_GROUP,
        version=KUBEOVN_API_VERSION,
        plural="vpcs",
        body=vpc_manifest,
    )

    # 2. Create default subnet in VPC
    #    provider must match NAD: {nad_name}.{namespace}.ovn
    provider = get_nad_provider(nad_name, tenant_ns)
    # ACL rules: allow intra-VPC, block host/private networks, allow internet
    acls = [
        {"action": "allow-related", "direction": "from-lport",
         "match": f"ip4.src == {cidr} && ip4.dst == {cidr}", "priority": 3000},
        {"action": "allow-related", "direction": "from-lport",
         "match": f"ip4.src == {cidr} && ip4.dst == {VPCDNS_VIP}", "priority": 2500},
        {"action": "drop", "direction": "from-lport",
         "match": f"ip4.src == {cidr} && ip4.dst == 192.168.196.0/24", "priority": 2000},
        {"action": "drop", "direction": "from-lport",
         "match": f"ip4.src == {cidr} && ip4.dst == 10.0.0.0/8", "priority": 1999},
        {"action": "drop", "direction": "from-lport",
         "match": f"ip4.src == {cidr} && ip4.dst == 172.16.0.0/12", "priority": 1998},
        {"action": "drop", "direction": "from-lport",
         "match": f"ip4.src == {cidr} && ip4.dst == 192.168.203.0/24", "priority": 1997},
        {"action": "allow-related", "direction": "from-lport",
         "match": f"ip4.src == {cidr}", "priority": 1000},
    ]

    # Reserve fixed IP for TCP pod (gateway + 1) — must be excluded from DHCP pool
    # so VpcDns pods don't claim it before the TCP deployment starts.
    gw_parts = gateway.split(".")
    fixed_ip = f"{gw_parts[0]}.{gw_parts[1]}.{gw_parts[2]}.{int(gw_parts[3]) + 1}"

    subnet_manifest = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Subnet",
        "metadata": {"name": subnet_name, "labels": labels},
        "spec": {
            "protocol": "IPv4",
            "cidrBlock": cidr,
            "gateway": gateway,
            "vpc": vpc_name,
            "provider": provider,
            "enableDHCP": True,
            "natOutgoing": False,
            "excludeIps": [fixed_ip],
            "acls": acls,
        },
    }
    await k8s.custom_api.create_cluster_custom_object(
        group=KUBEOVN_API_GROUP,
        version=KUBEOVN_API_VERSION,
        plural="subnets",
        body=subnet_manifest,
    )

    # 3. Create NAD in tenant namespace — Multus uses this to attach net1
    await create_nad_for_subnet(k8s, nad_name, tenant_ns)

    # 4. NetworkPolicy: isolate tenant from other tenants, allow infra namespaces
    network_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{tenant_name}-isolation",
            "namespace": tenant_ns,
            "labels": labels,
        },
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress"],
            "ingress": [
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": tenant_ns}}}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "o0-cnpg"}}}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "o0-kamaji"}}}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "o0-capi"}}}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "flux-system"}}}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "o0-ingress-nginx"}}}]},
            ],
        },
    }
    networking_api = client.NetworkingV1Api(k8s._api_client)
    await networking_api.create_namespaced_network_policy(
        namespace=tenant_ns,
        body=network_policy,
    )

    # 5. Attach to egress gateway for external connectivity (SNAT via macvlan)
    #    Uses hub-and-spoke VPC peering — gateway_name=None finds default gateway
    gw_result = await attach_tenant_to_gateway(
        k8s, gateway_name=None,
        tenant_vpc_name=vpc_name, tenant_subnet_name=subnet_name,
        tenant_cidr=cidr,
    )
    if gw_result:
        logger.info(f"Attached VPC {vpc_name} to egress gateway (transit IP: {gw_result.transit_ip})")
    else:
        logger.warning(f"No egress gateway found — VPC {vpc_name} will have no external connectivity")

    # 6. Set up VpcDns — in-cluster DNS resolution for VMs in VPC
    from app.api.v1.network import _find_kubeovn_namespace
    kubeovn_ns = await _find_kubeovn_namespace(k8s)
    await _ensure_vpcdns_prerequisites(k8s, kubeovn_ns)
    await _create_vpcdns_for_tenant(k8s, tenant_name, vpc_name, subnet_name)

    logger.info(f"Created VPC {vpc_name} with subnet {subnet_name} ({cidr}), NAD in {tenant_ns}, fixed TCP IP {fixed_ip}")
    return {
        "vpc_name": vpc_name,
        "subnet_name": subnet_name,
        "nad_name": nad_name,
        "cidr": cidr,
        "fixed_ip": fixed_ip,
        "provider": provider,
    }


async def _delete_tenant_vpc(k8s, tenant_name: str) -> None:
    """Delete VPC resources associated with a tenant.

    Order matters due to finalizers:
    VpcDns → detach from egress gateway → Subnets → VPC
    Shared resources (RBAC, ConfigMaps) are NOT deleted.
    """
    from app.api.v1.egress_gateway import (
        detach_tenant_from_gateway, _find_gateway_for_vpc,
    )

    vpc_name = f"vpc-{tenant_name}"
    subnet_name = f"{vpc_name}-default"
    label_sel = f"kubevirt-ui.io/tenant={tenant_name}"

    # Delete VpcDns CR first (before subnets/VPC)
    await _delete_vpcdns_for_tenant(k8s, tenant_name)

    # Detach from egress gateway (removes peering, routes, policies)
    gateway_name = await _find_gateway_for_vpc(k8s, vpc_name)
    if gateway_name:
        try:
            await detach_tenant_from_gateway(k8s, gateway_name, vpc_name, subnet_name)
        except Exception as e:
            logger.warning(f"Failed to detach VPC {vpc_name} from egress gateway: {e}")

    # Delete subnets
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            label_selector=label_sel,
        )
        for item in result.get("items", []):
            try:
                await k8s.custom_api.delete_cluster_custom_object(
                    group=KUBEOVN_API_GROUP,
                    version=KUBEOVN_API_VERSION,
                    plural="subnets",
                    name=item["metadata"]["name"],
                )
            except ApiException:
                pass
    except ApiException:
        pass

    # Delete VPC
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            name=vpc_name,
        )
        logger.info(f"Deleted VPC {vpc_name} for tenant {tenant_name}")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete VPC {vpc_name}: {e}")
