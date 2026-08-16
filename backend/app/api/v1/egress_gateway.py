"""Egress Gateway API endpoints.

Manages VPC egress gateways using kube-ovn VpcEgressGateway CRD.
Hub-and-spoke architecture: a gateway VPC with macvlan SNAT provides
internet access to tenant VPCs via VPC peering.

Supports flexible topologies:
  - Shared gateway: one gateway for all/multiple VPCs
  - Per-VPC gateway: dedicated gateway per tenant
  - Groups: gateway A for VPC 1,2,3 — gateway B for VPC 4,5
"""

import asyncio
import ipaddress
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException, V1ConfigMap, V1ObjectMeta

from app.core.auth import User, require_auth, require_admin
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION, SYSTEM_NAMESPACE as _SYSTEM_NS
from app.core.errors import k8s_error_to_http
from app.core.allocators import find_cidr_conflicts, list_subnet_cidrs
from app.core.vpc_peering import (
    ensure_vpc_peering as _ensure_vpc_peering,
    remove_vpc_peering as _remove_vpc_peering,
    transit_prefix_len as _transit_prefix_len,
)
from app.models.egress_gateway import (
    AttachedVpcInfo,
    AttachTenantRequest,
    DetachTenantRequest,
    EgressGatewayCreateRequest,
    EgressGatewayListResponse,
    EgressGatewayResponse,
    GatewayPodInfo,
)

logger = logging.getLogger(__name__)
router = APIRouter()

KUBEOVN_GROUP = KUBEOVN_API_GROUP
KUBEOVN_VERSION = KUBEOVN_API_VERSION
SYSTEM_NAMESPACE = _SYSTEM_NS

# Label used on all managed resources
MANAGED_LABEL = "kubevirt-ui.io/managed"
GATEWAY_LABEL = "kubevirt-ui.io/egress-gateway"


# ============================================================================
# Transit IP Allocator (ConfigMap-based)
# ============================================================================

def _transit_cm_name(gateway_name: str) -> str:
    """ConfigMap name for tracking transit IP allocations per gateway."""
    return f"egress-transit-{gateway_name}"


async def _get_transit_allocator(k8s, gateway_name: str) -> tuple[dict[str, str], str]:
    """Read transit IP allocator ConfigMap. Returns (data_dict, resourceVersion)."""
    cm_name = _transit_cm_name(gateway_name)
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=cm_name, namespace=SYSTEM_NAMESPACE,
        )
        return cm.data or {}, cm.metadata.resource_version
    except ApiException as e:
        if e.status == 404:
            return {}, ""
        raise


async def _save_transit_allocator(
    k8s, gateway_name: str, data: dict[str, str], resource_version: str,
) -> None:
    """Save transit IP allocator ConfigMap with optimistic locking."""
    cm_name = _transit_cm_name(gateway_name)
    body = V1ConfigMap(
        metadata=V1ObjectMeta(
            name=cm_name,
            namespace=SYSTEM_NAMESPACE,
            labels={MANAGED_LABEL: "true", GATEWAY_LABEL: gateway_name},
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


def _allocate_transit_ip(transit_cidr: str, used_ips: set[str]) -> str:
    """Allocate next available IP from transit CIDR, skipping network/gateway/broadcast."""
    network = ipaddress.IPv4Network(transit_cidr, strict=False)
    # Skip .0 (network), .1 (gateway reserved for gateway VPC), last (broadcast)
    for host in network.hosts():
        ip_str = str(host)
        if ip_str not in used_ips and host != network.network_address + 1:
            return ip_str
    raise HTTPException(status_code=409, detail=f"Transit CIDR {transit_cidr} exhausted")


def _gateway_transit_ip(transit_cidr: str) -> str:
    """Gateway always gets .1 in the transit subnet."""
    network = ipaddress.IPv4Network(transit_cidr, strict=False)
    return str(network.network_address + 1)


# ============================================================================
# Helpers
# ============================================================================

async def _get_gateway_config(k8s, gateway_name: str) -> dict[str, Any] | None:
    """Read the gateway's VPC and extract config from labels/annotations."""
    gw_vpc_name = f"egw-{gateway_name}"
    try:
        vpc = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=gw_vpc_name,
        )
        return vpc
    except ApiException as e:
        if e.status == 404:
            return None
        raise


async def _get_vpc_egress_gateway(k8s, gateway_name: str) -> dict[str, Any] | None:
    """Read the VpcEgressGateway CR."""
    try:
        return await k8s.custom_api.get_namespaced_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            namespace="kube-system", plural="vpc-egress-gateways",
            name=gateway_name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _cidrs_overlap(cidr1: str, cidr2: str) -> bool:
    """Check if two CIDR ranges overlap."""
    n1 = ipaddress.ip_network(cidr1, strict=False)
    n2 = ipaddress.ip_network(cidr2, strict=False)
    return n1.overlaps(n2)


async def _validate_cidr_no_overlap(k8s, gw_vpc_cidr: str, transit_cidr: str) -> None:
    """Validate that gateway CIDRs don't overlap with each other or existing VPCs."""
    # Check gateway and transit CIDRs don't overlap with each other
    if _cidrs_overlap(gw_vpc_cidr, transit_cidr):
        raise HTTPException(
            status_code=422,
            detail=f"Gateway VPC CIDR ({gw_vpc_cidr}) and transit CIDR ({transit_cidr}) overlap",
        )

    # Collect existing CIDRs from all VPCs
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
        )
    except ApiException:
        return  # If we can't list subnets, skip overlap check

    for subnet in result.get("items", []):
        existing_cidr = subnet.get("spec", {}).get("cidrBlock", "")
        if not existing_cidr:
            continue
        subnet_name = subnet.get("metadata", {}).get("name", "")
        if _cidrs_overlap(gw_vpc_cidr, existing_cidr):
            raise HTTPException(
                status_code=422,
                detail=f"Gateway VPC CIDR ({gw_vpc_cidr}) overlaps with existing subnet '{subnet_name}' ({existing_cidr})",
            )
        if _cidrs_overlap(transit_cidr, existing_cidr):
            raise HTTPException(
                status_code=422,
                detail=f"Transit CIDR ({transit_cidr}) overlaps with existing subnet '{subnet_name}' ({existing_cidr})",
            )


def _expand_excluded(entries: list[str]) -> set[str]:
    """kube-ovn's `excludeIps` holds both single addresses and `a..b` ranges."""
    out: set[str] = set()
    for entry in entries or []:
        entry = str(entry).strip()
        if ".." in entry:
            start, _, end = entry.partition("..")
            try:
                first = ipaddress.IPv4Address(start.strip())
                last = ipaddress.IPv4Address(end.strip())
            except ValueError:
                continue
            if int(last) - int(first) > 4096:
                continue  # absurdly wide; do not materialise it
            out.update(str(ipaddress.IPv4Address(i)) for i in range(int(first), int(last) + 1))
        elif entry:
            out.add(entry)
    return out


