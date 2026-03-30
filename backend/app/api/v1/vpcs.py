"""VPC Management API endpoints (Kube-OVN).

Dedicated VPC router covering:
  - CRUD for Vpc CRD (kubeovn.io/v1)
  - VPC peering connections
  - Static route management
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.core.allocators import allocate_vpc_cidr
from app.core.auth import User, require_auth
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.core.errors import k8s_error_to_http
from app.models.vpc import (
    VpcCreateRequest,
    VpcListResponse,
    VpcPeeringCreateRequest,
    VpcPeeringInfo,
    VpcResponse,
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

    return VpcResponse(
        name=metadata.get("name", ""),
        tenant=labels.get("kubevirt-ui.io/tenant"),
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


async def _get_vpc_subnets(k8s, vpc_name: str) -> list[VpcSubnetInfo]:
    """Get all subnets belonging to a VPC."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
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
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpc-peerings",
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
# VPC CRUD
# ============================================================================

@router.get("", response_model=VpcListResponse)
async def list_vpcs(request: Request, tenant: str | None = None, user: User = Depends(require_auth)) -> VpcListResponse:
    """List all VPCs, optionally filtered by tenant."""
    k8s = request.app.state.k8s_client

    try:
        kwargs: dict[str, str] = {}
        if tenant:
            kwargs["label_selector"] = f"kubevirt-ui.io/tenant={tenant}"
        else:
            kwargs["label_selector"] = "kubevirt-ui.io/managed=true"

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
        vpc.subnets = await _get_vpc_subnets(k8s, vpc.name)
        vpcs.append(vpc)

    return VpcListResponse(items=vpcs, total=len(vpcs))


@router.post("", response_model=VpcResponse, status_code=201)
async def create_vpc(request: Request, data: VpcCreateRequest, user: User = Depends(require_auth)) -> VpcResponse:
    """Create a VPC with a default subnet.

    If subnet_cidr is not provided, auto-allocates a /24 from 10.{200+N}.0.0/24.
    """
    k8s = request.app.state.k8s_client

    if data.subnet_cidr:
        cidr = data.subnet_cidr
        parts = cidr.split("/")[0].rsplit(".", 1)
        gateway = f"{parts[0]}.1"
    else:
        cidr, gateway = await allocate_vpc_cidr(k8s)

    bind_namespace = f"tenant-{data.tenant}" if data.tenant else None
    labels: dict[str, str] = {"kubevirt-ui.io/managed": "true"}
    if data.tenant:
        labels["kubevirt-ui.io/tenant"] = data.tenant

    # Build VPC spec
    vpc_spec: dict[str, Any] = {
        "namespaces": [bind_namespace] if bind_namespace else [],
    }
    if data.enable_nat_gateway:
        vpc_spec["enableExternal"] = True
    if data.static_routes:
        vpc_spec["staticRoutes"] = [
            {"cidr": r.cidr, "nextHopIP": r.next_hop_ip, "policy": r.policy}
            for r in data.static_routes
        ]

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
    subnet_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "Subnet",
        "metadata": {"name": default_subnet_name, "labels": labels},
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

    # Set up OVN NAT if enabled
    if data.enable_nat_gateway:
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

    return VpcResponse(
        name=data.name,
        tenant=data.tenant,
        enable_nat_gateway=data.enable_nat_gateway,
        default_subnet=default_subnet_name,
        subnets=[VpcSubnetInfo(
            name=default_subnet_name, cidr_block=cidr, gateway=gateway,
        )],
        static_routes=[
            VpcStaticRoute(cidr=r.cidr, nextHopIP=r.next_hop_ip, policy=r.policy)
            for r in data.static_routes
        ],
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
    vpc.subnets = await _get_vpc_subnets(k8s, name)
    vpc.peerings = await _get_vpc_peerings(k8s, name)
    return vpc


@router.delete("/{name}")
async def delete_vpc(request: Request, name: str, user: User = Depends(require_auth)) -> dict:
    """Delete a VPC and cascade-delete its subnets and peerings."""
    k8s = request.app.state.k8s_client

    # Clean up OVN NAT gateway if exists (keyed by vpc_name)
    from app.api.v1.ovn_gateway import _cleanup_ovn_gateway, _get_gateway_tracking
    tracking, _ = await _get_gateway_tracking(k8s, name)
    if tracking:
        try:
            await _cleanup_ovn_gateway(k8s, name)
        except Exception as e:
            logger.warning(f"Failed to cleanup OVN NAT for VPC {name}: {e}")

    # Delete peerings first
    peerings = await _get_vpc_peerings(k8s, name)
    for p in peerings:
        try:
            await k8s.custom_api.delete_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpc-peerings",
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
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                name=s.name,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete subnet {s.name}: {e}")

    # Delete VPC
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{name}' not found")
        raise k8s_error_to_http(e, "VPC operation")

    return {
        "status": "deleted",
        "name": name,
        "subnets_deleted": len(subnets),
        "peerings_deleted": len(peerings),
    }


# ============================================================================
# VPC Peering
# ============================================================================

@router.post("/{name}/peerings", response_model=VpcPeeringInfo, status_code=201)
async def create_vpc_peering(
    request: Request, name: str, data: VpcPeeringCreateRequest,
    user: User = Depends(require_auth),
) -> VpcPeeringInfo:
    """Create a VPC peering connection. Path VPC is the local side."""
    k8s = request.app.state.k8s_client

    # Validate both VPCs exist
    for vpc_name in [name, data.remote_vpc]:
        try:
            await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
                name=vpc_name,
            )
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
            raise k8s_error_to_http(e, "VPC operation")

    peering_name = f"{name}-to-{data.remote_vpc}"
    peering_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "VpcPeering",
        "metadata": {
            "name": peering_name,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "localVpc": name,
            "remoteVpc": data.remote_vpc,
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpc-peerings",
            body=peering_manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"Peering '{peering_name}' already exists")
        raise k8s_error_to_http(e, "VPC operation")

    return VpcPeeringInfo(name=peering_name, local_vpc=name, remote_vpc=data.remote_vpc)


@router.delete("/{name}/peerings/{remote_vpc}")
async def delete_vpc_peering(request: Request, name: str, remote_vpc: str, user: User = Depends(require_auth)) -> dict:
    """Delete a VPC peering connection."""
    k8s = request.app.state.k8s_client

    peering_name = f"{name}-to-{remote_vpc}"
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpc-peerings",
            name=peering_name,
        )
    except ApiException as e:
        if e.status == 404:
            # Try reverse direction
            peering_name = f"{remote_vpc}-to-{name}"
            try:
                await k8s.custom_api.delete_cluster_custom_object(
                    group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpc-peerings",
                    name=peering_name,
                )
            except ApiException as e2:
                if e2.status == 404:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Peering between '{name}' and '{remote_vpc}' not found",
                    )
                raise k8s_error_to_http(e2, "VPC peering cleanup")
        else:
            raise k8s_error_to_http(e, "VPC operation")

    return {"status": "deleted", "peering": peering_name}


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
    user: User = Depends(require_auth),
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
