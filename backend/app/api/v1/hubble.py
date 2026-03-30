"""Hubble Network Flows API endpoints.

Proxies Hubble flow data by executing `hubble observe` CLI inside a
cilium agent pod via kubectl exec. The hubble-relay pod is distroless
(no shell), so we exec into cilium agent DaemonSet pods instead.
"""

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()

# Namespaces where cilium agent pods might be deployed
CILIUM_NAMESPACES = ["o0-cilium", "cilium", "kube-system"]
CILIUM_AGENT_LABEL = "app.kubernetes.io/name=cilium-agent"


# ============================================================================
# Models
# ============================================================================

class HubbleFlow(BaseModel):
    time: str = ""
    source_namespace: str = ""
    source_pod: str = ""
    source_ip: str = ""
    destination_namespace: str = ""
    destination_pod: str = ""
    destination_ip: str = ""
    destination_port: int = 0
    protocol: str = ""
    verdict: str = ""
    drop_reason: str = ""
    policy_match: str = ""
    summary: str = ""


class HubbleFlowsResponse(BaseModel):
    flows: list[HubbleFlow] = []
    total: int = 0
    hubble_namespace: str = ""


class HubbleStatusResponse(BaseModel):
    available: bool = False
    namespace: str = ""
    pod_name: str = ""
    num_connected_nodes: int = 0
    max_flows: int = 0
    message: str = ""


# ============================================================================
# Helpers
# ============================================================================

async def _find_cilium_agent_pod(k8s) -> tuple[str, str] | None:
    """Find a running cilium agent pod for hubble CLI exec.

    Hubble Relay is distroless (no shell), so we exec into cilium agent
    DaemonSet pods which have the hubble CLI built-in.
    Returns (namespace, pod_name) or None.
    """
    for ns in CILIUM_NAMESPACES:
        try:
            pods = await k8s.core_api.list_namespaced_pod(
                namespace=ns, label_selector=CILIUM_AGENT_LABEL,
            )
            for pod in pods.items or []:
                if pod.status and pod.status.phase == "Running":
                    return (ns, pod.metadata.name)
        except ApiException:
            continue
    return None


async def _exec_in_pod(k8s, namespace: str, pod_name: str, command: list[str]) -> str:
    """Execute command in a pod and return stdout."""
    from kubernetes_asyncio.stream import WsApiClient

    ws_client = WsApiClient(configuration=k8s._api_client.configuration)
    try:
        from kubernetes_asyncio.client import CoreV1Api
        ws_core = CoreV1Api(ws_client)
        resp = await ws_core.connect_get_namespaced_pod_exec(
            name=pod_name, namespace=namespace,
            command=command,
            stderr=True, stdin=False, stdout=True, tty=False,
        )
        return resp
    finally:
        await ws_client.close()


def _parse_hubble_flow(raw: dict[str, Any]) -> HubbleFlow:
    """Parse a single Hubble JSON flow entry."""
    flow = raw.get("flow", raw)

    source = flow.get("source", {})
    dest = flow.get("destination", {})
    l4 = flow.get("l4", {})

    # Extract protocol and port from l4
    protocol = ""
    dest_port = 0
    if "TCP" in l4:
        protocol = "TCP"
        dest_port = l4["TCP"].get("destination_port", 0)
    elif "UDP" in l4:
        protocol = "UDP"
        dest_port = l4["UDP"].get("destination_port", 0)
    elif "ICMPv4" in l4:
        protocol = "ICMP"
    elif "ICMPv6" in l4:
        protocol = "ICMPv6"

    drop_reason_id = flow.get("drop_reason", 0)
    drop_reason_desc = flow.get("drop_reason_desc", "")

    return HubbleFlow(
        time=flow.get("time", ""),
        source_namespace=source.get("namespace", ""),
        source_pod=source.get("pod_name", ""),
        source_ip=flow.get("IP", {}).get("source", ""),
        destination_namespace=dest.get("namespace", ""),
        destination_pod=dest.get("pod_name", ""),
        destination_ip=flow.get("IP", {}).get("destination", ""),
        destination_port=dest_port,
        protocol=protocol,
        verdict=flow.get("verdict", ""),
        drop_reason=drop_reason_desc if drop_reason_desc else (str(drop_reason_id) if drop_reason_id else ""),
        policy_match=str(flow.get("policy_match_type", "")),
        summary=flow.get("Summary", ""),
    )


