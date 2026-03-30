"""Kube-OVN Network Management API endpoints."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.core.allocators import allocate_vpc_cidr
from app.core.auth import User, require_auth
from app.core.errors import k8s_error_to_http
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.models.network import (
    ProviderNetworkCreate,
    ProviderNetworkResponse,
    VlanCreate,
    VlanResponse,
    SubnetCreate,
    SubnetResponse,
    SubnetStatistics,
    SubnetDetail,
    IPLease,
    ReserveIPRequest,
    ReservedIP,
    NodeNetworkInfo,
    NetworkOverview,
    VpcCreate,
    VpcResponse,
    VpcSubnetInfo,
    VpcPeeringCreate,
    VpcPeeringInfo,
)

logger = logging.getLogger(__name__)
router = APIRouter()




# ============================================================================
# Helper Functions
# ============================================================================

def parse_ip_range(ip_range: str) -> int:
    """Parse IP range and return count of IPs.
    
    Examples:
        "192.168.1.50" -> 1
        "192.168.1.50..192.168.1.60" -> 11
    """
    if ".." in ip_range:
        start, end = ip_range.split("..")
        start_last = int(start.strip().split(".")[-1])
        end_last = int(end.strip().split(".")[-1])
        return end_last - start_last + 1
    return 1


# NetworkAttachmentDefinition (NAD) constants
NAD_API_GROUP = "k8s.cni.cncf.io"
NAD_API_VERSION = "v1"


async def _find_kubeovn_namespace(k8s) -> str:
    """Find the namespace where kube-ovn controller is deployed.

    This is needed for infrastructure subnets — their NADs must be in
    the kube-ovn namespace because VPC NAT Gateway pods run there.
    """
    try:
        deployments = await k8s.apps_api.list_deployment_for_all_namespaces(
            label_selector="app=kube-ovn-controller",
        )
        if deployments.items:
            return deployments.items[0].metadata.namespace
    except ApiException:
        pass
    # Fallback: search by deployment name
    try:
        deployments = await k8s.apps_api.list_deployment_for_all_namespaces()
        for dep in deployments.items:
            if dep.metadata.name == "kube-ovn-controller":
                return dep.metadata.namespace
    except ApiException:
        pass
    raise HTTPException(status_code=500, detail="Cannot find kube-ovn namespace")


async def _find_infra_subnet(k8s) -> dict | None:
    """Find the infrastructure subnet for VPC NAT Gateway external connectivity."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            label_selector="kubevirt-ui.io/purpose=infrastructure",
        )
        items = result.get("items", [])
        if items:
            return items[0]
    except ApiException:
        pass
    return None


async def _label_nodes_for_external_gw(k8s, vlan_name: str) -> int:
    """Label provider network ready nodes with ovn.kubernetes.io/external-gw=true.

    This is required for OVN-based NAT to work. Finds the provider network
    associated with the VLAN and labels its ready nodes.
    Returns the number of nodes labeled.
    """
    # Find the VLAN to get its provider network name
    try:
        vlan = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vlans",
            name=vlan_name,
        )
    except ApiException:
        logger.warning(f"VLAN {vlan_name} not found, cannot label nodes")
        return 0

    provider_name = vlan.get("spec", {}).get("provider", "")
    if not provider_name:
        return 0

    # Get provider network to find ready nodes
    try:
        pn = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="provider-networks",
            name=provider_name,
        )
    except ApiException:
        logger.warning(f"Provider network {provider_name} not found")
        return 0

    ready_nodes = pn.get("status", {}).get("readyNodes", [])
    if not ready_nodes:
        logger.warning(f"Provider network {provider_name} has no ready nodes")
        return 0

    labeled_count = 0
    for node_name in ready_nodes:
        try:
            node = await k8s.core_api.read_node(name=node_name)
            node_labels = node.metadata.labels or {}
            if node_labels.get("ovn.kubernetes.io/external-gw") == "true":
                continue  # Already labeled
            await k8s.core_api.patch_node(
                name=node_name,
                body={"metadata": {"labels": {"ovn.kubernetes.io/external-gw": "true"}}},
            )
            labeled_count += 1
            logger.info(f"Labeled node {node_name} with ovn.kubernetes.io/external-gw=true")
        except ApiException as e:
            logger.warning(f"Failed to label node {node_name}: {e}")

    return labeled_count


def get_nad_provider(nad_name: str, namespace: str) -> str:
    """Get the Kube-OVN provider string for a NAD.
    
    Format: {nad_name}.{namespace}.ovn
    This must match the subnet's spec.provider field.
    """
    return f"{nad_name}.{namespace}.ovn"


