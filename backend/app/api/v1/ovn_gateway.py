"""OVN Gateway API endpoints.

OVN-native NAT gateway using kube-ovn CRDs: OvnEip, OvnSnatRule, OvnDnatRule, OvnFip.
Alternative to VpcEgressGateway — NAT is handled directly by the OVN logical router,
no extra gateway pods needed.

VPC-centric API: the gateway is identified by vpc_name — one OVN NAT config per VPC.
Endpoints use /ovn-gateways/{vpc_name}/... paths.

Requires:
  - ProviderNetwork + VLAN + infrastructure subnet (e.g. alv111)
  - ENABLE_NAT_GW: true in kube-ovn Helm values (enable-eip-snat=true)
  - Nodes labeled: ovn.kubernetes.io/external-gw=true
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException
from kubernetes_asyncio.stream import WsApiClient

from app.core.auth import User, require_auth
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION, SYSTEM_NAMESPACE
from app.core.errors import k8s_error_to_http, validate_k8s_name
from app.models.ovn_gateway import (
    OvnDnatRuleCreateRequest,
    OvnDnatRuleInfo,
    OvnEipInfo,
    OvnFipCreateRequest,
    OvnFipInfo,
    OvnGatewayCreateRequest,
    OvnGatewayListResponse,
    OvnGatewayResponse,
    OvnSnatRuleCreateRequest,
    OvnSnatRuleInfo,
)

logger = logging.getLogger(__name__)
router = APIRouter()

KUBEOVN_GROUP = KUBEOVN_API_GROUP
KUBEOVN_VERSION = KUBEOVN_API_VERSION

# Labels
MANAGED_LABEL = "kubevirt-ui.io/managed"
OVN_GW_LABEL = "kubevirt-ui.io/ovn-gateway"


# ============================================================================
# Helpers
# ============================================================================

async def _find_kubeovn_ns(k8s) -> str:
    """Find kube-ovn namespace (e.g. o0-kube-ovn)."""
    from app.api.v1.network import _find_kubeovn_namespace
    return await _find_kubeovn_namespace(k8s)


async def _label_nodes_external_gw(k8s, infra_subnet: dict) -> None:
    """Label nodes for external gateway based on infra subnet's VLAN."""
    from app.api.v1.network import _label_nodes_for_external_gw
    spec = infra_subnet.get("spec", {})
    vlan_name = spec.get("vlan", "")
    if vlan_name:
        await _label_nodes_for_external_gw(k8s, vlan_name)


def _gw_tracking_name(vpc_name: str) -> str:
    """ConfigMap name for tracking OVN NAT state per VPC."""
    return f"ovn-gw-{vpc_name}"


def _gateway_labels(vpc_name: str) -> dict[str, str]:
    return {MANAGED_LABEL: "true", OVN_GW_LABEL: vpc_name}


def _parse_eip(item: dict[str, Any]) -> OvnEipInfo:
    """Parse OvnEip CR into response model."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    labels = metadata.get("labels", {})
    return OvnEipInfo(
        name=metadata.get("name", ""),
        v4ip=status.get("v4Ip", spec.get("v4ip", "")),
        type=spec.get("type", "nat"),
        external_subnet=spec.get("externalSubnet", ""),
        ready=status.get("ready", False),
        vpc=labels.get("kubevirt-ui.io/vpc", ""),
    )


def _parse_snat_rule(item: dict[str, Any]) -> OvnSnatRuleInfo:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    return OvnSnatRuleInfo(
        name=metadata.get("name", ""),
        ovn_eip=spec.get("ovnEip", ""),
        v4ip=status.get("v4Ip", ""),
        vpc=status.get("vpc", spec.get("vpc", "")),
        vpc_subnet=spec.get("vpcSubnet", ""),
        internal_cidr=spec.get("internalCIDR", ""),
        ready=status.get("ready", False),
    )


def _parse_dnat_rule(item: dict[str, Any]) -> OvnDnatRuleInfo:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    return OvnDnatRuleInfo(
        name=metadata.get("name", ""),
        ovn_eip=spec.get("ovnEip", ""),
        v4ip=status.get("v4Ip", ""),
        protocol=spec.get("protocol", ""),
        internal_port=spec.get("internalPort", ""),
        external_port=spec.get("externalPort", ""),
        ip_name=spec.get("ipName", ""),
        ready=status.get("ready", False),
    )


def _parse_fip(item: dict[str, Any]) -> OvnFipInfo:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    return OvnFipInfo(
        name=metadata.get("name", ""),
        ovn_eip=spec.get("ovnEip", ""),
        v4ip=status.get("v4Ip", ""),
        ip_name=spec.get("ipName", ""),
        ready=status.get("ready", False),
    )


async def _list_crd_by_label(
    k8s, plural: str, label_selector: str,
) -> list[dict[str, Any]]:
    """List cluster-scoped kube-ovn CRDs by label."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural=plural, label_selector=label_selector,
        )
        return result.get("items", [])
    except ApiException as e:
        if e.status == 404:
            return []
        raise


