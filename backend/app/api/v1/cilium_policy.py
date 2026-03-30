"""CiliumNetworkPolicy CRUD API endpoints.

Manages CiliumNetworkPolicy CRDs (cilium.io/v2) with template support.
"""

import logging
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth
from app.core.errors import k8s_error_to_http

logger = logging.getLogger(__name__)
router = APIRouter()

CILIUM_GROUP = "cilium.io"
CILIUM_VERSION = "v2"
CNP_PLURAL = "ciliumnetworkpolicies"

MANAGED_LABEL = "kubevirt-ui.io/managed"


# ============================================================================
# Models
# ============================================================================

class CiliumPolicyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=63)
    namespace: str = Field(..., min_length=1)
    template: str | None = Field(None, description="dns-allow, http-filter, block-egress, or None for custom")
    # Template params
    allowed_fqdns: list[str] = Field(default_factory=list, description="For dns-allow template")
    allowed_http_methods: list[str] = Field(default_factory=list, description="For http-filter template")
    allowed_http_paths: list[str] = Field(default_factory=list, description="For http-filter template")
    # Custom spec
    custom_spec: dict | None = Field(None, description="Raw CNP spec for advanced users")


class CiliumPolicyResponse(BaseModel):
    name: str
    namespace: str
    spec: dict = {}
    status: dict | None = None
    ready: bool = False
    yaml_repr: str = ""


class CiliumPolicyListResponse(BaseModel):
    items: list[CiliumPolicyResponse] = []
    total: int = 0


# ============================================================================
# Template Generators
# ============================================================================