async def create_nad_for_subnet(k8s, nad_name: str, namespace: str) -> None:
    """Create a NetworkAttachmentDefinition for a Kube-OVN subnet in a namespace.
    
    This allows KubeVirt VMs to connect to the Kube-OVN subnet using Multus.
    The NAD name + namespace determine the provider: {nad_name}.{namespace}.ovn
    The subnet's spec.provider MUST match this value.
    """
    import json
    
    provider = get_nad_provider(nad_name, namespace)
    
    # Kube-OVN NAD config
    config = {
        "cniVersion": "0.3.1",
        "type": "kube-ovn",
        "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
        "provider": provider,
    }
    
    nad_manifest = {
        "apiVersion": f"{NAD_API_GROUP}/{NAD_API_VERSION}",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {
            "name": nad_name,
            "namespace": namespace,
        },
        "spec": {
            "config": json.dumps(config),
        },
    }
    
    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group=NAD_API_GROUP,
            version=NAD_API_VERSION,
            namespace=namespace,
            plural="network-attachment-definitions",
            body=nad_manifest,
        )
        logger.info(f"Created NAD {nad_name} in namespace {namespace} (provider: {provider})")
    except ApiException as e:
        if e.status == 409:
            logger.info(f"NAD {nad_name} already exists in namespace {namespace}")
        else:
            raise


def extract_vm_name(pod_name: str) -> tuple[str, str]:
    """Extract VM name from virt-launcher pod name.
    
    Returns:
        tuple of (resource_type, resource_name)
    """
    if pod_name and "virt-launcher-" in pod_name:
        # Format: virt-launcher-{vm-name}-{hash}
        parts = pod_name.replace("virt-launcher-", "").rsplit("-", 1)
        if parts:
            return "vm", parts[0]
    return "pod", pod_name


# ============================================================================
# Network Overview
# ============================================================================

@router.get("/overview", response_model=NetworkOverview)
async def get_network_overview(request: Request, user: User = Depends(require_auth)) -> NetworkOverview:
    """Get overview of all network resources."""
    k8s = request.app.state.k8s_client
    
    try:
        # Count provider networks
        pn_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="provider-networks",
        )
        provider_networks = len(pn_result.get("items", []))
    except ApiException:
        provider_networks = 0
    
    try:
        # Count VLANs
        vlan_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vlans",
        )
        vlans = len(vlan_result.get("items", []))
    except ApiException:
        vlans = 0
    
    try:
        # Count subnets and IP usage
        subnet_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
        )
        subnets_list = subnet_result.get("items", [])
        subnets = len(subnets_list)
        
        total_used = 0
        total_available = 0
        for subnet in subnets_list:
            status = subnet.get("status", {})
            total_used += status.get("v4usingIPs", 0) or status.get("v6usingIPs", 0)
            total_available += status.get("v4availableIPs", 0) or status.get("v6availableIPs", 0)
    except ApiException:
        subnets = 0
        total_used = 0
        total_available = 0
    
    try:
        vpc_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            label_selector="kubevirt-ui.io/managed=true",
        )
        vpc_count = len(vpc_result.get("items", []))
    except ApiException:
        vpc_count = 0

    return NetworkOverview(
        provider_networks=provider_networks,
        vlans=vlans,
        subnets=subnets,
        vpcs=vpc_count,
        total_ips_used=total_used,
        total_ips_available=total_available,
    )


# ============================================================================
# Provider Networks
# ============================================================================

@router.get("/provider-networks", response_model=list[ProviderNetworkResponse])
async def list_provider_networks(request: Request, user: User = Depends(require_auth)) -> list[ProviderNetworkResponse]:
    """List all Kube-OVN ProviderNetworks."""
    k8s = request.app.state.k8s_client
    
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="provider-networks",
        )
    except ApiException as e:
        if e.status == 404:
            return []  # Kube-OVN not installed
        raise k8s_error_to_http(e, "network operation")
    
    networks = []
    for item in result.get("items", []):
        spec = item.get("spec", {})
        status = item.get("status", {})
        
        # Parse conditions
        conditions = status.get("conditions", [])
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in conditions
        )
        
        # vlans can be a list or empty dict depending on Kube-OVN version/state
        vlans_raw = status.get("vlans", [])
        vlans_list = vlans_raw if isinstance(vlans_raw, list) else []
        
        networks.append(ProviderNetworkResponse(
            name=item["metadata"]["name"],
            default_interface=spec.get("defaultInterface", ""),
            auto_create_vlan_subinterfaces=spec.get("autoCreateVlanSubinterfaces", False),
            exchange_link_name=spec.get("exchangeLinkName", False),
            ready=ready,
            ready_nodes=status.get("readyNodes", []),
            not_ready_nodes=status.get("notReadyNodes", []),
            vlans=vlans_list,
            conditions=conditions,
        ))
    
    return networks


@router.post("/provider-networks", response_model=ProviderNetworkResponse)
async def create_provider_network(
    request: Request,
    data: ProviderNetworkCreate,
    user: User = Depends(require_auth),
) -> ProviderNetworkResponse:
    """Create a new ProviderNetwork (connects to physical network)."""
    k8s = request.app.state.k8s_client
    
    manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "ProviderNetwork",
        "metadata": {"name": data.name},
        "spec": {
            "defaultInterface": data.default_interface,
        }
    }
    
    # RECOMMENDED: autoCreateVlanSubinterfaces keeps management traffic on base interface
    # while Kube-OVN automatically creates VLAN sub-interfaces for VM traffic
    # This is the SAFE option for single-NIC setups
    if data.auto_create_vlan_subinterfaces:
        manifest["spec"]["autoCreateVlanSubinterfaces"] = True
    
    # LEGACY/DANGEROUS: exchangeLinkName moves IP from physical interface to OVS bridge
    # This can break cluster connectivity - use autoCreateVlanSubinterfaces instead!
    if data.exchange_link_name:
        manifest["spec"]["exchangeLinkName"] = True
    
    # Add custom interface mappings if provided
    if data.custom_interfaces:
        manifest["spec"]["customInterfaces"] = [
            {"interface": iface, "nodes": [node]}
            for node, iface in data.custom_interfaces.items()
        ]
    
    try:
        result = await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="provider-networks",
            body=manifest,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")
    
    return ProviderNetworkResponse(
        name=result["metadata"]["name"],
        default_interface=data.default_interface,
        auto_create_vlan_subinterfaces=data.auto_create_vlan_subinterfaces,
        exchange_link_name=data.exchange_link_name,
        ready=False,
        ready_nodes=[],
        not_ready_nodes=[],
        vlans=[],
        conditions=[],
    )


