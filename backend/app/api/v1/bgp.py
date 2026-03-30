"""BGP Speaker Management API endpoints.

Manages kube-ovn-speaker DaemonSet deployment, node labeling,
and BGP route announcements for subnets/services/EIPs.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.api.v1.network import _find_kubeovn_namespace
from app.core.auth import User, require_auth
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION
from app.core.errors import k8s_error_to_http
from app.models.bgp import (
    AnnouncementRequest,
    AnnouncementResponse,
    BGPSessionResponse,
    GatewayConfigExample,
    SpeakerDeployRequest,
    SpeakerStatusResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

SPEAKER_NAME = "kube-ovn-speaker"
BGP_NODE_LABEL = "ovn.kubernetes.io/bgp"
BGP_ANNOTATION = "ovn.kubernetes.io/bgp"


# ============================================================================
# Helpers
# ============================================================================

async def _get_kubeovn_image(k8s, namespace: str) -> str:
    """Get kube-ovn image from the kube-ovn-controller deployment."""
    try:
        dep = await k8s.apps_api.read_namespaced_deployment(
            name="kube-ovn-controller", namespace=namespace,
        )
        for container in dep.spec.template.spec.containers:
            if "kube-ovn" in container.image:
                return container.image
    except ApiException as e:
        logger.warning(f"Failed to read kube-ovn-controller deployment: {e}")
    raise HTTPException(
        status_code=500,
        detail="Cannot detect kube-ovn image from kube-ovn-controller deployment",
    )


def _build_speaker_daemonset(
    namespace: str,
    image: str,
    neighbor_address: str,
    neighbor_as: int,
    cluster_as: int,
    announce_cluster_ip: bool,
) -> dict[str, Any]:
    """Build kube-ovn-speaker DaemonSet manifest."""
    args = [
        f"--neighbor-address={neighbor_address}",
        f"--neighbor-as={neighbor_as}",
        f"--cluster-as={cluster_as}",
    ]
    if announce_cluster_ip:
        args.append("--announce-cluster-ip=true")

    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": SPEAKER_NAME,
            "namespace": namespace,
            "labels": {"app": SPEAKER_NAME},
        },
        "spec": {
            "selector": {"matchLabels": {"app": SPEAKER_NAME}},
            "template": {
                "metadata": {"labels": {"app": SPEAKER_NAME}},
                "spec": {
                    "hostNetwork": True,
                    "serviceAccountName": "ovn",
                    "automountServiceAccountToken": True,
                    "nodeSelector": {BGP_NODE_LABEL: "true"},
                    "tolerations": [
                        {
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        },
                    ],
                    "containers": [
                        {
                            "name": SPEAKER_NAME,
                            "image": image,
                            "command": ["/kube-ovn/kube-ovn-speaker"],
                            "args": args,
                            "env": [
                                {
                                    "name": "POD_IP",
                                    "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "name": "kube-ovn-log",
                                    "mountPath": "/var/log/kube-ovn",
                                },
                            ],
                        },
                    ],
                    "volumes": [
                        {
                            "name": "kube-ovn-log",
                            "hostPath": {
                                "path": "/var/log/kube-ovn",
                                "type": "DirectoryOrCreate",
                            },
                        },
                    ],
                },
            },
        },
    }


def _parse_speaker_args(ds: Any) -> dict[str, Any]:
    """Parse speaker config from DaemonSet args."""
    config: dict[str, Any] = {}
    try:
        containers = ds.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if not containers:
            return config
        args = containers[0].get("args", [])
        for arg in args:
            if "=" in arg:
                key, val = arg.lstrip("-").split("=", 1)
                config[key] = val
    except (IndexError, AttributeError):
        pass
    return config


async def _label_nodes(k8s, node_names: list[str], add: bool = True) -> None:
    """Add or remove BGP label from nodes."""
    label_value = "true" if add else None
    body = {"metadata": {"labels": {BGP_NODE_LABEL: label_value}}}
    for name in node_names:
        try:
            await k8s.core_api.patch_node(
                name=name, body=body,
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            logger.warning(f"Failed to {'label' if add else 'unlabel'} node {name}: {e}")


async def _get_bgp_nodes(k8s) -> list[str]:
    """List nodes with the BGP label."""
    try:
        nodes = await k8s.core_api.list_node(
            label_selector=f"{BGP_NODE_LABEL}=true",
        )
        return [n.metadata.name for n in nodes.items]
    except ApiException:
        return []


# ============================================================================
# Speaker Endpoints
# ============================================================================

@router.post("/speaker", response_model=SpeakerStatusResponse, status_code=201)
async def deploy_speaker(
    request: Request, data: SpeakerDeployRequest,
    user: User = Depends(require_auth),
) -> SpeakerStatusResponse:
    """Deploy kube-ovn-speaker DaemonSet."""
    k8s = request.app.state.k8s_client
    namespace = await _find_kubeovn_namespace(k8s)
    image = await _get_kubeovn_image(k8s, namespace)

    manifest = _build_speaker_daemonset(
        namespace=namespace,
        image=image,
        neighbor_address=data.neighbor_address,
        neighbor_as=data.neighbor_as,
        cluster_as=data.cluster_as,
        announce_cluster_ip=data.announce_cluster_ip,
    )

    try:
        await k8s.apps_api.create_namespaced_daemon_set(
            namespace=namespace, body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail="kube-ovn-speaker DaemonSet already exists")
        raise k8s_error_to_http(e, "creating kube-ovn-speaker DaemonSet")

    # Label nodes
    if data.node_names:
        await _label_nodes(k8s, data.node_names, add=True)

    logger.info(f"Deployed kube-ovn-speaker in {namespace} (neighbor={data.neighbor_address})")
    return await _get_speaker_status(k8s, namespace)


@router.get("/speaker", response_model=SpeakerStatusResponse)
async def get_speaker(
    request: Request, user: User = Depends(require_auth),
) -> SpeakerStatusResponse:
    """Get speaker deployment status."""
    k8s = request.app.state.k8s_client
    namespace = await _find_kubeovn_namespace(k8s)
    return await _get_speaker_status(k8s, namespace)


@router.put("/speaker", response_model=SpeakerStatusResponse)
async def update_speaker(
    request: Request, data: SpeakerDeployRequest,
    user: User = Depends(require_auth),
) -> SpeakerStatusResponse:
    """Update speaker config (patches DaemonSet args, triggers rolling restart)."""
    k8s = request.app.state.k8s_client
    namespace = await _find_kubeovn_namespace(k8s)
    image = await _get_kubeovn_image(k8s, namespace)

    manifest = _build_speaker_daemonset(
        namespace=namespace,
        image=image,
        neighbor_address=data.neighbor_address,
        neighbor_as=data.neighbor_as,
        cluster_as=data.cluster_as,
        announce_cluster_ip=data.announce_cluster_ip,
    )

    try:
        await k8s.apps_api.replace_namespaced_daemon_set(
            name=SPEAKER_NAME, namespace=namespace, body=manifest,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail="kube-ovn-speaker DaemonSet not found")
        raise k8s_error_to_http(e, "updating kube-ovn-speaker DaemonSet")

    # Update node labels: label new nodes, unlabel removed ones
    if data.node_names:
        current_nodes = set(await _get_bgp_nodes(k8s))
        desired_nodes = set(data.node_names)
        to_add = desired_nodes - current_nodes
        to_remove = current_nodes - desired_nodes
        if to_add:
            await _label_nodes(k8s, list(to_add), add=True)
        if to_remove:
            await _label_nodes(k8s, list(to_remove), add=False)

    logger.info(f"Updated kube-ovn-speaker config (neighbor={data.neighbor_address})")
    return await _get_speaker_status(k8s, namespace)


@router.delete("/speaker")
async def delete_speaker(
    request: Request, user: User = Depends(require_auth),
) -> dict:
    """Undeploy speaker: delete DaemonSet and remove node labels."""
    k8s = request.app.state.k8s_client
    namespace = await _find_kubeovn_namespace(k8s)

    # Delete DaemonSet
    try:
        await k8s.apps_api.delete_namespaced_daemon_set(
            name=SPEAKER_NAME, namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail="kube-ovn-speaker DaemonSet not found")
        raise k8s_error_to_http(e, "deleting kube-ovn-speaker DaemonSet")

    # Remove BGP labels from all nodes
    bgp_nodes = await _get_bgp_nodes(k8s)
    if bgp_nodes:
        await _label_nodes(k8s, bgp_nodes, add=False)

    logger.info("Deleted kube-ovn-speaker DaemonSet and removed BGP node labels")
    return {"status": "deleted"}


async def _get_speaker_status(k8s, namespace: str) -> SpeakerStatusResponse:
    """Build speaker status response."""
    try:
        ds = await k8s.apps_api.read_namespaced_daemon_set(
            name=SPEAKER_NAME, namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            return SpeakerStatusResponse(deployed=False)
        raise

    # Parse config from DaemonSet spec
    config: dict[str, Any] = {}
    try:
        containers = ds.spec.template.spec.containers
        if containers:
            for arg in (containers[0].args or []):
                if "=" in arg:
                    key, val = arg.lstrip("-").split("=", 1)
                    config[key] = val
    except (IndexError, AttributeError):
        pass

    # List speaker pods
    pods_info = []
    try:
        pods = await k8s.core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={SPEAKER_NAME}",
        )
        for pod in pods.items:
            pods_info.append({
                "name": pod.metadata.name,
                "node": pod.spec.node_name or "",
                "status": pod.status.phase if pod.status else "Unknown",
            })
    except ApiException:
        pass

    node_labels = await _get_bgp_nodes(k8s)

    return SpeakerStatusResponse(
        deployed=True,
        config=config,
        pods=pods_info,
        node_labels=node_labels,
    )


# ============================================================================
# Announcement Endpoints
# ============================================================================

@router.get("/announcements", response_model=list[AnnouncementResponse])
async def list_announcements(
    request: Request, user: User = Depends(require_auth),
) -> list[AnnouncementResponse]:
    """List all resources with BGP announcements enabled."""
    k8s = request.app.state.k8s_client
    results: list[AnnouncementResponse] = []

    # Subnets
    try:
        subnets = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="subnets",
        )
        for s in subnets.get("items", []):
            ann = s.get("metadata", {}).get("annotations", {})
            bgp_val = ann.get(BGP_ANNOTATION, "")
            if bgp_val:
                results.append(AnnouncementResponse(
                    resource_type="subnet",
                    resource_name=s["metadata"]["name"],
                    bgp_enabled=True,
                    policy=bgp_val,
                ))
    except ApiException:
        pass

    # Services (across all namespaces)
    try:
        services = await k8s.core_api.list_service_for_all_namespaces()
        for svc in services.items:
            ann = svc.metadata.annotations or {}
            bgp_val = ann.get(BGP_ANNOTATION, "")
            if bgp_val:
                results.append(AnnouncementResponse(
                    resource_type="service",
                    resource_name=svc.metadata.name,
                    resource_namespace=svc.metadata.namespace,
                    bgp_enabled=True,
                    policy=bgp_val,
                ))
    except ApiException:
        pass

    # OvnEips
    try:
        eips = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION, plural="ovn-eips",
        )
        for e in eips.get("items", []):
            ann = e.get("metadata", {}).get("annotations", {})
            bgp_val = ann.get(BGP_ANNOTATION, "")
            if bgp_val:
                results.append(AnnouncementResponse(
                    resource_type="eip",
                    resource_name=e["metadata"]["name"],
                    bgp_enabled=True,
                    policy=bgp_val,
                ))
    except ApiException:
        pass

    return results


@router.post("/announcements", response_model=AnnouncementResponse)
async def create_announcement(
    request: Request, data: AnnouncementRequest,
    user: User = Depends(require_auth),
) -> AnnouncementResponse:
    """Add BGP announcement annotation to a resource."""
    k8s = request.app.state.k8s_client
    annotation_value = data.policy or "true"
    patch = {"metadata": {"annotations": {BGP_ANNOTATION: annotation_value}}}

    try:
        if data.resource_type == "subnet":
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="subnets", name=data.resource_name, body=patch,
                _content_type="application/merge-patch+json",
            )
        elif data.resource_type == "service":
            if not data.resource_namespace:
                raise HTTPException(status_code=422, detail="resource_namespace required for services")
            await k8s.core_api.patch_namespaced_service(
                name=data.resource_name, namespace=data.resource_namespace,
                body=patch,
                _content_type="application/merge-patch+json",
            )
        elif data.resource_type == "eip":
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="ovn-eips", name=data.resource_name, body=patch,
                _content_type="application/merge-patch+json",
            )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported resource type: {data.resource_type}. Use subnet, service, or eip.",
            )
    except ApiException as e:
        raise k8s_error_to_http(e, f"annotating {data.resource_type} '{data.resource_name}'")

    logger.info(f"Added BGP announcement for {data.resource_type}/{data.resource_name} (policy={annotation_value})")
    return AnnouncementResponse(
        resource_type=data.resource_type,
        resource_name=data.resource_name,
        resource_namespace=data.resource_namespace,
        bgp_enabled=True,
        policy=annotation_value,
    )


@router.delete("/announcements", response_model=AnnouncementResponse)
async def delete_announcement(
    request: Request, data: AnnouncementRequest,
    user: User = Depends(require_auth),
) -> AnnouncementResponse:
    """Remove BGP announcement annotation from a resource."""
    k8s = request.app.state.k8s_client
    # Setting annotation to None removes it in merge-patch
    patch = {"metadata": {"annotations": {BGP_ANNOTATION: None}}}

    try:
        if data.resource_type == "subnet":
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="subnets", name=data.resource_name, body=patch,
                _content_type="application/merge-patch+json",
            )
        elif data.resource_type == "service":
            if not data.resource_namespace:
                raise HTTPException(status_code=422, detail="resource_namespace required for services")
            await k8s.core_api.patch_namespaced_service(
                name=data.resource_name, namespace=data.resource_namespace,
                body=patch,
                _content_type="application/merge-patch+json",
            )
        elif data.resource_type == "eip":
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP, version=KUBEOVN_API_VERSION,
                plural="ovn-eips", name=data.resource_name, body=patch,
                _content_type="application/merge-patch+json",
            )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported resource type: {data.resource_type}. Use subnet, service, or eip.",
            )
    except ApiException as e:
        raise k8s_error_to_http(e, f"removing annotation from {data.resource_type} '{data.resource_name}'")

    logger.info(f"Removed BGP announcement for {data.resource_type}/{data.resource_name}")
    return AnnouncementResponse(
        resource_type=data.resource_type,
        resource_name=data.resource_name,
        resource_namespace=data.resource_namespace,
        bgp_enabled=False,
    )


# ============================================================================
# BGP Sessions Endpoint
# ============================================================================

@router.get("/sessions", response_model=list[BGPSessionResponse])
async def list_sessions(
    request: Request, user: User = Depends(require_auth),
) -> list[BGPSessionResponse]:
    """Get BGP session status from speaker pods.

    Execs into each speaker pod and attempts to parse GoBGP neighbor status.
    Returns empty list if speaker is not deployed or exec fails.
    """
    k8s = request.app.state.k8s_client
    namespace = await _find_kubeovn_namespace(k8s)

    try:
        pods = await k8s.core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={SPEAKER_NAME}",
        )
    except ApiException:
        return []

    results: list[BGPSessionResponse] = []

    # Parse speaker config to find expected peers
    try:
        ds = await k8s.apps_api.read_namespaced_daemon_set(
            name=SPEAKER_NAME, namespace=namespace,
        )
        config = {}
        containers = ds.spec.template.spec.containers
        if containers:
            for arg in (containers[0].args or []):
                if "=" in arg:
                    key, val = arg.lstrip("-").split("=", 1)
                    config[key] = val
        neighbor_addresses = [a.strip() for a in config.get("neighbor-address", "").split(",") if a.strip()]
        neighbor_as = int(config.get("neighbor-as", "0"))
    except (ApiException, ValueError):
        neighbor_addresses = []
        neighbor_as = 0

    for pod in pods.items:
        if pod.status.phase != "Running":
            continue
        node_name = pod.spec.node_name or ""

        # Parse pod logs for BGP session state
        # kube-ovn-speaker logs lines like:
        #   "Peer Up" / "Peer Down" / "Add a]peer" with neighbor address
        peer_states: dict[str, str] = {}
        try:
            log = await k8s.core_api.read_namespaced_pod_log(
                name=pod.metadata.name,
                namespace=namespace,
                tail_lines=200,
            )
            for line in log.split("\n"):
                line_lower = line.lower()
                for addr in neighbor_addresses:
                    if addr in line:
                        if "established" in line_lower or "peer up" in line_lower:
                            peer_states[addr] = "Established"
                        elif "peer down" in line_lower or "closed" in line_lower:
                            peer_states[addr] = "Active"
                        elif "add a peer" in line_lower or "add peer" in line_lower:
                            if addr not in peer_states:
                                peer_states[addr] = "Connecting"
        except ApiException as e:
            logger.debug(f"Failed to read logs from {pod.metadata.name}: {e}")

        # Build session entries — one per neighbor per speaker pod
        for addr in neighbor_addresses:
            state = peer_states.get(addr, "Unknown")
            results.append(BGPSessionResponse(
                peer_address=addr,
                peer_asn=neighbor_as,
                state=state,
                uptime="",
                prefixes_received=0,
                node=node_name,
            ))

    return results


# ============================================================================
# Gateway Config Examples
# ============================================================================

def _generate_gateway_examples(
    cluster_as: int, neighbor_as: int, node_ips: list[str],
) -> list[GatewayConfigExample]:
    """Generate gateway config examples based on current speaker config."""
    nodes_frr = "\n".join(f"  neighbor {ip} remote-as {cluster_as}" for ip in node_ips)
    nodes_frr_activate = "\n".join(f"    neighbor {ip} activate" for ip in node_ips)
    nodes_bird = "\n".join(
        f'  neighbor k8s_node_{i+1} from k8s_nodes {{\n    neighbor {ip};\n  }}'
        for i, ip in enumerate(node_ips)
    )

    frr_config = f"""frr defaults traditional