async def _delete_crd_ignore_404(k8s, plural: str, name: str) -> None:
    """Delete a cluster-scoped kube-ovn CRD, ignoring 404."""
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural=plural, name=name,
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete {plural}/{name}: {e}")


async def _get_gateway_tracking(k8s, vpc_name: str) -> tuple[dict[str, str], str]:
    """Read gateway tracking ConfigMap. Returns (data, resourceVersion)."""
    cm_name = _gw_tracking_name(vpc_name)
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=cm_name, namespace=SYSTEM_NAMESPACE,
        )
        return cm.data or {}, cm.metadata.resource_version
    except ApiException as e:
        if e.status == 404:
            return {}, ""
        raise


async def _save_gateway_tracking(
    k8s, vpc_name: str, data: dict[str, str], resource_version: str,
) -> None:
    """Save gateway tracking ConfigMap with optimistic locking."""
    from kubernetes_asyncio.client import V1ConfigMap, V1ObjectMeta

    cm_name = _gw_tracking_name(vpc_name)
    body = V1ConfigMap(
        metadata=V1ObjectMeta(
            name=cm_name,
            namespace=SYSTEM_NAMESPACE,
            labels=_gateway_labels(vpc_name),
            **({"resource_version": resource_version} if resource_version else {}),
        ),
        data=data,
    )
    if resource_version:
        await k8s.core_api.replace_namespaced_config_map(
            name=cm_name, namespace=SYSTEM_NAMESPACE, body=body,
        )
    else:
        await k8s.core_api.create_namespaced_config_map(
            namespace=SYSTEM_NAMESPACE, body=body,
        )


async def _patch_lsp_nat_options(
    k8s, vpc_name: str, infra_subnet_name: str,
) -> bool:
    """Patch LSP options for NAT on the VPC's external port.

    kube-ovn does NOT auto-set nat-addresses and router-port on custom VPC
    external ports. Without these, GARP is not sent for SNAT EIP and return
    traffic is lost.

    Must run ovn-nbctl inside the ovn-central pod.
    """
    kubeovn_ns = await _find_kubeovn_ns(k8s)
    # Validate names before interpolating into shell command
    # Attack vector blocked: vpc_name='foo; rm -rf /' would fail validation
    validate_k8s_name(vpc_name, "vpc_name")
    validate_k8s_name(infra_subnet_name, "infra_subnet_name")
    lsp_name = f"{infra_subnet_name}-{vpc_name}"
    lrp_name = f"{vpc_name}-{infra_subnet_name}"

    # Find ovn-central pod
    try:
        pods = await k8s.core_api.list_namespaced_pod(
            namespace=kubeovn_ns,
            label_selector="app=ovn-central",
        )
    except ApiException as e:
        logger.error(f"Cannot find ovn-central pods: {e}")
        return False

    if not pods.items:
        logger.error("No ovn-central pods found")
        return False

    pod_name = pods.items[0].metadata.name

    # Use sh -c to avoid WsApiClient issues with '=' in arguments
    command = [
        "sh", "-c",
        f"ovn-nbctl lsp-set-options {lsp_name} nat-addresses=router router-port={lrp_name}",
    ]

    try:
        async with WsApiClient() as ws_api:
            from kubernetes_asyncio import client as k8s_client
            core_v1 = k8s_client.CoreV1Api(ws_api)
            resp = await core_v1.connect_get_namespaced_pod_exec(
                pod_name, kubeovn_ns,
                command=command,
                stderr=True, stdout=True, stdin=False, tty=False,
            )
            logger.info(f"Patched LSP {lsp_name}: nat-addresses=router, router-port={lrp_name}. Output: {resp}")
            return True
    except Exception as e:
        logger.error(f"Failed to patch LSP options via ovn-nbctl: {e}")
        return False