async def _addresses_pinned_elsewhere(k8s) -> set[str]:
    """Every address already written into some VpcEgressGateway's spec."""
    try:
        result = await k8s.custom_api.list_namespaced_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            namespace="kube-system", plural="vpc-egress-gateways",
        )
    except ApiException:
        return set()

    taken: set[str] = set()
    for item in result.get("items", []):
        spec = item.get("spec", {}) or {}
        for key in ("internalIPs", "externalIPs"):
            taken.update(str(ip).split("/")[0] for ip in (spec.get(key) or []))
    return taken


async def _free_addresses(k8s, subnet_name: str, count: int, taken: set[str]) -> list[str]:
    """`count` addresses from `subnet_name` that nothing else is using."""
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets", name=subnet_name,
        )
    except ApiException:
        return []

    spec = subnet.get("spec", {}) or {}
    cidr = spec.get("cidrBlock", "")
    if not cidr:
        return []

    blocked = set(taken)
    blocked.update(_expand_excluded(spec.get("excludeIps") or []))
    gateway = spec.get("gateway", "")
    if gateway:
        blocked.add(gateway)

    network = ipaddress.IPv4Network(cidr, strict=False)
    out: list[str] = []
    for host in network.hosts():
        ip = str(host)
        if ip in blocked:
            continue
        out.append(ip)
        if len(out) == count:
            break
    return out


async def allocate_gateway_ips(
    k8s, internal_subnet: str, external_subnet: str, replicas: int,
) -> tuple[list[str], list[str]]:
    """Pin one internal and one external address per replica.

    Left unpinned, kube-ovn hands every replacement pod a fresh address. The
    routing survives that (BIRD withdraws the old route and rebuilds ECMP), but
    the border router accumulates one dead dynamic BGP session per burned
    address, and anything keyed on the gateway's address has to be rewritten by
    hand each time a pod restarts.

    Allocation avoids the subnet gateway, the subnet's `excludeIps` and every
    address already pinned by another gateway. Returns empty lists when the
    subnets cannot be read — the caller then leaves the fields off the spec and
    kube-ovn behaves exactly as before, which is a worse default but not a
    failure.
    """
    taken = await _addresses_pinned_elsewhere(k8s)

    internal = await _free_addresses(k8s, internal_subnet, replicas, taken)
    taken.update(internal)
    external = await _free_addresses(k8s, external_subnet, replicas, taken)

    return internal, external


# Where a gateway's own networks are carved from. Deliberately a /16 of its
# own: the gateway VPC and its transit link are infrastructure, not tenant
# space, and must not be taken out of the tenant supernet.
GATEWAY_CIDR_POOL = "10.199.128.0/17"


async def suggest_gateway_cidrs(k8s, pool: str | None = None) -> dict[str, str]:
    """A gateway VPC CIDR and a transit CIDR the cluster is not already using.

    The form's default used to be the constant `10.199.0.0/24`, which on a
    deployment whose control-plane transit is `10.199.0.0/22` overlaps it —
    and the overlap check then reported "No CIDR overlaps detected" for the
    pair it had just proposed.

    This is the allocator's own walk without its write: nothing is persisted,
    so a suggestion costs nothing and does not burn a range per page load.
    Both /24s are checked against every subnet in the cluster, including the
    ones we did not create, and against each other.

    Returns empty strings when the pool has no room left — an empty field the
    operator must fill is better than a suggestion that collides.
    """
    network = ipaddress.ip_network(pool or GATEWAY_CIDR_POOL, strict=False)
    existing = await list_subnet_cidrs(k8s)

    free: list[str] = []
    for candidate in network.subnets(new_prefix=24):
        cidr = str(candidate)
        if find_cidr_conflicts(cidr, existing):
            continue
        free.append(cidr)
        if len(free) == 2:
            return {"gw_vpc_cidr": free[0], "transit_cidr": free[1]}

    logger.warning("No free /24 pair for a gateway in %s", network)
    return {"gw_vpc_cidr": "", "transit_cidr": ""}


@router.get("/suggest-cidrs")
async def get_suggested_cidrs(
    request: Request, user: User = Depends(require_auth),
) -> dict[str, str]:
    """Free CIDRs for the create-gateway form to start from."""
    return await suggest_gateway_cidrs(request.app.state.k8s_client)


async def _require_external_subnet(k8s) -> None:
    """kube-ovn wants a Subnet named literally `external` before EIP/SNAT works.

    With `enable-eip-snat` on, kube-ovn refuses to program NAT without it:

        enable-eip-snat need external subnet external to be exist

    and the gateway then fails with a message about BFD, which sends you
    chasing the wrong thing entirely — measured on the lab, where the fix was
    to rebuild the egress underlay under that exact name.
    """
    try:
        await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets", name="external",
        )
    except ApiException as e:
        if e.status != 404:
            return  # cannot check — do not block on our own inability to look
        raise HTTPException(
            status_code=422,
            detail=(
                "kube-ovn requires a Subnet named exactly 'external' before it will "
                "program EIP/SNAT (`enable-eip-snat need external subnet external to "
                "be exist`). Build the egress underlay with that name — without it "
                "the gateway fails with an unrelated-looking BFD error."
            ),
        )


async def adopt_gateway_pins(k8s, gateway_name: str, veg: dict[str, Any] | None) -> bool:
    """Write a gateway's current addresses into its spec if nothing pins them.

    Pinning at creation only protects gateways created after that change. One
    already on the cluster keeps churning: measured while verifying this work,
    an unpinned gateway had its pods replaced, took `10.199.16.5/.7`, and left
    the hub's default route pointing at `10.199.16.3` — an address that no
    longer existed. VpcEgressGateway Ready, BGP Established, peering intact,
    and every packet the tenant sent going nowhere.

    Adopting on read closes that window without asking anyone to notice first.
    An existing pin is never overwritten — that would turn a pin into a record
    of whatever the pods last drifted to, which is the opposite of the point.

    Returns True when something was written.
    """
    spec = (veg or {}).get("spec", {}) or {}
    status = (veg or {}).get("status", {}) or {}

    # Only adopt a status that is fully populated. Clearing the pins makes
    # kube-ovn replace pods, and for a few seconds `status` holds one address
    # for a two-replica gateway — measured live. Pinning that would leave the
    # second replica with nowhere to come up, which is worse than the churn
    # this is meant to stop.
    replicas = spec.get("replicas") or 1

    patch: dict[str, list[str]] = {}
    for field in ("internalIPs", "externalIPs"):
        if spec.get(field):
            continue
        current = [str(ip).split("/")[0] for ip in (status.get(field) or [])]
        if current and len(current) >= replicas:
            patch[field] = current

    if not patch:
        return False

    try:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            namespace="kube-system", plural="vpc-egress-gateways",
            name=gateway_name, body={"spec": patch},
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        logger.warning("Could not adopt address pins for %r: %s", gateway_name, e)
        return False

    logger.info("Gateway %r: adopted its current addresses as pins (%s)", gateway_name, patch)
    return True