@router.get("/provider-networks/{name}", response_model=ProviderNetworkResponse)
async def get_provider_network(request: Request, name: str, user: User = Depends(require_auth)) -> ProviderNetworkResponse:
    """Get a specific ProviderNetwork."""
    k8s = request.app.state.k8s_client
    
    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="provider-networks",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"ProviderNetwork '{name}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    spec = item.get("spec", {})
    status = item.get("status", {})
    conditions = status.get("conditions", [])
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conditions
    )
    
    # vlans can be a list or empty dict depending on Kube-OVN version/state
    vlans_raw = status.get("vlans", [])
    vlans_list = vlans_raw if isinstance(vlans_raw, list) else []
    
    return ProviderNetworkResponse(
        name=item["metadata"]["name"],
        default_interface=spec.get("defaultInterface", ""),
        auto_create_vlan_subinterfaces=spec.get("autoCreateVlanSubinterfaces", False),
        exchange_link_name=spec.get("exchangeLinkName", False),
        ready=ready,
        ready_nodes=status.get("readyNodes", []),
        not_ready_nodes=status.get("notReadyNodes", []),
        vlans=vlans_list,
        conditions=conditions,
    )


@router.delete("/provider-networks/{name}")
async def delete_provider_network(request: Request, name: str, user: User = Depends(require_auth)) -> dict:
    """Delete a ProviderNetwork and all dependent VLANs and Subnets.

    Cascade order: subnets → NADs → VLANs → ProviderNetwork.
    kube-ovn blocks ProviderNetwork deletion while dependent resources exist.
    """
    k8s = request.app.state.k8s_client

    # 1. Find VLANs that reference this provider
    try:
        vlans_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="vlans",
        )
        dependent_vlans = [
            v["metadata"]["name"]
            for v in vlans_result.get("items", [])
            if v.get("spec", {}).get("provider") == name
        ]
    except ApiException:
        dependent_vlans = []

    # 2. Find subnets that reference these VLANs and delete them + their NADs
    for vlan_name in dependent_vlans:
        try:
            subnets_result = await k8s.custom_api.list_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
            )
            for s in subnets_result.get("items", []):
                if s.get("spec", {}).get("vlan") == vlan_name:
                    subnet_name = s["metadata"]["name"]
                    # Delete NAD if it exists (extract namespace from provider)
                    provider_str = s.get("spec", {}).get("provider", "")
                    if provider_str and provider_str.endswith(".ovn"):
                        parts = provider_str.rsplit(".", 2)
                        if len(parts) == 3:
                            nad_name, nad_ns = parts[0], parts[1]
                            try:
                                await k8s.custom_api.delete_namespaced_custom_object(
                                    group=NAD_API_GROUP, version=NAD_API_VERSION,
                                    namespace=nad_ns, plural="network-attachment-definitions",
                                    name=nad_name,
                                )
                            except ApiException:
                                pass
                    # Delete subnet
                    try:
                        await k8s.custom_api.delete_cluster_custom_object(
                            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                            plural="subnets", name=subnet_name,
                        )
                        logger.info(f"Cascade deleted subnet {subnet_name}")
                    except ApiException:
                        pass
        except ApiException:
            pass

    # 3. Delete VLANs
    for vlan_name in dependent_vlans:
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="vlans", name=vlan_name,
            )
            logger.info(f"Cascade deleted VLAN {vlan_name}")
        except ApiException:
            pass

    # 4. Delete ProviderNetwork
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="provider-networks",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"ProviderNetwork '{name}' not found")
        raise k8s_error_to_http(e, "network operation")

    return {"status": "deleted", "name": name, "cascade": {"vlans": dependent_vlans}}


# ============================================================================
# VLANs
# ============================================================================

@router.get("/vlans", response_model=list[VlanResponse])
async def list_vlans(request: Request, user: User = Depends(require_auth)) -> list[VlanResponse]:
    """List all Kube-OVN VLANs."""
    k8s = request.app.state.k8s_client
    
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vlans",
        )
    except ApiException as e:
        if e.status == 404:
            return []
        raise k8s_error_to_http(e, "network operation")
    
    vlans = []
    for item in result.get("items", []):
        spec = item.get("spec", {})
        vlans.append(VlanResponse(
            name=item["metadata"]["name"],
            id=spec.get("id", 0),
            provider=spec.get("provider", ""),
        ))
    
    return vlans