async def _ensure_vpc_external_config(
    k8s, vpc_name: str, infra_subnet_name: str, infra_gateway: str,
) -> None:
    """Patch VPC with enableExternal, extraExternalSubnets, and default route."""
    try:
        vpc = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpcs", name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
        raise

    spec = vpc.get("spec", {})

    # Build patch
    patch: dict[str, Any] = {"spec": {}}

    # enableExternal
    if not spec.get("enableExternal"):
        patch["spec"]["enableExternal"] = True

    # extraExternalSubnets
    existing_ext = spec.get("extraExternalSubnets", [])
    if infra_subnet_name not in existing_ext:
        patch["spec"]["extraExternalSubnets"] = existing_ext + [infra_subnet_name]

    # Static route 0.0.0.0/0
    routes = spec.get("staticRoutes", [])
    has_default = any(r.get("cidr") == "0.0.0.0/0" for r in routes)
    if not has_default:
        routes.append({
            "cidr": "0.0.0.0/0",
            "nextHopIP": infra_gateway,
            "policy": "policyDst",
        })
        patch["spec"]["staticRoutes"] = routes

    if patch["spec"]:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpcs", name=vpc_name, body=patch,
            _content_type="application/merge-patch+json",
        )
        logger.info(f"Patched VPC {vpc_name} for OVN NAT: external={infra_subnet_name}")


async def _remove_vpc_external_config(
    k8s, vpc_name: str, infra_subnet_name: str,
) -> None:
    """Remove OVN NAT configuration from VPC."""
    try:
        vpc = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpcs", name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise

    spec = vpc.get("spec", {})
    patch: dict[str, Any] = {"spec": {}}

    # Remove from extraExternalSubnets
    ext_subnets = spec.get("extraExternalSubnets", [])
    if infra_subnet_name in ext_subnets:
        new_ext = [s for s in ext_subnets if s != infra_subnet_name]
        patch["spec"]["extraExternalSubnets"] = new_ext

    # Remove default route
    routes = spec.get("staticRoutes", [])
    new_routes = [r for r in routes if r.get("cidr") != "0.0.0.0/0"]
    if len(new_routes) != len(routes):
        patch["spec"]["staticRoutes"] = new_routes

    # Disable external if no more external subnets
    if not patch["spec"].get("extraExternalSubnets"):
        patch["spec"]["enableExternal"] = False

    if patch["spec"]:
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="vpcs", name=vpc_name, body=patch,
            _content_type="application/merge-patch+json",
        )


async def _build_gateway_response(k8s, vpc_name: str) -> OvnGatewayResponse:
    """Build full gateway response from tracking CM and CRDs."""
    data, _ = await _get_gateway_tracking(k8s, vpc_name)
    label_sel = f"{OVN_GW_LABEL}={vpc_name}"

    # Get EIP
    eip_items = await _list_crd_by_label(k8s, "ovn-eips", label_sel)
    eip = _parse_eip(eip_items[0]) if eip_items else None

    # Get rules
    snat_items = await _list_crd_by_label(k8s, "ovn-snat-rules", label_sel)
    dnat_items = await _list_crd_by_label(k8s, "ovn-dnat-rules", label_sel)
    fip_items = await _list_crd_by_label(k8s, "ovn-fips", label_sel)

    snat_rules = [_parse_snat_rule(i) for i in snat_items]
    dnat_rules = [_parse_dnat_rule(i) for i in dnat_items]
    fips = [_parse_fip(i) for i in fip_items]

    # Ready = EIP ready + all SNAT rules ready
    ready = bool(eip and eip.ready and all(r.ready for r in snat_rules))

    return OvnGatewayResponse(
        name=vpc_name,
        vpc_name=data.get("vpc_name", vpc_name),
        subnet_name=data.get("subnet_name", ""),
        external_subnet=data.get("external_subnet", ""),
        eip=eip,
        snat_rules=snat_rules,
        dnat_rules=dnat_rules,
        fips=fips,
        lsp_patched=data.get("lsp_patched") == "true",
        ready=ready,
    )