async def _get_gateway_pod_ips(k8s, gateway_name: str) -> list[GatewayPodInfo]:
    """Get assigned IPs from egress gateway pods."""
    try:
        pods = await k8s.core_api.list_namespaced_pod(
            namespace="kube-system",
            label_selector=f"app=vpc-egress-gateway,kubevirt-ui.io/egress-gateway={gateway_name}",
        )
    except ApiException:
        return []

    result = []
    for pod in pods.items or []:
        annotations = pod.metadata.annotations or {}
        result.append(GatewayPodInfo(
            pod=pod.metadata.name,
            node=pod.spec.node_name or "",
            internal_ip=annotations.get("ovn.kubernetes.io/ip_address", ""),
            external_ip=annotations.get("ovn.kubernetes.io/provider_network_ip", ""),
        ))
    return result


def _current_conditions(veg_status: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop conditions that a later successful reconcile has already answered.

    kube-ovn does not clear a condition when the next generation succeeds, so a
    gateway that failed validation once and then came up keeps both
    `Validated=False` and `Ready=True` forever. Handing the raw list to
    consumers invites them to draw the stale failure next to a healthy gateway.

    Anything stamped at or after the newest `Ready=True` is current and stays.
    """
    conditions = list(veg_status.get("conditions") or [])
    ready_times = [
        c.get("lastTransitionTime", "")
        for c in conditions
        if c.get("type") == "Ready" and c.get("status") == "True"
    ]
    if not ready_times:
        return conditions

    newest_ready = max(ready_times)
    return [
        c for c in conditions
        if c.get("status") != "False" or c.get("lastTransitionTime", "") >= newest_ready
    ]


def _parse_gateway(
    vpc: dict[str, Any],
    veg: dict[str, Any] | None,
    attached: list[AttachedVpcInfo],
    assigned_ips: list[GatewayPodInfo] | None = None,
    degraded_reason: str | None = None,
) -> EgressGatewayResponse:
    """Parse gateway VPC + VpcEgressGateway into response model."""
    metadata = vpc.get("metadata", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    spec = vpc.get("spec", {})
    status = vpc.get("status", {})

    name = labels.get(GATEWAY_LABEL, metadata.get("name", "").removeprefix("egw-"))

    veg_spec = veg.get("spec", {}) if veg else {}
    veg_status = veg.get("status", {}) if veg else {}

    node_selector = {}
    if veg_spec.get("nodeSelector"):
        for sel in veg_spec["nodeSelector"]:
            match_labels = sel.get("matchLabels", {})
            node_selector.update(match_labels)

    # Ready comes from VpcEgressGateway status, not VPC status
    ready = veg_status.get("ready", False) if veg_status else False

    if veg_status:
        veg_status = {**veg_status, "conditions": _current_conditions(veg_status)}

    exclude_ips_str = annotations.get("kubevirt-ui.io/exclude-ips", "")
    exclude_ips = [ip.strip() for ip in exclude_ips_str.split(",") if ip.strip()] if exclude_ips_str else []

    return EgressGatewayResponse(
        name=name,
        gw_vpc_name=metadata.get("name", ""),
        gw_vpc_cidr=annotations.get("kubevirt-ui.io/gw-vpc-cidr", ""),
        transit_cidr=annotations.get("kubevirt-ui.io/transit-cidr", ""),
        macvlan_subnet=annotations.get("kubevirt-ui.io/macvlan-subnet", ""),
        replicas=veg_spec.get("replicas", 0),
        bfd_enabled=veg_spec.get("bfd", {}).get("enabled", False),
        node_selector=node_selector,
        exclude_ips=exclude_ips,
        attached_vpcs=attached,
        assigned_ips=assigned_ips or [],
        bgp_conf=veg_spec.get("bgpConf", "") or "",
        internal_ips=list(veg_status.get("internalIPs") or []),
        external_ips=list(veg_status.get("externalIPs") or []),
        # A gateway whose tenant cannot reach it is not Ready, whatever the
        # VpcEgressGateway status says about the gateway pods.
        ready=ready and degraded_reason is None,
        degraded_reason=degraded_reason,
        status=veg_status if veg_status else None,
    )


async def _verify_and_heal_peerings(
    k8s, gw_vpc_name: str, transit_cidr: str, attached: list[AttachedVpcInfo],
) -> str | None:
    """Check every attached tenant still declares its half of the peering.

    kube-ovn builds the link only when both routers declare it, and
    `spec.vpcPeerings` is a plain list on a shared object — another flow, a
    GitOps reconcile or a `kubectl patch --type merge` with a one-element array
    all replace it wholesale and take our entry with them. The tenant is then
    left with a default route to a next hop it has no interface for, which
    drops all of its egress while every status field stays green.

    So: re-assert the invariant on read, repair what is missing, and return a
    reason string when something was broken (the caller reports Degraded rather
    than Ready). Repair is safe — `ensure_vpc_peering` upserts under
    compare-and-set and leaves unrelated peerings alone.
    """
    broken: list[str] = []

    for vpc in attached:
        item = await _get_vpc(k8s, vpc.vpc_name)
        if item is None:
            continue
        peerings = (item.get("spec", {}) or {}).get("vpcPeerings") or []
        if any(p.get("remoteVpc") == gw_vpc_name for p in peerings):
            continue

        vpc.peering_ok = False
        broken.append(vpc.vpc_name)
        logger.warning(
            "Tenant VPC %r lost its peering to gateway VPC %r; restoring it",
            vpc.vpc_name, gw_vpc_name,
        )
        if vpc.transit_ip and transit_cidr:
            prefix = _transit_prefix_len(transit_cidr)
            try:
                await _ensure_vpc_peering(
                    k8s, vpc.vpc_name, gw_vpc_name, f"{vpc.transit_ip}/{prefix}",
                )
            except ApiException as e:
                logger.error("Could not restore peering for %r: %s", vpc.vpc_name, e)

    if not broken:
        return None
    return (
        "tenant side peering missing for "
        + ", ".join(broken)
        + " (restored; re-check in a moment)"
    )


async def _get_vpc(k8s, name: str) -> dict[str, Any] | None:
    try:
        return await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


async def _list_attached_vpcs(k8s, gateway_name: str) -> list[AttachedVpcInfo]:
    """List tenant VPCs attached to a gateway by reading transit allocator."""
    data, _ = await _get_transit_allocator(k8s, gateway_name)
    attached = []
    for key, value in data.items():
        if key.startswith("vpc."):
            vpc_name = key.removeprefix("vpc.")
            parts = value.split(",")  # transit_ip,subnet_name,cidr
            attached.append(AttachedVpcInfo(
                vpc_name=vpc_name,
                transit_ip=parts[0] if len(parts) > 0 else "",
                subnet_name=parts[1] if len(parts) > 1 else "",
                cidr=parts[2] if len(parts) > 2 else "",
                peering_name=f"{gateway_name}-to-{vpc_name}",
            ))
    return attached


# ============================================================================
# REST Endpoints
# ============================================================================

@router.get("", response_model=EgressGatewayListResponse)
async def list_egress_gateways(request: Request, user: User = Depends(require_auth)) -> EgressGatewayListResponse:
    """List all egress gateways with statuses."""
    k8s = request.app.state.k8s_client

    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            label_selector=f"{GATEWAY_LABEL}",
        )
    except ApiException as e:
        if e.status == 404:
            return EgressGatewayListResponse(items=[], total=0)
        raise k8s_error_to_http(e, "listing egress gateways")

    items = []
    for vpc in result.get("items", []):
        gw_name = vpc.get("metadata", {}).get("labels", {}).get(GATEWAY_LABEL, "")
        veg = await _get_vpc_egress_gateway(k8s, gw_name)
        if await adopt_gateway_pins(k8s, gw_name, veg):
            veg = await _get_vpc_egress_gateway(k8s, gw_name)
        attached = await _list_attached_vpcs(k8s, gw_name)
        pod_ips = await _get_gateway_pod_ips(k8s, gw_name)
        annotations = vpc.get("metadata", {}).get("annotations", {})
        degraded = await _verify_and_heal_peerings(
            k8s, vpc.get("metadata", {}).get("name", ""),
            annotations.get("kubevirt-ui.io/transit-cidr", ""), attached,
        )
        items.append(_parse_gateway(vpc, veg, attached, pod_ips, degraded))

    return EgressGatewayListResponse(items=items, total=len(items))


@router.get("/{name}", response_model=EgressGatewayResponse)
async def get_egress_gateway(request: Request, name: str, user: User = Depends(require_auth)) -> EgressGatewayResponse:
    """Get egress gateway details including attached VPCs."""
    k8s = request.app.state.k8s_client

    vpc = await _get_gateway_config(k8s, name)
    if not vpc:
        raise HTTPException(status_code=404, detail=f"Egress gateway '{name}' not found")

    veg = await _get_vpc_egress_gateway(k8s, name)
    if await adopt_gateway_pins(k8s, name, veg):
        veg = await _get_vpc_egress_gateway(k8s, name)
    attached = await _list_attached_vpcs(k8s, name)
    pod_ips = await _get_gateway_pod_ips(k8s, name)
    annotations = vpc.get("metadata", {}).get("annotations", {})
    degraded = await _verify_and_heal_peerings(
        k8s, vpc.get("metadata", {}).get("name", ""),
        annotations.get("kubevirt-ui.io/transit-cidr", ""), attached,
    )
    return _parse_gateway(vpc, veg, attached, pod_ips, degraded)


@router.post("", response_model=EgressGatewayResponse, status_code=201)
async def create_egress_gateway(
    request: Request, data: EgressGatewayCreateRequest,
    user: User = Depends(require_admin),
) -> EgressGatewayResponse:
    """Create an egress gateway (VPC + subnet + VpcEgressGateway)."""
    k8s = request.app.state.k8s_client

    # Determine external subnet: existing or create new
    create_external = bool(data.external_interface and data.external_cidr and data.external_gateway)
    if not data.macvlan_subnet and not create_external:
        raise HTTPException(
            status_code=422,
            detail="Either macvlan_subnet (existing) or external_interface + external_cidr + external_gateway (create new) must be provided",
        )
    if data.macvlan_subnet and create_external:
        raise HTTPException(
            status_code=422,
            detail="Provide either macvlan_subnet OR external_* fields, not both",
        )

    # Validate existing subnet
    if data.macvlan_subnet:
        if '/' in data.macvlan_subnet:
            raise HTTPException(
                status_code=422,
                detail=f"macvlan_subnet must be a subnet name (e.g. 'alv111'), not a CIDR ('{data.macvlan_subnet}')",
            )
        try:
            await k8s.custom_api.get_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                name=data.macvlan_subnet,
            )
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"Macvlan subnet '{data.macvlan_subnet}' not found.",
                )
            raise k8s_error_to_http(e, "validating macvlan subnet")

    # Name for the external subnet (existing or to-be-created)
    external_subnet_name = data.macvlan_subnet or f"egw-{data.name}-external"

    await _require_external_subnet(k8s)

    # Validate CIDRs don't overlap with each other or existing subnets
    await _validate_cidr_no_overlap(k8s, data.gw_vpc_cidr, data.transit_cidr)

    gw_vpc_name = f"egw-{data.name}"
    gw_subnet_name = f"egw-{data.name}-subnet"

    # Parse CIDRs
    gw_network = ipaddress.IPv4Network(data.gw_vpc_cidr, strict=False)
    gw_gateway = str(gw_network.network_address + 1)
    transit_network = ipaddress.IPv4Network(data.transit_cidr, strict=False)
    transit_gateway = str(transit_network.network_address + 1)

    labels: dict[str, str] = {
        MANAGED_LABEL: "true",
        GATEWAY_LABEL: data.name,
    }
    annotations: dict[str, str] = {
        "kubevirt-ui.io/gw-vpc-cidr": data.gw_vpc_cidr,
        "kubevirt-ui.io/transit-cidr": data.transit_cidr,
        "kubevirt-ui.io/macvlan-subnet": external_subnet_name,
    }
    if data.exclude_ips:
        annotations["kubevirt-ui.io/exclude-ips"] = ",".join(data.exclude_ips)
    if create_external:
        annotations["kubevirt-ui.io/external-interface"] = data.external_interface  # type: ignore[assignment]
        annotations["kubevirt-ui.io/external-cidr"] = data.external_cidr  # type: ignore[assignment]
        annotations["kubevirt-ui.io/external-gateway"] = data.external_gateway  # type: ignore[assignment]

    # 1. Create gateway VPC
    vpc_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "Vpc",
        "metadata": {
            "name": gw_vpc_name,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "enableExternal": True,
            **({"bfdPort": {"enabled": True, "ip": str(transit_network.network_address + 254)}} if data.bfd_enabled else {}),
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            body=vpc_manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail=f"Egress gateway '{data.name}' already exists")
        raise k8s_error_to_http(e, "creating egress gateway VPC")

    # 2. Create internal gateway subnet
    gw_subnet_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "Subnet",
        "metadata": {"name": gw_subnet_name, "labels": labels},
        "spec": {
            "protocol": "IPv4",
            "cidrBlock": data.gw_vpc_cidr,
            "gateway": gw_gateway,
            "vpc": gw_vpc_name,
            "enableDHCP": True,
            "natOutgoing": False,
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            body=gw_subnet_manifest,
        )
    except ApiException as e:
        logger.error(f"Failed to create gateway subnet: {e}")
        await _cleanup_gateway_vpc(k8s, gw_vpc_name)
        raise k8s_error_to_http(e, "creating gateway subnet")

    # 3. No Subnet is created for the transit CIDR — deliberately.
    #
    # A VPC peering is a router-to-router link: each side gets an address out
    # of the transit CIDR on its peering port, and no logical switch is
    # involved. Creating a Subnet for that same CIDR attaches a second port to
    # the gateway router carrying the *same* address as the peering port
    # (both take .1), and a router with one address on two ports silently
    # stops forwarding — a tenant could not even ping the far end of its own
    # link. The CIDR stays reserved through the gateway VPC's annotation and
    # the overlap check in `_validate_cidr_no_overlap`.

    # 4. Create external macvlan NAD + Subnet (if not using existing)
    if create_external:
        import json
        nad_name = f"egw-{data.name}-macvlan"
        nad_namespace = "kube-system"
        provider = f"{nad_name}.{nad_namespace}"

        nad_config = {
            "cniVersion": "0.3.0",
            "type": "macvlan",
            "master": data.external_interface,
            "mode": "bridge",
            "ipam": {
                "type": "kube-ovn",
                "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
                "provider": provider,
            },
        }
        nad_manifest = {
            "apiVersion": "k8s.cni.cncf.io/v1",
            "kind": "NetworkAttachmentDefinition",
            "metadata": {
                "name": nad_name,
                "namespace": nad_namespace,
                "labels": labels,
            },
            "spec": {"config": json.dumps(nad_config)},
        }
        try:
            await k8s.custom_api.create_namespaced_custom_object(
                group="k8s.cni.cncf.io", version="v1",
                namespace=nad_namespace, plural="network-attachment-definitions",
                body=nad_manifest,
            )
        except ApiException as e:
            if e.status != 409:
                logger.error(f"Failed to create macvlan NAD: {e}")
                await _cleanup_gateway_resources(k8s, data.name)
                raise k8s_error_to_http(e, "creating macvlan NAD")

        ext_subnet_manifest: dict[str, Any] = {
            "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
            "kind": "Subnet",
            "metadata": {"name": external_subnet_name, "labels": labels},
            "spec": {
                "protocol": "IPv4",
                "provider": provider,
                "cidrBlock": data.external_cidr,
                "gateway": data.external_gateway,
                "excludeIps": data.exclude_ips if data.exclude_ips else [],
                "enableDHCP": False,
            },
        }
        try:
            await k8s.custom_api.create_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                body=ext_subnet_manifest,
            )
        except ApiException as e:
            logger.error(f"Failed to create external subnet: {e}")
            await _cleanup_gateway_resources(k8s, data.name)
            raise k8s_error_to_http(e, "creating external subnet")

    # 5. Create VpcEgressGateway (in kube-system namespace)
    veg_spec: dict[str, Any] = {
        "vpc": gw_vpc_name,
        "replicas": data.replicas,
        "prefix": data.name,
        "internalSubnet": gw_subnet_name,
        "externalSubnet": external_subnet_name,
        # These are the SOURCES whose egress goes through the gateway, not
        # destinations — a 0.0.0.0/0 here would also match return traffic and
        # bounce replies off the gateway.
        "policies": [{"snat": data.snat, "subnets": [gw_subnet_name]}],
        "bfd": {"enabled": data.bfd_enabled},
    }
    # Pin the addresses. Unpinned, every replaced pod takes a new one, which
    # leaves a dead dynamic BGP session behind on the border router and breaks
    # anything keyed on the gateway's address. An explicit request wins; we
    # allocate the rest.
    auto_internal, auto_external = await allocate_gateway_ips(
        k8s, gw_subnet_name, external_subnet_name, data.replicas,
    )
    if data.external_ips:
        veg_spec["externalIPs"] = data.external_ips
    elif auto_external:
        veg_spec["externalIPs"] = auto_external
    if auto_internal:
        veg_spec["internalIPs"] = auto_internal
    if data.bgp_conf:
        # Makes the gateway run FRR and announce the VPC's subnets. Without
        # it the VPC is NAT-only no matter what the upstream router is told.
        veg_spec["bgpConf"] = data.bgp_conf
    if data.node_selector:
        veg_spec["nodeSelector"] = [{"matchLabels": data.node_selector}]

    veg_manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "VpcEgressGateway",
        "metadata": {
            "name": data.name,
            "namespace": "kube-system",
            "labels": labels,
        },
        "spec": veg_spec,
    }

    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            namespace="kube-system", plural="vpc-egress-gateways",
            body=veg_manifest,
        )
    except ApiException as e:
        logger.error(f"Failed to create VpcEgressGateway: {e}")
        await _cleanup_gateway_resources(k8s, data.name)
        raise k8s_error_to_http(e, "creating VpcEgressGateway")

    # 6. Patch existing macvlan subnet with excludeIps (skip if we created it above)
    if data.exclude_ips and not create_external:
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                name=external_subnet_name,
                body={"spec": {"excludeIps": data.exclude_ips}},
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            logger.warning(f"Failed to patch macvlan subnet excludeIps: {e}")

    # 7. Initialize transit IP allocator ConfigMap
    await _save_transit_allocator(k8s, data.name, {"_gateway_ip": transit_gateway}, "")

    logger.info(f"Created egress gateway '{data.name}' (vpc={gw_vpc_name}, external={external_subnet_name})")

    return EgressGatewayResponse(
        name=data.name,
        gw_vpc_name=gw_vpc_name,
        gw_vpc_cidr=data.gw_vpc_cidr,
        transit_cidr=data.transit_cidr,
        macvlan_subnet=external_subnet_name,
        replicas=data.replicas,
        bfd_enabled=data.bfd_enabled,
        node_selector=data.node_selector,
        exclude_ips=data.exclude_ips,
        attached_vpcs=[],
        ready=False,
    )


@router.delete("/{name}")
async def delete_egress_gateway(request: Request, name: str, user: User = Depends(require_admin)) -> dict:
    """Delete an egress gateway. Fails if VPCs are still attached."""
    k8s = request.app.state.k8s_client

    vpc = await _get_gateway_config(k8s, name)
    if not vpc:
        raise HTTPException(status_code=404, detail=f"Egress gateway '{name}' not found")

    attached = await _list_attached_vpcs(k8s, name)
    if attached:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete gateway '{name}': {len(attached)} VPC(s) still attached "
                   f"({', '.join(v.vpc_name for v in attached)}). Detach them first.",
        )

    stranded = await _cleanup_gateway_resources(k8s, name)
    if stranded:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Egress gateway '{name}' is still being deleted "
                f"({', '.join(stranded)}). Its VPC has been left in place so "
                "kube-ovn can finish — removing it now would strand the "
                "gateway and its external address permanently. Retry in a "
                "moment."
            ),
        )

    # Delete transit allocator ConfigMap
    try:
        await k8s.core_api.delete_namespaced_config_map(
            name=_transit_cm_name(name), namespace=SYSTEM_NAMESPACE,
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete transit allocator CM: {e}")

    logger.info(f"Deleted egress gateway '{name}'")
    return {"status": "deleted", "name": name}


@router.post("/{name}/attach", response_model=AttachedVpcInfo)
async def attach_tenant_vpc(
    request: Request, name: str, data: AttachTenantRequest,
    user: User = Depends(require_admin),
) -> AttachedVpcInfo:
    """Attach a tenant VPC to an egress gateway."""
    k8s = request.app.state.k8s_client
    result = await attach_tenant_to_gateway(
        k8s, name, data.vpc_name, data.subnet_name, data.cidr,
    )
    return result


@router.post("/{name}/detach")
async def detach_tenant_vpc(
    request: Request, name: str, data: DetachTenantRequest,
    user: User = Depends(require_admin),
) -> dict:
    """Detach a tenant VPC from an egress gateway."""
    k8s = request.app.state.k8s_client
    await detach_tenant_from_gateway(k8s, name, data.vpc_name, data.subnet_name)
    return {"status": "detached", "gateway": name, "vpc": data.vpc_name}


def _reject_self_attach(gateway_name: str, tenant_vpc_name: str) -> None:
    """A gateway cannot be its own tenant.

    The dialog used to offer exactly one option — the gateway's own VPC — so
    this was the only attach a user could actually perform, and it would have
    written a peering from a router to itself.
    """
    if tenant_vpc_name == f"egw-{gateway_name}":
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{tenant_vpc_name}' is this gateway's own VPC. Attach a tenant "
                "VPC instead — the gateway provides egress for other VPCs, not "
                "for itself."
            ),
        )


# ============================================================================
# Internal Functions (called from tenants.py)
# ============================================================================

async def attach_tenant_to_gateway(
    k8s,
    gateway_name: str | None,
    tenant_vpc_name: str,
    tenant_subnet_name: str,
    tenant_cidr: str,
) -> AttachedVpcInfo | None:
    """Attach a tenant VPC to an egress gateway.

    If gateway_name is None, looks for a gateway labeled as "default".
    Returns None if no gateway found and gateway_name was not specified.

    Steps:
      1. Allocate transit IPs for tenant and gateway
      2. Create VpcPeering between gateway VPC and tenant VPC
      3. Add static route on tenant VPC: 0.0.0.0/0 → gateway transit IP
      4. Add return route on gateway VPC: tenant CIDR → tenant transit IP
      5. Update VpcEgressGateway policies to include tenant subnet
      6. Update tenant subnet ACLs to allow transit + gateway CIDRs
    """
    # Resolve gateway
    if not gateway_name:
        gateway_name = await _find_default_gateway(k8s)
        if not gateway_name:
            return None

    _reject_self_attach(gateway_name, tenant_vpc_name)

    vpc = await _get_gateway_config(k8s, gateway_name)
    if not vpc:
        raise HTTPException(status_code=404, detail=f"Egress gateway '{gateway_name}' not found")

    gw_vpc_name = vpc["metadata"]["name"]
    annotations = vpc.get("metadata", {}).get("annotations", {})
    transit_cidr = annotations.get("kubevirt-ui.io/transit-cidr", "")
    gw_vpc_cidr = annotations.get("kubevirt-ui.io/gw-vpc-cidr", "")

    if not transit_cidr:
        raise HTTPException(status_code=500, detail="Gateway missing transit-cidr annotation")

    # 1. Allocate transit IP for tenant
    alloc_data, resource_version = await _get_transit_allocator(k8s, gateway_name)
    used_ips = {v.split(",")[0] for k, v in alloc_data.items() if k.startswith("vpc.")}
    used_ips.add(alloc_data.get("_gateway_ip", ""))

    # An existing allocation is resumed, not short-circuited. Every step below
    # is idempotent, so re-running them is free — whereas returning early here
    # turns a retry after a half-finished attach into a silent no-op that
    # reports success while no peering or route was ever created.
    alloc_key = f"vpc.{tenant_vpc_name}"
    existing = [f for f in alloc_data.get(alloc_key, "").split(",") if f]
    if existing:
        tenant_transit_ip = existing[0]
        if len(existing) > 1:
            tenant_subnet_name = existing[1]
        if len(existing) > 2:
            tenant_cidr = existing[2]
    else:
        tenant_transit_ip = _allocate_transit_ip(transit_cidr, used_ips)

    gw_transit_ip = _gateway_transit_ip(transit_cidr)

    # 2. Peer the two VPCs. kube-ovn keeps peering in `vpc.spec.vpcPeerings[]`
    # and wants it declared on both sides — there is no VpcPeering CRD.
    peering_name = f"{gateway_name}-to-{tenant_vpc_name}"
    peer_prefix = _transit_prefix_len(transit_cidr)
    await _ensure_vpc_peering(
        k8s, gw_vpc_name, tenant_vpc_name, f"{gw_transit_ip}/{peer_prefix}",
    )
    await _ensure_vpc_peering(
        k8s, tenant_vpc_name, gw_vpc_name, f"{tenant_transit_ip}/{peer_prefix}",
    )

    # 3. Add default route on tenant VPC: 0.0.0.0/0 → gateway transit IP
    await _add_static_route(k8s, tenant_vpc_name, "0.0.0.0/0", gw_transit_ip)

    # 4. Add return route on gateway VPC: tenant CIDR → tenant transit IP
    await _add_static_route(k8s, gw_vpc_name, tenant_cidr, tenant_transit_ip)

    # 5. Update VpcEgressGateway policies
    await _update_veg_policies(k8s, gateway_name, add_cidr=tenant_cidr)

    # 5b. Give the gateway VPC a default route to the gateway pod.
    await _ensure_gateway_default_route(k8s, gateway_name, gw_vpc_name)

    # 6. Update tenant subnet ACLs to allow transit + gateway CIDRs
    await _add_acl_allow_cidrs(
        k8s, tenant_subnet_name, tenant_cidr,
        [transit_cidr, gw_vpc_cidr],
    )

    # 7. Record the allocation only once the wiring above is actually in place,
    # so a failure leaves the VPC unattached rather than booked-but-unwired.
    if not existing:
        alloc_data[alloc_key] = f"{tenant_transit_ip},{tenant_subnet_name},{tenant_cidr}"
        await _save_transit_allocator(k8s, gateway_name, alloc_data, resource_version)

    logger.info(
        f"Attached VPC '{tenant_vpc_name}' to egress gateway '{gateway_name}' "
        f"(transit: tenant={tenant_transit_ip}, gw={gw_transit_ip})"
    )

    return AttachedVpcInfo(
        vpc_name=tenant_vpc_name,
        transit_ip=tenant_transit_ip,
        subnet_name=tenant_subnet_name,
        cidr=tenant_cidr,
        peering_name=peering_name,
    )


async def detach_tenant_from_gateway(
    k8s,
    gateway_name: str,
    tenant_vpc_name: str,
    tenant_subnet_name: str,
) -> None:
    """Detach a tenant VPC from an egress gateway.

    Steps:
      1. Delete VpcPeering
      2. Remove return route from gateway VPC
      3. Remove default route from tenant VPC
      4. Remove tenant subnet from VpcEgressGateway policies
      5. Release transit IP allocation
    """
    vpc = await _get_gateway_config(k8s, gateway_name)
    if not vpc:
        raise HTTPException(status_code=404, detail=f"Egress gateway '{gateway_name}' not found")

    gw_vpc_name = vpc["metadata"]["name"]
    annotations = vpc.get("metadata", {}).get("annotations", {})
    transit_cidr = annotations.get("kubevirt-ui.io/transit-cidr", "")
    gw_transit_ip = _gateway_transit_ip(transit_cidr) if transit_cidr else ""

    # Get tenant allocation
    alloc_data, resource_version = await _get_transit_allocator(k8s, gateway_name)
    alloc_key = f"vpc.{tenant_vpc_name}"
    tenant_info = alloc_data.get(alloc_key, "")
    tenant_cidr = tenant_info.split(",")[2] if len(tenant_info.split(",")) > 2 else ""

    # 1. Drop the peering from both VPCs (it lives in spec.vpcPeerings)
    await _remove_vpc_peering(k8s, gw_vpc_name, tenant_vpc_name)
    await _remove_vpc_peering(k8s, tenant_vpc_name, gw_vpc_name)

    # 2. Remove return route from gateway VPC
    if tenant_cidr:
        await _remove_static_route(k8s, gw_vpc_name, tenant_cidr)

    # 3. Remove the default route this gateway installed — and only that one.
    if gw_transit_ip:
        await _remove_static_route(
            k8s, tenant_vpc_name, "0.0.0.0/0", next_hop=gw_transit_ip,
        )

    # 4. Remove tenant subnet from VpcEgressGateway policies
    if tenant_cidr:
        await _update_veg_policies(k8s, gateway_name, remove_cidr=tenant_cidr)

    # 5. Release transit IP
    if alloc_key in alloc_data:
        del alloc_data[alloc_key]
        await _save_transit_allocator(k8s, gateway_name, alloc_data, resource_version)

    logger.info(f"Detached VPC '{tenant_vpc_name}' from egress gateway '{gateway_name}'")


# ============================================================================
# Low-level Helpers
# ============================================================================

async def _find_gateway_for_vpc(k8s, vpc_name: str) -> str | None:
    """Find which egress gateway a VPC is attached to by checking allocators."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            label_selector=f"{GATEWAY_LABEL}",
        )
    except ApiException:
        return None

    for item in result.get("items", []):
        gw_name = item.get("metadata", {}).get("labels", {}).get(GATEWAY_LABEL, "")
        if not gw_name:
            continue
        data, _ = await _get_transit_allocator(k8s, gw_name)
        if f"vpc.{vpc_name}" in data:
            return gw_name

    return None