@router.post("/vlans", response_model=VlanResponse)
async def create_vlan(request: Request, data: VlanCreate, user: User = Depends(require_auth)) -> VlanResponse:
    """Create a new VLAN."""
    k8s = request.app.state.k8s_client
    
    manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Vlan",
        "metadata": {"name": data.name},
        "spec": {
            "id": data.id,
            "provider": data.provider,
        }
    }
    
    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vlans",
            body=manifest,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")
    
    return VlanResponse(
        name=data.name,
        id=data.id,
        provider=data.provider,
    )


@router.delete("/vlans/{name}")
async def delete_vlan(request: Request, name: str, user: User = Depends(require_auth)) -> dict:
    """Delete a VLAN and all dependent Subnets/NADs.

    kube-ovn blocks VLAN deletion while dependent subnets exist.
    """
    k8s = request.app.state.k8s_client

    # 1. Delete dependent subnets + NADs
    deleted_subnets = []
    try:
        subnets_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
        )
        for s in subnets_result.get("items", []):
            if s.get("spec", {}).get("vlan") == name:
                subnet_name = s["metadata"]["name"]
                # Delete NAD
                provider_str = s.get("spec", {}).get("provider", "")
                if provider_str and provider_str.endswith(".ovn"):
                    parts = provider_str.rsplit(".", 2)
                    if len(parts) == 3:
                        try:
                            await k8s.custom_api.delete_namespaced_custom_object(
                                group=NAD_API_GROUP, version=NAD_API_VERSION,
                                namespace=parts[1], plural="network-attachment-definitions",
                                name=parts[0],
                            )
                        except ApiException:
                            pass
                # Delete subnet
                try:
                    await k8s.custom_api.delete_cluster_custom_object(
                        group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                        plural="subnets", name=subnet_name,
                    )
                    deleted_subnets.append(subnet_name)
                    logger.info(f"Cascade deleted subnet {subnet_name}")
                except ApiException:
                    pass
    except ApiException:
        pass

    # 2. Delete VLAN
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vlans",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VLAN '{name}' not found")
        raise k8s_error_to_http(e, "network operation")

    return {"status": "deleted", "name": name, "cascade": {"subnets": deleted_subnets}}


# ============================================================================
# Subnets
# ============================================================================

@router.get("/subnets", response_model=list[SubnetResponse])
async def list_subnets(request: Request, user: User = Depends(require_auth)) -> list[SubnetResponse]:
    """List all Kube-OVN Subnets."""
    k8s = request.app.state.k8s_client
    
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
        )
    except ApiException as e:
        if e.status == 404:
            return []
        raise k8s_error_to_http(e, "network operation")
    
    subnets = []
    for item in result.get("items", []):
        spec = item.get("spec", {})
        status = item.get("status", {})
        
        # Calculate statistics
        available = status.get("v4availableIPs", 0) or status.get("v6availableIPs", 0)
        used = status.get("v4usingIPs", 0) or status.get("v6usingIPs", 0)
        
        # Calculate reserved IPs count
        exclude_ips = spec.get("excludeIps", [])
        reserved = sum(parse_ip_range(r) for r in exclude_ips)
        
        # Check ready status - Kube-OVN uses 'validated' or conditions
        is_ready = status.get("validated", False)
        if not is_ready:
            # Fallback: check conditions array
            conditions = status.get("conditions", [])
            is_ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )
        # Also consider subnet ready if it has IPs allocated (practical check)
        if not is_ready and (available > 0 or used > 0):
            is_ready = True
        
        # Extract namespace from provider format: {nad_name}.{namespace}.ovn
        provider_str = spec.get("provider")
        namespace = None
        if provider_str and provider_str.endswith(".ovn"):
            parts = provider_str.rsplit(".", 2)  # [nad_name, namespace, "ovn"]
            if len(parts) == 3:
                namespace = parts[1]
        
        # Read purpose from label (default to "vm" for backwards compatibility)
        labels = item.get("metadata", {}).get("labels", {})
        purpose = labels.get("kubevirt-ui.io/purpose", "vm")

        subnets.append(SubnetResponse(
            name=item["metadata"]["name"],
            cidr_block=spec.get("cidrBlock", ""),
            gateway=spec.get("gateway", ""),
            exclude_ips=exclude_ips,
            provider=provider_str,
            vlan=spec.get("vlan"),
            vpc=spec.get("vpc"),
            namespace=namespace,
            protocol=spec.get("protocol", "IPv4"),
            enable_dhcp=spec.get("enableDHCP", True),
            disable_gateway_check=spec.get("disableGatewayCheck", False),
            purpose=purpose,
            statistics=SubnetStatistics(
                total=available + used,
                available=available,
                used=used,
                reserved=reserved,
            ),
            ready=is_ready,
        ))
    
    return subnets