# ============================================================================
# Cleanup
# ============================================================================

async def _cleanup_ovn_gateway(k8s, vpc_name: str) -> None:
    """Cascade delete all OVN NAT resources for a VPC."""
    label_sel = f"{OVN_GW_LABEL}={vpc_name}"

    # 1. DNAT rules
    for item in await _list_crd_by_label(k8s, "ovn-dnat-rules", label_sel):
        await _delete_crd_ignore_404(k8s, "ovn-dnat-rules", item["metadata"]["name"])

    # 2. FIPs
    for item in await _list_crd_by_label(k8s, "ovn-fips", label_sel):
        await _delete_crd_ignore_404(k8s, "ovn-fips", item["metadata"]["name"])

    # 3. SNAT rules
    for item in await _list_crd_by_label(k8s, "ovn-snat-rules", label_sel):
        await _delete_crd_ignore_404(k8s, "ovn-snat-rules", item["metadata"]["name"])

    # 4. EIPs
    for item in await _list_crd_by_label(k8s, "ovn-eips", label_sel):
        await _delete_crd_ignore_404(k8s, "ovn-eips", item["metadata"]["name"])

    # 5. Remove VPC external config
    data, _ = await _get_gateway_tracking(k8s, vpc_name)
    ext_subnet = data.get("external_subnet", "")
    if ext_subnet:
        await _remove_vpc_external_config(k8s, vpc_name, ext_subnet)

    # 6. Delete tracking ConfigMap
    try:
        await k8s.core_api.delete_namespaced_config_map(
            name=_gw_tracking_name(vpc_name), namespace=SYSTEM_NAMESPACE,
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete tracking CM: {e}")

    logger.info(f"Cleaned up OVN gateway for VPC '{vpc_name}'")


# ============================================================================
# REST Endpoints — VPC-centric
# ============================================================================

@router.get("", response_model=OvnGatewayListResponse)
async def list_ovn_gateways(
    request: Request, user: User = Depends(require_auth),
) -> OvnGatewayListResponse:
    """List all VPCs with OVN NAT enabled."""
    k8s = request.app.state.k8s_client

    # Find gateways by tracking ConfigMaps
    try:
        cms = await k8s.core_api.list_namespaced_config_map(
            namespace=SYSTEM_NAMESPACE,
            label_selector=f"{MANAGED_LABEL}=true,{OVN_GW_LABEL}",
        )
    except ApiException as e:
        if e.status == 404:
            return OvnGatewayListResponse(items=[], total=0)
        raise k8s_error_to_http(e, "listing OVN gateways")

    items = []
    for cm in cms.items or []:
        vpc_name = (cm.metadata.labels or {}).get(OVN_GW_LABEL, "")
        if vpc_name:
            items.append(await _build_gateway_response(k8s, vpc_name))

    return OvnGatewayListResponse(items=items, total=len(items))


@router.get("/{vpc_name}", response_model=OvnGatewayResponse)
async def get_ovn_gateway(
    request: Request, vpc_name: str, user: User = Depends(require_auth),
) -> OvnGatewayResponse:
    """Get OVN NAT details for a VPC."""
    k8s = request.app.state.k8s_client

    data, _ = await _get_gateway_tracking(k8s, vpc_name)
    if not data:
        raise HTTPException(status_code=404, detail=f"OVN NAT not enabled for VPC '{vpc_name}'")

    return await _build_gateway_response(k8s, vpc_name)


@router.post("", response_model=OvnGatewayResponse, status_code=201)
async def create_ovn_gateway(
    request: Request, data: OvnGatewayCreateRequest,
    user: User = Depends(require_auth),
) -> OvnGatewayResponse:
    """Enable OVN NAT for a VPC.

    Steps:
      1. Validate infra subnet exists
      2. Patch VPC with enableExternal + extraExternalSubnets + default route
      3. Label nodes for external gateway
      4. Create OvnEip (or reuse shared)
      5. Create OvnSnatRule
      6. Patch LSP options (nat-addresses, router-port) — CRITICAL
      7. Create tracking ConfigMap
    """
    k8s = request.app.state.k8s_client
    vpc_name = data.vpc_name

    # Check not duplicate
    existing, _ = await _get_gateway_tracking(k8s, vpc_name)
    if existing:
        raise HTTPException(status_code=409, detail=f"OVN NAT already enabled for VPC '{vpc_name}'")

    # 1. Validate infra subnet
    try:
        infra = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="subnets", name=data.infra_subnet,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Infrastructure subnet '{data.infra_subnet}' not found",
            )
        raise k8s_error_to_http(e, "reading infrastructure subnet")

    infra_subnet_name = infra["metadata"]["name"]
    infra_gateway = infra.get("spec", {}).get("gateway", "")

    # 2. Patch VPC
    await _ensure_vpc_external_config(k8s, vpc_name, infra_subnet_name, infra_gateway)

    # 3. Label nodes
    await _label_nodes_external_gw(k8s, infra)

    # 4. Create or reuse EIP
    eip_name = data.shared_eip or f"eip-{vpc_name}"
    if not data.shared_eip:
        eip_spec: dict[str, Any] = {
            "externalSubnet": infra_subnet_name,
            "type": "nat",
        }
        if data.eip_address:
            eip_spec["v4ip"] = data.eip_address

        eip_manifest = {
            "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
            "kind": "OvnEip",
            "metadata": {
                "name": eip_name,
                "labels": {
                    **_gateway_labels(vpc_name),
                    "kubevirt-ui.io/vpc": vpc_name,
                },
            },
            "spec": eip_spec,
        }
        try:
            await k8s.custom_api.create_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                plural="ovn-eips", body=eip_manifest,
            )
        except ApiException as e:
            if e.status == 409:
                logger.info(f"OvnEip {eip_name} already exists, reusing")
            else:
                await _remove_vpc_external_config(k8s, vpc_name, infra_subnet_name)
                raise k8s_error_to_http(e, "creating OvnEip")

    # 5. Create SNAT rule
    if data.auto_snat:
        snat_name = f"snat-{vpc_name}"
        snat_manifest = {
            "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
            "kind": "OvnSnatRule",
            "metadata": {
                "name": snat_name,
                "labels": _gateway_labels(vpc_name),
            },
            "spec": {
                "ovnEip": eip_name,
                "vpcSubnet": data.subnet_name,
            },
        }
        try:
            await k8s.custom_api.create_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                plural="ovn-snat-rules", body=snat_manifest,
            )
        except ApiException as e:
            if e.status != 409:
                logger.error(f"Failed to create SNAT rule: {e}")
                if not data.shared_eip:
                    await _delete_crd_ignore_404(k8s, "ovn-eips", eip_name)
                await _remove_vpc_external_config(k8s, vpc_name, infra_subnet_name)
                raise k8s_error_to_http(e, "creating OvnSnatRule")

    # 6. Patch LSP options (with retry — LRP may take a moment to appear)
    lsp_patched = False
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(2)
        lsp_patched = await _patch_lsp_nat_options(k8s, vpc_name, infra_subnet_name)
        if lsp_patched:
            break
    if not lsp_patched:
        logger.warning(
            f"LSP options not patched for VPC {vpc_name}. "
            "SNAT return traffic may not work until patched. "
            f"Retry via POST /ovn-gateways/{vpc_name}/patch-lsp"
        )

    # 7. Create tracking ConfigMap
    tracking_data = {
        "vpc_name": vpc_name,
        "subnet_name": data.subnet_name,
        "external_subnet": infra_subnet_name,
        "eip_name": eip_name,
        "shared_eip": data.shared_eip or "",
        "lsp_patched": str(lsp_patched).lower(),
    }
    await _save_gateway_tracking(k8s, vpc_name, tracking_data, "")

    logger.info(f"Enabled OVN NAT for VPC '{vpc_name}' (eip={eip_name}, infra={infra_subnet_name})")
    return await _build_gateway_response(k8s, vpc_name)