async def _find_default_gateway(k8s) -> str | None:
    """Find a gateway labeled as default, or the first available one."""
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            label_selector=f"{GATEWAY_LABEL}",
        )
    except ApiException:
        return None

    items = result.get("items", [])
    if not items:
        return None

    # Prefer one labeled as default
    for item in items:
        labels = item.get("metadata", {}).get("labels", {})
        if labels.get("kubevirt-ui.io/egress-default") == "true":
            return labels.get(GATEWAY_LABEL, "")

    # Fall back to first gateway
    return items[0].get("metadata", {}).get("labels", {}).get(GATEWAY_LABEL, "")


async def _ensure_gateway_default_route(
    k8s, gateway_name: str, gw_vpc_name: str,
) -> None:
    """Point the gateway VPC's default route at the egress gateway pod.

    OVN runs `lr_in_ip_routing` (stage 15) *before* `lr_in_policy` (stage 17),
    so a packet with no matching route is dropped before the gateway's own
    reroute policy at priority 29100 is ever consulted. Without this route the
    hub is a black hole that looks perfectly configured: the peering is up,
    the policy lists the tenant CIDR, and every packet still dies one hop past
    the tenant's own router.

    The pod address only exists once the VpcEgressGateway has reconciled,
    which is why this runs on attach rather than at creation time.
    """
    veg = await _get_vpc_egress_gateway(k8s, gateway_name)
    # Prefer the pinned address from the spec: it is known the moment the
    # gateway is created, whereas status only fills in once a pod has come up —
    # and it changes under us whenever one is replaced, silently pointing the
    # hub's default route at an address that no longer exists.
    internal_ips = (
        ((veg or {}).get("spec", {}) or {}).get("internalIPs")
        or ((veg or {}).get("status", {}) or {}).get("internalIPs")
        or []
    )

    if not internal_ips:
        logger.warning(
            f"Egress gateway '{gateway_name}' has no internal IP yet — the gateway "
            f"VPC keeps no default route, and attached tenants have no egress "
            f"until it is attached again.",
        )
        return

    next_hop = internal_ips[0].split("/")[0]
    await _add_static_route(k8s, gw_vpc_name, "0.0.0.0/0", next_hop)