@router.post("/subnets", response_model=SubnetResponse)
async def create_subnet(request: Request, data: SubnetCreate, user: User = Depends(require_auth)) -> SubnetResponse:
    """Create a new Subnet with Multus NAD for KubeVirt VMs.
    
    For VLAN-based subnets:
    1. Creates Subnet with provider={nad_name}.{namespace}.ovn
    2. Creates NAD in the namespace with matching provider
    
    The NAD name is derived from the VLAN name (e.g., vlan111).
    The provider format {nad_name}.{namespace}.ovn links Subnet ↔ NAD.
    VMs use Multus with default:true to connect to this network.
    """
    k8s = request.app.state.k8s_client
    
    # Determine NAD name and provider for Multus integration
    # NAD name = VLAN name (e.g., "vlan111"), provider = "vlan111.test.ovn"
    nad_name = data.vlan if data.vlan else data.name
    is_infra = data.purpose == "infrastructure"

    # For infrastructure subnets, NAD goes in kube-ovn namespace (VPC NAT Gateway pods live there)
    # For VM subnets, NAD goes in the user-specified namespace
    if is_infra and data.vlan:
        kubeovn_ns = await _find_kubeovn_namespace(k8s)
        infra_nad_name = data.name  # Use subnet name as NAD name
        provider = get_nad_provider(infra_nad_name, kubeovn_ns)
    elif data.namespace:
        provider = get_nad_provider(nad_name, data.namespace)
    else:
        provider = None

    manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Subnet",
        "metadata": {
            "name": data.name,
            "labels": {
                "kubevirt-ui.io/purpose": data.purpose,
            },
        },
        "spec": {
            "protocol": "IPv4",
            "cidrBlock": data.cidr_block,
            "gateway": data.gateway,
            "enableDHCP": data.enable_dhcp,
        }
    }

    if data.exclude_ips:
        manifest["spec"]["excludeIps"] = data.exclude_ips

    if data.vlan:
        manifest["spec"]["vlan"] = data.vlan
        # Both infra and VM subnets need provider for NAD linkage
        if provider:
            manifest["spec"]["provider"] = provider

    if data.vpc:
        manifest["spec"]["vpc"] = data.vpc

    if data.disable_gateway_check:
        manifest["spec"]["disableGatewayCheck"] = True

    # NOTE: We intentionally DO NOT add namespaces to the Subnet manifest!
    # Adding namespaces causes Kube-OVN to annotate the namespace with logical_switch,
    # which makes ALL pods in that namespace use this subnet (breaking CDI, services, etc.)
    # Instead, we create a NAD so VMs can explicitly opt-in via Multus.

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            body=manifest,
        )

        if data.vlan:
            if is_infra:
                # Infrastructure subnets: NAD in kube-ovn namespace for OVN NAT
                try:
                    await create_nad_for_subnet(k8s, infra_nad_name, kubeovn_ns)
                except ApiException as nad_err:
                    logger.warning(f"Failed to create infra NAD in {kubeovn_ns}: {nad_err}")
                # Label provider network ready nodes for OVN external gateway
                labeled = await _label_nodes_for_external_gw(k8s, data.vlan)
                logger.info(f"Labeled {labeled} nodes for OVN external gateway")
            elif data.namespace:
                # VM subnets: NAD in user namespace for Multus attachment
                try:
                    await create_nad_for_subnet(k8s, nad_name, data.namespace)
                except ApiException as nad_err:
                    logger.warning(f"Failed to create NAD in {data.namespace}: {nad_err}")

    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")

    reserved = sum(parse_ip_range(r) for r in data.exclude_ips)

    return SubnetResponse(
        name=data.name,
        cidr_block=data.cidr_block,
        gateway=data.gateway,
        exclude_ips=data.exclude_ips,
        provider=provider,
        vlan=data.vlan,
        vpc=data.vpc,
        namespace=data.namespace if not is_infra else None,
        enable_dhcp=data.enable_dhcp,
        disable_gateway_check=data.disable_gateway_check,
        purpose=data.purpose,
        statistics=SubnetStatistics(
            total=0,
            available=0,
            used=0,
            reserved=reserved,
        ),
        ready=False,
    )


@router.get("/subnets/{name}", response_model=SubnetDetail)
async def get_subnet_detail(request: Request, name: str, user: User = Depends(require_auth)) -> SubnetDetail:
    """Get detailed subnet information including IP leases."""
    k8s = request.app.state.k8s_client
    
    # Get subnet
    try:
        subnet_item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Subnet '{name}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    spec = subnet_item.get("spec", {})
    status = subnet_item.get("status", {})
    
    # Calculate statistics
    available = status.get("v4availableIPs", 0) or status.get("v6availableIPs", 0)
    used = status.get("v4usingIPs", 0) or status.get("v6usingIPs", 0)
    exclude_ips = spec.get("excludeIps", [])
    reserved_count = sum(parse_ip_range(r) for r in exclude_ips)
    
    # Check ready status
    is_ready = status.get("validated", False)
    if not is_ready:
        conditions = status.get("conditions", [])
        is_ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in conditions
        )
    if not is_ready and (available > 0 or used > 0):
        is_ready = True
    
    # Extract namespace from provider format: {nad_name}.{namespace}.ovn
    provider_str = spec.get("provider")
    namespace = None
    if provider_str and provider_str.endswith(".ovn"):
        parts = provider_str.rsplit(".", 2)
        if len(parts) == 3:
            namespace = parts[1]
    
    subnet = SubnetResponse(
        name=subnet_item["metadata"]["name"],
        cidr_block=spec.get("cidrBlock", ""),
        gateway=spec.get("gateway", ""),
        exclude_ips=exclude_ips,
        provider=provider_str,
        vlan=spec.get("vlan"),
        vpc=spec.get("vpc"),
        namespace=namespace,
        protocol=spec.get("protocol", "IPv4"),
        enable_dhcp=spec.get("enableDHCP", True),
        disable_gateway_check=spec.get("disableGatewayCheck", False),
        statistics=SubnetStatistics(
            total=available + used,
            available=available,
            used=used,
            reserved=reserved_count,
        ),
        ready=is_ready,
    )
    
    # Get IP leases for this subnet
    leases = []
    try:
        ips_result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="ips",
        )
        
        for ip_item in ips_result.get("items", []):
            ip_spec = ip_item.get("spec", {})
            if ip_spec.get("subnet") == name:
                pod_name = ip_spec.get("podName", "")
                resource_type, resource_name = extract_vm_name(pod_name)
                
                leases.append(IPLease(
                    ip_address=ip_spec.get("ipAddress", ""),
                    mac_address=ip_spec.get("macAddress"),
                    pod_name=pod_name,
                    namespace=ip_spec.get("namespace"),
                    node_name=ip_spec.get("nodeName"),
                    subnet=name,
                    resource_type=resource_type,
                    resource_name=resource_name or pod_name,
                ))
    except ApiException:
        pass  # IPs CRD might not exist
    
    # Parse reserved IPs
    reserved = []
    for ip_range in exclude_ips:
        reserved.append(ReservedIP(
            ip_or_range=ip_range,
            count=parse_ip_range(ip_range),
            note=None,  # Kube-OVN doesn't store notes
        ))
    
    return SubnetDetail(
        subnet=subnet,
        leases=leases,
        reserved=reserved,
    )