def _generate_dns_allow_spec(fqdns: list[str]) -> dict[str, Any]:
    """Generate CiliumNetworkPolicy spec for DNS-allow template."""
    to_fqdns = []
    for fqdn in fqdns:
        if "*" in fqdn:
            to_fqdns.append({"matchPattern": fqdn})
        else:
            to_fqdns.append({"matchName": fqdn})

    return {
        "endpointSelector": {},
        "egress": [
            {"toFQDNs": to_fqdns},
            {
                "toEndpoints": [
                    {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system"}},
                ],
                "toPorts": [
                    {
                        "ports": [{"port": "53", "protocol": "UDP"}],
                        "rules": {"dns": [{"matchPattern": "*"}]},
                    },
                ],
            },
        ],
    }


def _generate_block_egress_spec() -> dict[str, Any]:
    """Generate CiliumNetworkPolicy spec for block-egress template."""
    return {
        "endpointSelector": {},
        "egressDeny": [
            {"toCIDR": ["0.0.0.0/0"]},
        ],
    }


def _generate_http_filter_spec(methods: list[str], paths: list[str]) -> dict[str, Any]:
    """Generate CiliumNetworkPolicy spec for HTTP filter template."""
    http_rules = []
    for method in (methods or ["GET"]):
        for path in (paths or ["/"]):
            http_rules.append({"method": method, "path": path})

    return {
        "endpointSelector": {},
        "egress": [
            {
                "toPorts": [
                    {
                        "ports": [{"port": "80", "protocol": "TCP"}],
                        "rules": {"http": http_rules},
                    },
                ],
            },
        ],
    }


def _generate_spec_from_template(data: CiliumPolicyCreateRequest) -> dict[str, Any]:
    """Generate CNP spec from template name and params."""
    if data.template == "dns-allow":
        if not data.allowed_fqdns:
            raise HTTPException(status_code=422, detail="dns-allow template requires allowed_fqdns")
        return _generate_dns_allow_spec(data.allowed_fqdns)
    elif data.template == "block-egress":
        return _generate_block_egress_spec()
    elif data.template == "http-filter":
        return _generate_http_filter_spec(data.allowed_http_methods, data.allowed_http_paths)
    else:
        raise HTTPException(status_code=422, detail=f"Unknown template: {data.template}")


# ============================================================================
# Helpers
# ============================================================================

def _parse_cnp(item: dict[str, Any]) -> CiliumPolicyResponse:
    """Parse a CiliumNetworkPolicy CR into response model."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})

    # Check readiness from status
    nodes = status.get("nodes", {})
    ready = bool(nodes) and all(
        node_status.get("enforcing", False)
        for node_status in nodes.values()
        if isinstance(node_status, dict)
    )

    # Render YAML for display
    try:
        yaml_repr = yaml.dump(spec, default_flow_style=False)
    except Exception:
        yaml_repr = ""

    return CiliumPolicyResponse(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace", ""),
        spec=spec,
        status=status if status else None,
        ready=ready,
        yaml_repr=yaml_repr,
    )


# ============================================================================
# Endpoints
# ============================================================================

@router.get("", response_model=CiliumPolicyListResponse)
async def list_cilium_policies(
    request: Request,
    namespace: str | None = Query(None, description="Filter by namespace"),
    user: User = Depends(require_auth),
) -> CiliumPolicyListResponse:
    """List CiliumNetworkPolicies, optionally filtered by namespace."""
    k8s = request.app.state.k8s_client

    try:
        if namespace:
            result = await k8s.custom_api.list_namespaced_custom_object(
                group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CNP_PLURAL,
                namespace=namespace,
            )
        else:
            result = await k8s.custom_api.list_cluster_custom_object(
                group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CNP_PLURAL,
            )
    except ApiException as e:
        if e.status == 404:
            return CiliumPolicyListResponse(items=[], total=0)
        raise k8s_error_to_http(e, "cilium policy operation")

    items = [_parse_cnp(item) for item in result.get("items", [])]
    return CiliumPolicyListResponse(items=items, total=len(items))


@router.get("/{namespace}/{name}", response_model=CiliumPolicyResponse)
async def get_cilium_policy(
    request: Request, namespace: str, name: str,
    user: User = Depends(require_auth),
) -> CiliumPolicyResponse:
    """Get a CiliumNetworkPolicy by namespace and name."""
    k8s = request.app.state.k8s_client

    try:
        item = await k8s.custom_api.get_namespaced_custom_object(
            group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CNP_PLURAL,
            namespace=namespace, name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"CiliumNetworkPolicy '{name}' not found in '{namespace}'",
            )
        raise k8s_error_to_http(e, "cilium policy operation")

    return _parse_cnp(item)


@router.post("", response_model=CiliumPolicyResponse, status_code=201)
async def create_cilium_policy(
    request: Request, data: CiliumPolicyCreateRequest,
    user: User = Depends(require_auth),
) -> CiliumPolicyResponse:
    """Create a CiliumNetworkPolicy from template or custom spec."""
    k8s = request.app.state.k8s_client

    if data.template:
        spec = _generate_spec_from_template(data)
    elif data.custom_spec:
        spec = data.custom_spec
    else:
        raise HTTPException(
            status_code=422,
            detail="Either template or custom_spec must be provided",
        )

    manifest: dict[str, Any] = {
        "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
        "kind": "CiliumNetworkPolicy",
        "metadata": {
            "name": data.name,
            "namespace": data.namespace,
            "labels": {
                MANAGED_LABEL: "true",
                "kubevirt-ui.io/template": data.template or "custom",
            },
        },
        "spec": spec,
    }

    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CNP_PLURAL,
            namespace=data.namespace, body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409,
                detail=f"CiliumNetworkPolicy '{data.name}' already exists in '{data.namespace}'",
            )
        raise k8s_error_to_http(e, "cilium policy operation")

    try:
        yaml_repr = yaml.dump(spec, default_flow_style=False)
    except Exception:
        yaml_repr = ""

    return CiliumPolicyResponse(
        name=data.name, namespace=data.namespace,
        spec=spec, ready=False, yaml_repr=yaml_repr,
    )


@router.delete("/{namespace}/{name}")
async def delete_cilium_policy(
    request: Request, namespace: str, name: str,
    user: User = Depends(require_auth),
) -> dict:
    """Delete a CiliumNetworkPolicy."""
    k8s = request.app.state.k8s_client

    try:
        await k8s.custom_api.delete_namespaced_custom_object(
            group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CNP_PLURAL,
            namespace=namespace, name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"CiliumNetworkPolicy '{name}' not found in '{namespace}'",
            )
        raise k8s_error_to_http(e, "cilium policy operation")

    return {"status": "deleted", "name": name, "namespace": namespace}