@router.delete("/{vpc_name}")
async def delete_ovn_gateway(
    request: Request, vpc_name: str, user: User = Depends(require_auth),
) -> dict:
    """Disable OVN NAT for a VPC — cascade deletes all NAT rules."""
    k8s = request.app.state.k8s_client

    data, _ = await _get_gateway_tracking(k8s, vpc_name)
    if not data:
        raise HTTPException(status_code=404, detail=f"OVN NAT not enabled for VPC '{vpc_name}'")

    await _cleanup_ovn_gateway(k8s, vpc_name)
    return {"status": "deleted", "vpc_name": vpc_name}


# ============================================================================
# SNAT Rules
# ============================================================================

@router.get("/{vpc_name}/snat-rules", response_model=list[OvnSnatRuleInfo])
async def list_snat_rules(
    request: Request, vpc_name: str, user: User = Depends(require_auth),
) -> list[OvnSnatRuleInfo]:
    """List SNAT rules for a VPC."""
    k8s = request.app.state.k8s_client
    items = await _list_crd_by_label(k8s, "ovn-snat-rules", f"{OVN_GW_LABEL}={vpc_name}")
    return [_parse_snat_rule(i) for i in items]


@router.post("/{vpc_name}/snat-rules", response_model=OvnSnatRuleInfo, status_code=201)
async def create_snat_rule(
    request: Request, vpc_name: str, data: OvnSnatRuleCreateRequest,
    user: User = Depends(require_auth),
) -> OvnSnatRuleInfo:
    """Create an additional SNAT rule for a VPC."""
    k8s = request.app.state.k8s_client

    tracking, _ = await _get_gateway_tracking(k8s, vpc_name)
    if not tracking:
        raise HTTPException(status_code=404, detail=f"OVN NAT not enabled for VPC '{vpc_name}'")

    if not data.vpc_subnet and not data.internal_cidr:
        raise HTTPException(status_code=422, detail="Either vpc_subnet or internal_cidr is required")

    suffix = data.vpc_subnet or data.internal_cidr.replace("/", "-").replace(".", "-")
    rule_name = f"snat-{vpc_name}-{suffix}"

    spec: dict[str, str] = {"ovnEip": data.ovn_eip}
    if data.vpc_subnet:
        spec["vpcSubnet"] = data.vpc_subnet
    else:
        spec["internalCIDR"] = data.internal_cidr

    manifest = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "OvnSnatRule",
        "metadata": {
            "name": rule_name,
            "labels": _gateway_labels(vpc_name),
        },
        "spec": spec,
    }

    try:
        result = await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="ovn-snat-rules", body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"SNAT rule '{rule_name}' already exists")
        raise k8s_error_to_http(e, "creating OvnSnatRule")

    return _parse_snat_rule(result)