async def _add_static_route(k8s, vpc_name: str, cidr: str, next_hop_ip: str) -> None:
    """Add a static route to a VPC (idempotent)."""
    try:
        vpc = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VPC '{vpc_name}' not found")
        raise

    routes = vpc.get("spec", {}).get("staticRoutes", [])

    # Check if route already exists
    for r in routes:
        if r.get("cidr") == cidr and r.get("nextHopIP") == next_hop_ip:
            return  # Already exists

    routes.append({"cidr": cidr, "nextHopIP": next_hop_ip, "policy": "policyDst"})

    await k8s.custom_api.patch_cluster_custom_object(
        group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
        name=vpc_name, body={"spec": {"staticRoutes": routes}},
        _content_type="application/merge-patch+json",
    )


async def _remove_static_route(
    k8s, vpc_name: str, cidr: str, next_hop: str | None = None,
) -> None:
    """Remove a static route from a VPC.

    `next_hop` narrows it to the route this gateway actually installed.
    Without it, detaching a VPC deleted **every** route with that CIDR —
    including the `0.0.0.0/0 -> 10.198.191.254` the VPC was created with, so
    a detach left the tenant with no way out at all. Measured on the lab
    cluster: acme-net went from four static routes to three, losing the
    default it had before the gateway ever existed.
    """
    try:
        vpc = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            return  # VPC already gone
        raise

    routes = vpc.get("spec", {}).get("staticRoutes", [])
    new_routes = [
        r for r in routes
        if r.get("cidr") != cidr
        or (next_hop is not None and r.get("nextHopIP") != next_hop)
    ]

    if len(new_routes) != len(routes):
        await k8s.custom_api.patch_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=vpc_name, body={"spec": {"staticRoutes": new_routes}},
            _content_type="application/merge-patch+json",
        )