@router.delete("/subnets/{name}")
async def delete_subnet(request: Request, name: str, user: User = Depends(require_auth)) -> dict:
    """Delete a Subnet."""
    k8s = request.app.state.k8s_client
    
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Subnet '{name}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    return {"status": "deleted", "name": name}


@router.post("/subnets/{name}/nad/{namespace}")
async def create_subnet_nad(request: Request, name: str, namespace: str, user: User = Depends(require_auth)) -> dict:
    """Create NetworkAttachmentDefinition for a subnet in a specific namespace.
    
    This allows KubeVirt VMs in that namespace to connect to this Kube-OVN subnet.
    NAD name is derived from the subnet's VLAN name.
    """
    k8s = request.app.state.k8s_client
    
    # Get subnet to determine NAD name (from VLAN)
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Subnet '{name}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    # NAD name = VLAN name, or subnet name if no VLAN
    nad_name = subnet.get("spec", {}).get("vlan", name)
    
    # Verify namespace exists
    try:
        await k8s.core_api.read_namespace(namespace)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Namespace '{namespace}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    # Create NAD with provider matching {nad_name}.{namespace}.ovn
    try:
        await create_nad_for_subnet(k8s, nad_name, namespace)
    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")
    
    provider = get_nad_provider(nad_name, namespace)
    return {"status": "created", "name": nad_name, "namespace": namespace, "provider": provider}


# ============================================================================
# IP Reservation
# ============================================================================

@router.post("/subnets/{name}/reserve")
async def reserve_ip(request: Request, name: str, data: ReserveIPRequest, user: User = Depends(require_auth)) -> dict:
    """Add IP or IP range to subnet's excludeIps."""
    k8s = request.app.state.k8s_client
    
    # Get current subnet
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Subnet '{name}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    # Get current excludeIps
    exclude_ips = subnet.get("spec", {}).get("excludeIps", [])
    
    # Check if already reserved
    if data.ip_or_range in exclude_ips:
        raise HTTPException(status_code=400, detail=f"IP '{data.ip_or_range}' already reserved")
    
    # Add new reservation
    exclude_ips.append(data.ip_or_range)
    
    # Patch subnet
    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
            body={"spec": {"excludeIps": exclude_ips}},
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")
    
    return {
        "status": "reserved",
        "ip_or_range": data.ip_or_range,
        "count": parse_ip_range(data.ip_or_range),
    }


@router.delete("/subnets/{name}/reserve/{ip_or_range:path}")
async def unreserve_ip(request: Request, name: str, ip_or_range: str, user: User = Depends(require_auth)) -> dict:
    """Remove IP or IP range from subnet's excludeIps."""
    k8s = request.app.state.k8s_client
    
    # Get current subnet
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Subnet '{name}' not found")
        raise k8s_error_to_http(e, "network operation")
    
    # Get current excludeIps
    exclude_ips = subnet.get("spec", {}).get("excludeIps", [])
    
    # Check if exists
    if ip_or_range not in exclude_ips:
        raise HTTPException(status_code=404, detail=f"IP '{ip_or_range}' not in reserved list")
    
    # Remove reservation
    exclude_ips.remove(ip_or_range)
    
    # Patch subnet
    try:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            name=name,
            body={"spec": {"excludeIps": exclude_ips}},
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")
    
    return {"status": "unreserved", "ip_or_range": ip_or_range}


# ============================================================================
# Node Network Information
# ============================================================================