# Strict validation patterns for hubble CLI arguments
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_TIME_WINDOW_RE = re.compile(r"^\d+[smh]$")
_VERDICT_VALUES = {"FORWARDED", "DROPPED", "ERROR", "AUDIT", "REDIRECTED", "TRACED"}
_PROTOCOL_VALUES = {"tcp", "udp", "icmp", "icmpv6"}


def _validate_hubble_param(value: str, pattern: re.Pattern, name: str) -> str:
    """Validate a hubble CLI parameter against a strict regex.
    # Attack vector blocked: value='5m; rm -rf /' would fail validation
    """
    if not pattern.match(value):
        raise HTTPException(status_code=422, detail=f"Invalid {name}: {value!r}")
    return value


# ============================================================================
# Endpoints
# ============================================================================

from app.core.auth import User, require_auth


@router.get("/flows", response_model=HubbleFlowsResponse)
async def get_hubble_flows(
    request: Request,
    namespace: str | None = Query(None, description="Filter by namespace"),
    pod: str | None = Query(None, description="Filter by pod name"),
    verdict: str | None = Query(None, description="FORWARDED, DROPPED, ERROR"),
    protocol: str | None = Query(None, description="tcp, udp, icmp"),
    limit: int = Query(100, ge=1, le=10000, description="Max flows to return"),
    since: str = Query("5m", description="Time window (e.g. 5m, 1h)"),
    user: User = Depends(require_auth),
) -> HubbleFlowsResponse:
    """Query recent Hubble network flows."""
    k8s = request.app.state.k8s_client

    agent = await _find_cilium_agent_pod(k8s)
    if not agent:
        raise HTTPException(
            status_code=503,
            detail="Cilium agent pod not found. Check if Cilium is deployed.",
        )

    ns, pod_name = agent

    # Build hubble observe command as list (no shell — prevents injection)
    _validate_hubble_param(since, _TIME_WINDOW_RE, "since")
    hubble_cmd = ["hubble", "observe", "--output", "json", "--last", str(limit), "--since", since]
    if namespace:
        _validate_hubble_param(namespace, _K8S_NAME_RE, "namespace")
        hubble_cmd += ["--namespace", namespace]
    if pod:
        _validate_hubble_param(pod, _K8S_NAME_RE, "pod")
        hubble_cmd += ["--pod", pod]
    if verdict:
        if verdict.upper() not in _VERDICT_VALUES:
            raise HTTPException(status_code=422, detail=f"Invalid verdict: {verdict!r}")
        hubble_cmd += ["--verdict", verdict.upper()]
    if protocol:
        if protocol.lower() not in _PROTOCOL_VALUES:
            raise HTTPException(status_code=422, detail=f"Invalid protocol: {protocol!r}")
        hubble_cmd += ["--protocol", protocol.lower()]

    try:
        output = await _exec_in_pod(
            k8s, ns, pod_name,
            hubble_cmd,
        )
    except Exception as e:
        logger.error(f"Failed to exec hubble observe: {e}")
        raise HTTPException(status_code=500, detail="Hubble query failed")

    # Parse JSON lines output
    flows: list[HubbleFlow] = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            if "flow" in raw or "source" in raw:
                flows.append(_parse_hubble_flow(raw))
        except json.JSONDecodeError:
            continue

    return HubbleFlowsResponse(
        flows=flows, total=len(flows), hubble_namespace=ns,
    )


@router.get("/status", response_model=HubbleStatusResponse)
async def get_hubble_status(
    request: Request, user: User = Depends(require_auth),
) -> HubbleStatusResponse:
    """Check Hubble Relay health."""
    k8s = request.app.state.k8s_client

    agent = await _find_cilium_agent_pod(k8s)
    if not agent:
        return HubbleStatusResponse(
            available=False,
            message="Cilium agent pod not found in any known namespace",
        )

    ns, pod_name = agent

    try:
        output = await _exec_in_pod(
            k8s, ns, pod_name,
            ["sh", "-c", "hubble status --output json"],
        )
        status_data = json.loads(output)
        return HubbleStatusResponse(
            available=True,
            namespace=ns,
            pod_name=pod_name,
            num_connected_nodes=status_data.get("num_connected_nodes", {}).get("value", 0),
            max_flows=status_data.get("max_flows", {}).get("value", 0),
            message="OK",
        )
    except Exception as e:
        return HubbleStatusResponse(
            available=True,
            namespace=ns,
            pod_name=pod_name,
            message=f"Cilium agent pod found but hubble status check failed: {e}",
        )