async def _update_veg_policies(
    k8s, gateway_name: str,
    add_cidr: str | None = None,
    remove_cidr: str | None = None,
) -> None:
    """Add or remove a CIDR from the gateway's egress policies.

    Policies carry source ranges under `ipBlocks` (or `subnets`, for a named
    subnet). This used to write `{"cidr": ..., "snat": true}` — a key the CRD
    has no such field for, so kube-ovn ignored it and an attached tenant's
    traffic never actually went through the gateway, while the object looked
    correctly patched.

    `snat` is inherited from the gateway's existing policies rather than
    forced on: a routed tenant runs snat=false on purpose, and hardcoding
    true here would masquerade it back behind the gateway address and undo
    the BGP announcement.
    """
    veg = await _get_vpc_egress_gateway(k8s, gateway_name)
    if not veg:
        logger.warning(f"VpcEgressGateway '{gateway_name}' not found for policy update")
        return

    policies = veg.get("spec", {}).get("policies", [])

    def _blocks(policy: dict) -> list[str]:
        return policy.get("ipBlocks", []) if isinstance(policy, dict) else []

    existing_cidrs = {cidr for p in policies for cidr in _blocks(p)}
    snat = next(
        (p.get("snat", True) for p in policies if isinstance(p, dict)), True,
    )

    if add_cidr and add_cidr not in existing_cidrs:
        policies.append({"snat": snat, "ipBlocks": [add_cidr]})

    if remove_cidr:
        for policy in policies:
            if isinstance(policy, dict) and remove_cidr in _blocks(policy):
                policy["ipBlocks"] = [c for c in policy["ipBlocks"] if c != remove_cidr]
        # Drop policies whose last ipBlock just went away, but keep
        # subnet-based ones (they have no ipBlocks to begin with).
        policies = [
            p for p in policies
            if not (isinstance(p, dict) and "ipBlocks" in p and not p["ipBlocks"])
        ]

    patch = {"spec": {"policies": policies}}

    try:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            namespace="kube-system", plural="vpc-egress-gateways",
            name=gateway_name, body=patch,
            _content_type="application/merge-patch+json",
        )
    except ApiException as e:
        logger.error(f"Failed to update VpcEgressGateway policies: {e}")
        raise k8s_error_to_http(e, "updating VpcEgressGateway policies")