@router.delete("/{vpc_name}/snat-rules/{rule_name}")
async def delete_snat_rule(
    request: Request, vpc_name: str, rule_name: str,
    user: User = Depends(require_auth),
) -> dict:
    """Delete a SNAT rule."""
    k8s = request.app.state.k8s_client
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="ovn-snat-rules", name=rule_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"SNAT rule '{rule_name}' not found")
        raise k8s_error_to_http(e, "deleting OvnSnatRule")
    return {"status": "deleted", "rule": rule_name}


# ============================================================================
# DNAT Rules
# ============================================================================

@router.get("/{vpc_name}/dnat-rules", response_model=list[OvnDnatRuleInfo])
async def list_dnat_rules(
    request: Request, vpc_name: str, user: User = Depends(require_auth),
) -> list[OvnDnatRuleInfo]:
    """List DNAT rules for a VPC."""
    k8s = request.app.state.k8s_client
    items = await _list_crd_by_label(k8s, "ovn-dnat-rules", f"{OVN_GW_LABEL}={vpc_name}")
    return [_parse_dnat_rule(i) for i in items]


@router.post("/{vpc_name}/dnat-rules", response_model=OvnDnatRuleInfo, status_code=201)
async def create_dnat_rule(
    request: Request, vpc_name: str, data: OvnDnatRuleCreateRequest,
    user: User = Depends(require_auth),
) -> OvnDnatRuleInfo:
    """Create a DNAT rule (port forwarding) for a VPC."""
    k8s = request.app.state.k8s_client

    tracking, _ = await _get_gateway_tracking(k8s, vpc_name)
    if not tracking:
        raise HTTPException(status_code=404, detail=f"OVN NAT not enabled for VPC '{vpc_name}'")

    rule_name = f"dnat-{vpc_name}-{data.external_port}-{data.protocol}"
    manifest = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "OvnDnatRule",
        "metadata": {
            "name": rule_name,
            "labels": _gateway_labels(vpc_name),
        },
        "spec": {
            "ovnEip": data.ovn_eip,
            "ipName": data.ip_name,
            "protocol": data.protocol,
            "internalPort": data.internal_port,
            "externalPort": data.external_port,
        },
    }

    try:
        result = await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="ovn-dnat-rules", body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"DNAT rule '{rule_name}' already exists")
        raise k8s_error_to_http(e, "creating OvnDnatRule")

    return _parse_dnat_rule(result)