@router.get("/nodes", response_model=list[NodeNetworkInfo])
async def get_nodes_network_info(request: Request, user: User = Depends(require_auth)) -> list[NodeNetworkInfo]:
    """Get network-related information from all nodes."""
    k8s = request.app.state.k8s_client
    
    try:
        result = await k8s.core_api.list_node()
    except ApiException as e:
        raise k8s_error_to_http(e, "network operation")
    
    nodes = []
    for node in result.items:
        # Get internal IP
        internal_ip = next(
            (addr.address for addr in (node.status.addresses or []) if addr.type == "InternalIP"),
            None
        )
        
        # Get annotations that might contain network info
        annotations = node.metadata.annotations or {}
        
        # Filter for network-related annotations
        network_annotations = {
            k: v for k, v in annotations.items()
            if any(x in k.lower() for x in ["ovn", "network", "interface", "ip"])
        }
        
        nodes.append(NodeNetworkInfo(
            name=node.metadata.name,
            internal_ip=internal_ip,
            interfaces=[],  # Would need node-level access to get actual interfaces
            annotations=network_annotations,
        ))
    
    return nodes




# ============================================================================
# VPC Helpers
# ============================================================================

def _parse_vpc_response(item: dict[str, Any]) -> VpcResponse:
    """Parse a Kube-OVN Vpc CR into VpcResponse (without subnets/peerings)."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    labels = metadata.get("labels", {})

    conditions = status.get("conditions", [])
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conditions
    )
    # Kube-OVN VPCs may also use standby/default status fields
    if not ready and status.get("standby") is not None:
        ready = status.get("standby", False) or status.get("default", False)

    return VpcResponse(
        name=metadata.get("name", ""),
        tenant=labels.get("kubevirt-ui.io/tenant"),
        enable_nat_gateway=spec.get("enableExternal", False),
        default_subnet=status.get("defaultLogicalSwitch"),
        static_routes=[
            {"cidr": r.get("cidr", ""), "nextHopIP": r.get("nextHopIP", "")}
            for r in spec.get("staticRoutes", [])
        ],
        ready=ready,
        conditions=conditions,
    )


async def _get_vpc_subnets(k8s, vpc_name: str) -> list[VpcSubnetInfo]:
    """Get all subnets belonging to a VPC."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
        )
    except ApiException:
        return []

    subnets = []
    for item in result.get("items", []):
        if item.get("spec", {}).get("vpc") == vpc_name:
            spec = item.get("spec", {})
            st = item.get("status", {})
            subnets.append(VpcSubnetInfo(
                name=item["metadata"]["name"],
                cidr_block=spec.get("cidrBlock", ""),
                gateway=spec.get("gateway", ""),
                available_ips=st.get("v4availableIPs", 0),
                used_ips=st.get("v4usingIPs", 0),
            ))
    return subnets