async def _add_acl_allow_cidrs(
    k8s, subnet_name: str, src_cidr: str, allow_cidrs: list[str],
) -> None:
    """Add ACL rules to a tenant subnet allowing traffic to transit/gw CIDRs."""
    try:
        subnet = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            name=subnet_name,
        )
    except ApiException as e:
        if e.status == 404:
            logger.warning(f"Subnet '{subnet_name}' not found for ACL update")
            return
        raise

    acls = subnet.get("spec", {}).get("acls", [])

    for cidr in allow_cidrs:
        match_str = f"ip4.src == {src_cidr} && ip4.dst == {cidr}"
        already_exists = any(a.get("match") == match_str for a in acls)
        if not already_exists:
            acls.append({
                "action": "allow-related",
                "direction": "from-lport",
                "match": match_str,
                "priority": 2800,
            })

    await k8s.custom_api.patch_cluster_custom_object(
        group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
        name=subnet_name, body={"spec": {"acls": acls}},
        _content_type="application/merge-patch+json",
    )


async def _cleanup_gateway_vpc(k8s, gw_vpc_name: str) -> None:
    """Delete just the gateway VPC (used during creation rollback)."""
    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="vpcs",
            name=gw_vpc_name,
        )
    except ApiException:
        pass


# How long a delete waits for kube-ovn before handing the caller a retry.
GATEWAY_DRAIN_TIMEOUT = 45.0