hostname gateway
log syslog informational
!
router bgp {neighbor_as}
  bgp router-id <GATEWAY_IP>
  no bgp ebgp-requires-policy
  !
{nodes_frr}
  !
  address-family ipv4 unicast
{nodes_frr_activate}
  exit-address-family
!
! Optional: default route to upstream
! ip route 0.0.0.0/0 <UPSTREAM_GATEWAY>
!
! Verify: vtysh -c "show bgp summary"
! Routes: vtysh -c "show ip bgp"
"""

    bird_config = f"""log syslog all;
router id <GATEWAY_IP>;

protocol device {{
  scan time 10;
}}

protocol direct {{
  ipv4;
  interface "eth0";
}}

protocol kernel {{
  ipv4 {{
    export all;
    import all;
  }};
  learn;
}}

template bgp k8s_nodes {{
  local as {neighbor_as};
  neighbor as {cluster_as};
  ipv4 {{
    import all;
    export none;
  }};
  graceful restart on;
  connect retry time 10;
  hold time 30;
  keepalive time 10;
}}

{nodes_bird if nodes_bird else '# neighbor k8s_node_1 from k8s_nodes {{ neighbor <NODE_IP>; }}'}

# Verify: birdc show protocols all
# Routes: birdc show route
"""

    return [
        GatewayConfigExample(
            name="frr",
            title="FRRouting (FRR)",
            description="Most common Linux routing suite. Install: apt install frr / apk add frr",
            config=frr_config,
        ),
        GatewayConfigExample(
            name="bird",
            title="BIRD Internet Routing Daemon",
            description="Lightweight BGP daemon for IXPs. Install: apt install bird2 / apk add bird",
            config=bird_config,
        ),
    ]


@router.get("/gateway-config", response_model=list[GatewayConfigExample])
async def get_gateway_config_examples(
    request: Request, user: User = Depends(require_auth),
) -> list[GatewayConfigExample]:
    """Generate gateway config examples with actual ASN and node IPs from speaker config."""
    k8s = request.app.state.k8s_client
    namespace = await _find_kubeovn_namespace(k8s)

    cluster_as = 65001
    neighbor_as = 65000
    try:
        ds = await k8s.apps_api.read_namespaced_daemon_set(
            name=SPEAKER_NAME, namespace=namespace,
        )
        for arg in (ds.spec.template.spec.containers[0].args or []):
            if "=" in arg:
                key, val = arg.lstrip("-").split("=", 1)
                if key == "cluster-as":
                    cluster_as = int(val)
                elif key == "neighbor-as":
                    neighbor_as = int(val)
    except (ApiException, AttributeError, ValueError, IndexError):
        pass

    node_ips: list[str] = []
    bgp_nodes = await _get_bgp_nodes(k8s)
    if bgp_nodes:
        try:
            for node_name in bgp_nodes:
                node = await k8s.core_api.read_node(node_name)
                for addr in node.status.addresses:
                    if addr.type == "InternalIP":
                        node_ips.append(addr.address)
                        break
        except ApiException:
            pass

    if not node_ips:
        try:
            nodes = await k8s.core_api.list_node()
            for n in nodes.items:
                labels = n.metadata.labels or {}
                if "node-role.kubernetes.io/control-plane" not in labels:
                    for addr in n.status.addresses:
                        if addr.type == "InternalIP":
                            node_ips.append(addr.address)
                            break
        except ApiException:
            node_ips = ["<NODE_IP_1>", "<NODE_IP_2>"]

    return _generate_gateway_examples(cluster_as, neighbor_as, node_ips)