async def _get_vpc_peerings(k8s, vpc_name: str) -> list[VpcPeeringInfo]:
    """Get peering connections involving a VPC."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpc-peerings",
        )
    except ApiException:
        return []

    peerings = []
    for item in result.get("items", []):
        spec = item.get("spec", {})
        local = spec.get("localVpc", "")
        remote = spec.get("remoteVpc", "")
        if local == vpc_name or remote == vpc_name:
            peerings.append(VpcPeeringInfo(
                name=item["metadata"]["name"],
                local_vpc=local,
                remote_vpc=remote,
            ))
    return peerings


# ============================================================================
# VPC Endpoints
# ============================================================================

@router.get("/vpcs", response_model=list[VpcResponse])
async def list_vpcs(request: Request, tenant: str | None = None, user: User = Depends(require_auth)) -> list[VpcResponse]:
    """List all VPCs, optionally filtered by tenant."""
    k8s = request.app.state.k8s_client

    try:
        kwargs: dict[str, str] = {}
        if tenant:
            kwargs["label_selector"] = f"kubevirt-ui.io/tenant={tenant}"
        else:
            kwargs["label_selector"] = "kubevirt-ui.io/managed=true"

        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            **kwargs,
        )
    except ApiException as e:
        if e.status == 404:
            return []
        raise k8s_error_to_http(e, "network operation")

    vpcs = []
    for item in result.get("items", []):
        vpc = _parse_vpc_response(item)
        vpc.subnets = await _get_vpc_subnets(k8s, vpc.name)
        vpcs.append(vpc)

    return vpcs


@router.post("/vpcs", response_model=VpcResponse, status_code=201)
async def create_vpc(request: Request, data: VpcCreate, user: User = Depends(require_auth)) -> VpcResponse:
    """Create a VPC with a default subnet.

    If subnet_cidr is not provided, auto-allocates a /24 from 10.{200+N}.0.0/24.
    Range starts at 10.200.x to avoid K8s service CIDR (10.96.0.0/12).

    NOTE: VPC pods cannot reach kube-dns or internet by default (isolation).
    TODO: Add VpcDns or static routes for full external connectivity.
    """
    k8s = request.app.state.k8s_client

    # Allocate or use provided CIDR
    if data.subnet_cidr:
        cidr = data.subnet_cidr
        # Derive gateway as .1
        parts = cidr.split("/")[0].rsplit(".", 1)
        gateway = f"{parts[0]}.1"
    else:
        cidr, gateway = await allocate_vpc_cidr(k8s)

    # Determine namespace to bind VPC subnet to
    # Tenant VPCs bind to tenant-{name}, standalone VPCs need explicit namespace
    bind_namespace = f"tenant-{data.tenant}" if data.tenant else None

    # Labels for ownership tracking
    labels: dict[str, str] = {"kubevirt-ui.io/managed": "true"}
    if data.tenant:
        labels["kubevirt-ui.io/tenant"] = data.tenant

    # 1. Create Vpc CR
    vpc_namespaces = [bind_namespace] if bind_namespace else []
    vpc_spec: dict[str, Any] = {
        "namespaces": vpc_namespaces,
    }
    if data.enable_nat_gateway:
        vpc_spec["enableExternal"] = True
    if data.static_routes:
        vpc_spec["staticRoutes"] = [
            {"cidr": r["cidr"], "nextHopIP": r["nextHopIP"], "policy": "policyDst"}
            for r in data.static_routes
        ]

    vpc_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Vpc",
        "metadata": {
            "name": data.name,
            "labels": labels,
        },
        "spec": vpc_spec,
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            body=vpc_manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"VPC '{data.name}' already exists")
        raise k8s_error_to_http(e, "network operation")

    # 2. Create default Subnet in the VPC
    default_subnet_name = f"{data.name}-default"
    subnet_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "Subnet",
        "metadata": {
            "name": default_subnet_name,
            "labels": labels,
        },
        "spec": {
            "protocol": "IPv4",
            "cidrBlock": cidr,
            "gateway": gateway,
            "vpc": data.name,
            "enableDHCP": True,
            "natOutgoing": data.enable_nat_gateway,
            **({"namespaces": [bind_namespace]} if bind_namespace else {}),
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="subnets",
            body=subnet_manifest,
        )
    except ApiException as e:
        # Cleanup VPC if subnet creation fails
        logger.error(f"Failed to create default subnet for VPC {data.name}: {e}")
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_API_GROUP,
                version=KUBEOVN_API_VERSION,
                plural="vpcs",
                name=data.name,
            )
        except ApiException:
            pass
        raise k8s_error_to_http(e, "network operation")

    return VpcResponse(
        name=data.name,
        tenant=data.tenant,
        enable_nat_gateway=data.enable_nat_gateway,
        default_subnet=default_subnet_name,
        subnets=[VpcSubnetInfo(
            name=default_subnet_name,
            cidr_block=cidr,
            gateway=gateway,
        )],
        static_routes=[
            {"cidr": r["cidr"], "nextHopIP": r["nextHopIP"]}
            for r in data.static_routes
        ],
        ready=False,
    )


@router.get("/vpcs/{name}", response_model=VpcResponse)
async def get_vpc(request: Request, name: str, user: User = Depends(require_auth)) -> VpcResponse:
    """Get VPC detail with subnets and peerings."""
    k8s = request.app.state.k8s_client

    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "network operation")

    vpc = _parse_vpc_response(item)
    vpc.subnets = await _get_vpc_subnets(k8s, name)
    vpc.peerings = await _get_vpc_peerings(k8s, name)
    return vpc


@router.delete("/vpcs/{name}")
async def delete_vpc(request: Request, name: str, user: User = Depends(require_auth)) -> dict:
    """Delete a VPC and cascade-delete its subnets and peerings."""
    k8s = request.app.state.k8s_client

    # Delete peerings first
    peerings = await _get_vpc_peerings(k8s, name)
    for p in peerings:
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_API_GROUP,
                version=KUBEOVN_API_VERSION,
                plural="vpc-peerings",
                name=p.name,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete peering {p.name}: {e}")

    # Delete subnets
    subnets = await _get_vpc_subnets(k8s, name)
    for s in subnets:
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_API_GROUP,
                version=KUBEOVN_API_VERSION,
                plural="subnets",
                name=s.name,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete subnet {s.name}: {e}")

    # Delete VPC
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "network operation")

    return {"status": "deleted", "name": name, "subnets_deleted": len(subnets), "peerings_deleted": len(peerings)}


# ============================================================================
# VPC Peering
# ============================================================================

@router.post("/vpcs/{name}/peering", response_model=VpcPeeringInfo, status_code=201)
async def create_vpc_peering(
    request: Request, name: str, data: VpcPeeringCreate,
    user: User = Depends(require_auth),
) -> VpcPeeringInfo:
    """Create a VPC peering connection.

    The path {name} VPC is always the local side. data.remote_vpc is the peer.
    Both VPCs must exist.
    """
    k8s = request.app.state.k8s_client

    # Override local_vpc with the path parameter for consistency
    data.local_vpc = name

    # Validate both VPCs exist
    for vpc_name in [data.local_vpc, data.remote_vpc]:
        try:
            await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_API_GROUP,
                version=KUBEOVN_API_VERSION,
                plural="vpcs",
                name=vpc_name,
            )
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
            raise k8s_error_to_http(e, "network operation")

    peering_name = f"{data.local_vpc}-to-{data.remote_vpc}"
    peering_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_API_GROUP}/{KUBEOVN_API_VERSION}",
        "kind": "VpcPeering",
        "metadata": {
            "name": peering_name,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "localVpc": data.local_vpc,
            "remoteVpc": data.remote_vpc,
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpc-peerings",
            body=peering_manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"Peering '{peering_name}' already exists")
        raise k8s_error_to_http(e, "network operation")

    return VpcPeeringInfo(
        name=peering_name,
        local_vpc=data.local_vpc,
        remote_vpc=data.remote_vpc,
    )