async def _cleanup_gateway_resources(k8s, gateway_name: str) -> list[str]:
    """Delete all resources associated with an egress gateway.

    Returns whatever was still terminating when the wait ran out; empty when
    the teardown completed.
    """
    label_sel = f"{GATEWAY_LABEL}={gateway_name}"

    # Delete VpcEgressGateway
    try:
        await k8s.custom_api.delete_namespaced_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
            namespace="kube-system", plural="vpc-egress-gateways",
            name=gateway_name,
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete VpcEgressGateway: {e}")

    # Drop the peering each attached tenant still holds against the gateway
    # VPC. The gateway's own side goes with its VPC below, but a tenant left
    # pointing at a deleted VPC keeps a dead default route.
    gw_vpc_name = f"egw-{gateway_name}"
    alloc_data, _ = await _get_transit_allocator(k8s, gateway_name)
    for key in alloc_data:
        if not key.startswith("vpc."):
            continue
        tenant_vpc = key[len("vpc."):]
        try:
            await _remove_vpc_peering(k8s, tenant_vpc, gw_vpc_name)
            transit = (alloc_data.get(key, "").split(",") or [""])[0]
            await _remove_static_route(
                k8s, tenant_vpc, "0.0.0.0/0", next_hop=transit or None,
            )
        except ApiException as e:
            logger.warning(f"Failed to unpeer {tenant_vpc} from {gw_vpc_name}: {e}")

    # Delete subnets (transit + gw)
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
            label_selector=label_sel,
        )
        for item in result.get("items", []):
            try:
                await k8s.custom_api.delete_cluster_custom_object(
                    group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                    name=item["metadata"]["name"],
                )
            except ApiException:
                pass
    except ApiException:
        pass

    # Delete gateway VPC — but not before the objects finalized against its
    # logical router are actually gone.
    #
    # `shared-egress` sat on the lab cluster for 29 hours in exactly this
    # state: deletionTimestamp set, phase still Completed, holding
    # 10.198.190.207 from the external pool, while the controller looped
    #
    #   error syncing delete vpc egress gateway "kube-system/shared-egress":
    #   not found logical router "egw-shared-egress", requeuing
    #
    # The VPC had already been removed, so the finalizer could never run.
    gw_vpc_name = f"egw-{gateway_name}"
    left = await _await_gateway_gone(k8s, gateway_name)
    if left:
        logger.warning(
            f"Egress gateway {gateway_name!r}: {', '.join(left)} still "
            "terminating; leaving the gateway VPC in place so kube-ovn can "
            "finish. Retry the delete.",
        )
        return left
    await _cleanup_gateway_vpc(k8s, gw_vpc_name)

    logger.info(f"Cleaned up all resources for egress gateway '{gateway_name}'")
    return []


async def _await_gateway_gone(
    k8s, gateway_name: str, timeout: float | None = None,
) -> list[str]:
    """Wait for the VpcEgressGateway and its subnets to really disappear."""
    deadline = asyncio.get_running_loop().time() + (
        GATEWAY_DRAIN_TIMEOUT if timeout is None else timeout
    )
    label_sel = f"{GATEWAY_LABEL}={gateway_name}"
    while True:
        left: list[str] = []
        try:
            await k8s.custom_api.get_namespaced_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION,
                namespace="kube-system", plural="vpc-egress-gateways",
                name=gateway_name,
            )
            left.append(f"vpc-egress-gateway/{gateway_name}")
        except ApiException as e:
            if e.status != 404:
                left.append(f"vpc-egress-gateway/{gateway_name}")
        try:
            found = await k8s.custom_api.list_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="subnets",
                label_selector=label_sel,
            )
            left += [f"subnet/{i['metadata']['name']}" for i in found.get("items", [])]
        except ApiException:
            pass
        if not left:
            return []
        if asyncio.get_running_loop().time() >= deadline:
            return left
        await asyncio.sleep(2)