@router.delete("/{vpc_name}/dnat-rules/{rule_name}")
async def delete_dnat_rule(
    request: Request, vpc_name: str, rule_name: str,
    user: User = Depends(require_auth),
) -> dict:
    """Delete a DNAT rule."""
    k8s = request.app.state.k8s_client
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="ovn-dnat-rules", name=rule_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"DNAT rule '{rule_name}' not found")
        raise k8s_error_to_http(e, "deleting OvnDnatRule")
    return {"status": "deleted", "rule": rule_name}


# ============================================================================
# Floating IPs
# ============================================================================

@router.get("/{vpc_name}/fips", response_model=list[OvnFipInfo])
async def list_fips(
    request: Request, vpc_name: str, user: User = Depends(require_auth),
) -> list[OvnFipInfo]:
    """List Floating IPs for a VPC."""
    k8s = request.app.state.k8s_client
    items = await _list_crd_by_label(k8s, "ovn-fips", f"{OVN_GW_LABEL}={vpc_name}")
    return [_parse_fip(i) for i in items]


@router.post("/{vpc_name}/fips", response_model=OvnFipInfo, status_code=201)
async def create_fip(
    request: Request, vpc_name: str, data: OvnFipCreateRequest,
    user: User = Depends(require_auth),
) -> OvnFipInfo:
    """Create a Floating IP (1:1 NAT) for a VPC."""
    k8s = request.app.state.k8s_client

    tracking, _ = await _get_gateway_tracking(k8s, vpc_name)
    if not tracking:
        raise HTTPException(status_code=404, detail=f"OVN NAT not enabled for VPC '{vpc_name}'")

    fip_name = f"fip-{vpc_name}-{data.ip_name}"
    spec: dict[str, str] = {
        "ovnEip": data.ovn_eip,
        "ipName": data.ip_name,
    }
    if data.ip_type:
        spec["ipType"] = data.ip_type

    manifest = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "OvnFip",
        "metadata": {
            "name": fip_name,
            "labels": _gateway_labels(vpc_name),
        },
        "spec": spec,
    }

    try:
        result = await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="ovn-fips", body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"FIP '{fip_name}' already exists")
        raise k8s_error_to_http(e, "creating OvnFip")

    return _parse_fip(result)


@router.delete("/{vpc_name}/fips/{fip_name}")
async def delete_fip(
    request: Request, vpc_name: str, fip_name: str,
    user: User = Depends(require_auth),
) -> dict:
    """Delete a Floating IP."""
    k8s = request.app.state.k8s_client
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            plural="ovn-fips", name=fip_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"FIP '{fip_name}' not found")
        raise k8s_error_to_http(e, "deleting OvnFip")
    return {"status": "deleted", "fip": fip_name}


# ============================================================================
# LSP Patch (retry endpoint)
# ============================================================================

@router.post("/{vpc_name}/patch-lsp")
async def patch_lsp(
    request: Request, vpc_name: str, user: User = Depends(require_auth),
) -> dict:
    """Retry LSP options patching for a VPC's OVN NAT.

    Use this if the initial LSP patch failed (LRP not yet created).
    """
    k8s = request.app.state.k8s_client

    data, rv = await _get_gateway_tracking(k8s, vpc_name)
    if not data:
        raise HTTPException(status_code=404, detail=f"OVN NAT not enabled for VPC '{vpc_name}'")

    ext_subnet = data.get("external_subnet", "")
    if not ext_subnet:
        raise HTTPException(status_code=500, detail="Gateway tracking data incomplete")

    success = await _patch_lsp_nat_options(k8s, vpc_name, ext_subnet)
    if success:
        data["lsp_patched"] = "true"
        await _save_gateway_tracking(k8s, vpc_name, data, rv)
        return {"status": "patched", "lsp": f"{ext_subnet}-{vpc_name}"}
    else:
        raise HTTPException(status_code=500, detail="LSP patch failed — check ovn-central pod logs")
