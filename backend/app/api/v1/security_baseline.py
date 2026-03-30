"""Security Baseline API endpoints.

Manages CiliumClusterwideNetworkPolicy CRDs (cilium.io/v2) — global security rules
applied across all namespaces.
"""

import logging
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth
from app.core.errors import k8s_error_to_http

logger = logging.getLogger(__name__)
router = APIRouter()

CILIUM_GROUP = "cilium.io"
CILIUM_VERSION = "v2"
CCNP_PLURAL = "ciliumclusterwidenetworkpolicies"

BASELINE_LABEL = "kubevirt-ui.io/security-baseline"
MANAGED_LABEL = "kubevirt-ui.io/managed"


# ============================================================================
# Models
# ============================================================================

class SecurityBaselineCreateRequest(BaseModel):
    preset: str = Field("custom", description="Preset name or 'custom' for custom spec")
    name: str | None = Field(None, description="Custom policy name (auto-generated if None)")
    description: str = Field("", description="Custom policy description")
    custom_spec: dict | None = Field(None, description="Custom CCNP spec (required when preset='custom')")


class SecurityBaselineResponse(BaseModel):
    name: str
    preset: str = ""
    description: str = ""
    spec: dict = {}
    status: dict | None = None
    enabled: bool = True
    yaml_repr: str = ""


class SecurityBaselineListResponse(BaseModel):
    items: list[SecurityBaselineResponse] = []
    total: int = 0
    available_presets: list[dict[str, str]] = []


# ============================================================================
# Presets
# ============================================================================

PRESETS: dict[str, dict[str, Any]] = {
    "default-deny-external": {
        "description": "Block all egress to external networks (allow RFC1918 only)",
        "spec": {
            "endpointSelector": {},
            "egressDeny": [
                {
                    "toCIDR": ["0.0.0.0/0"],
                    "exceptCIDR": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
                },
            ],
        },
    },
    "allow-dns": {
        "description": "Allow DNS queries to kube-system",
        "spec": {
            "endpointSelector": {},
            "egress": [
                {
                    "toEndpoints": [
                        {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system"}},
                    ],
                    "toPorts": [
                        {
                            "ports": [
                                {"port": "53", "protocol": "UDP"},
                                {"port": "53", "protocol": "TCP"},
                            ],
                        },
                    ],
                },
            ],
        },
    },
    "block-metadata-api": {
        "description": "Block access to cloud metadata API (169.254.169.254)",
        "spec": {
            "endpointSelector": {},
            "egressDeny": [
                {"toCIDR": ["169.254.169.254/32"]},
            ],
        },
    },
    "allow-monitoring": {
        "description": "Allow ingress from monitoring namespace on port 9090",
        "spec": {
            "endpointSelector": {},
            "ingress": [
                {
                    "fromEndpoints": [
                        {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "monitoring"}},
                    ],
                    "toPorts": [
                        {
                            "ports": [{"port": "9090", "protocol": "TCP"}],
                        },
                    ],
                },
            ],
        },
    },
}


def _available_presets() -> list[dict[str, str]]:
    """Return list of available presets for the UI."""
    return [
        {"name": name, "description": info["description"]}
        for name, info in PRESETS.items()
    ]


# ============================================================================
# Helpers
# ============================================================================

def _parse_ccnp(item: dict[str, Any]) -> SecurityBaselineResponse:
    """Parse a CiliumClusterwideNetworkPolicy CR into response model."""
    metadata = item.get("metadata", {})
    labels = metadata.get("labels", {})
    spec = item.get("spec", {})
    status = item.get("status", {})

    preset = labels.get("kubevirt-ui.io/preset", "custom")
    description = PRESETS.get(preset, {}).get("description", "")

    try:
        yaml_repr = yaml.dump(spec, default_flow_style=False)
    except Exception:
        yaml_repr = ""

    return SecurityBaselineResponse(
        name=metadata.get("name", ""),
        preset=preset,
        description=description,
        spec=spec,
        status=status if status else None,
        enabled=True,
        yaml_repr=yaml_repr,
    )


# ============================================================================
# Endpoints
# ============================================================================

@router.get("", response_model=SecurityBaselineListResponse)
async def list_security_baselines(
    request: Request, user: User = Depends(require_auth),
) -> SecurityBaselineListResponse:
    """List all security baseline policies (CCNP with our label)."""
    k8s = request.app.state.k8s_client

    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CCNP_PLURAL,
            label_selector=f"{BASELINE_LABEL}=true",
        )
    except ApiException as e:
        if e.status == 404:
            return SecurityBaselineListResponse(
                items=[], total=0, available_presets=_available_presets(),
            )
        raise k8s_error_to_http(e, "security baseline operation")

    items = [_parse_ccnp(item) for item in result.get("items", [])]
    return SecurityBaselineListResponse(
        items=items, total=len(items),
        available_presets=_available_presets(),
    )


@router.post("", response_model=SecurityBaselineResponse, status_code=201)
async def create_security_baseline(
    request: Request, data: SecurityBaselineCreateRequest,
    user: User = Depends(require_auth),
) -> SecurityBaselineResponse:
    """Create a security baseline policy from a preset."""
    k8s = request.app.state.k8s_client

    if data.preset == "custom":
        if not data.custom_spec:
            raise HTTPException(status_code=422, detail="custom_spec is required when preset='custom'")
        if not data.name:
            raise HTTPException(status_code=422, detail="name is required for custom policies")
        spec = data.custom_spec
        policy_name = f"baseline-{data.name}"
        preset_info = {"description": data.description or "Custom cluster-wide policy"}
    elif data.preset not in PRESETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset '{data.preset}'. Available: {list(PRESETS.keys()) + ['custom']}",
        )
    else:
        preset_info = PRESETS[data.preset]
        spec = preset_info["spec"]
        policy_name = f"baseline-{data.preset}"

    manifest: dict[str, Any] = {
        "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
        "kind": "CiliumClusterwideNetworkPolicy",
        "metadata": {
            "name": policy_name,
            "labels": {
                MANAGED_LABEL: "true",
                BASELINE_LABEL: "true",
                "kubevirt-ui.io/preset": data.preset,
            },
        },
        "spec": spec,
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CCNP_PLURAL,
            body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409,
                detail=f"Security baseline '{data.preset}' already exists",
            )
        raise k8s_error_to_http(e, "security baseline operation")

    try:
        yaml_repr = yaml.dump(spec, default_flow_style=False)
    except Exception:
        yaml_repr = ""

    return SecurityBaselineResponse(
        name=policy_name,
        preset=data.preset,
        description=preset_info["description"],
        spec=spec,
        ready=False,
        yaml_repr=yaml_repr,
    )


@router.delete("/{name}")
async def delete_security_baseline(
    request: Request, name: str,
    user: User = Depends(require_auth),
) -> dict:
    """Delete a security baseline policy."""
    k8s = request.app.state.k8s_client

    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=CILIUM_GROUP, version=CILIUM_VERSION, plural=CCNP_PLURAL,
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Security baseline '{name}' not found",
            )
        raise k8s_error_to_http(e, "security baseline operation")

    return {"status": "deleted", "name": name}
