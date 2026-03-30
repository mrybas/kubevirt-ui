"""SecurityGroup Management API endpoints (Kube-OVN).

Covers:
  - CRUD for SecurityGroup CRD (kubeovn.io/v1)
  - Assign/remove SecurityGroup on VMs via pod template annotation
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client import ApiException

from app.core.auth import User, require_auth
from app.core.errors import k8s_error_to_http
from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION, KUBEVIRT_API_GROUP, KUBEVIRT_API_VERSION
from app.models.vpc import (
    SecurityGroupCreateRequest,
    SecurityGroupListResponse,
    SecurityGroupResponse,
    SecurityGroupRule,
    SecurityGroupUpdateRequest,
    VMSecurityGroupAssignRequest,
    VMSecurityGroupsResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

KUBEOVN_GROUP = KUBEOVN_API_GROUP
KUBEOVN_VERSION = KUBEOVN_API_VERSION
KUBEVIRT_GROUP = KUBEVIRT_API_GROUP
KUBEVIRT_VERSION = KUBEVIRT_API_VERSION

# Annotation key for SecurityGroup assignment on pods
SG_ANNOTATION = "ovn.kubernetes.io/security_groups"


# ============================================================================
# Helpers
# ============================================================================

def _parse_sg(item: dict[str, Any]) -> SecurityGroupResponse:
    """Parse a Kube-OVN SecurityGroup CR into SecurityGroupResponse."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})

    def _parse_rules(raw_rules: list[dict]) -> list[SecurityGroupRule]:
        return [
            SecurityGroupRule(
                ipVersion=r.get("ipVersion", "ipv4"),
                protocol=r.get("protocol", "all"),
                policy=r.get("policy", "allow"),
                priority=r.get("priority", 100),
                remoteAddress=r.get("remoteAddress", "0.0.0.0/0"),
                remoteType=r.get("remoteType", "address"),
                portRangeMin=r.get("portRangeMin"),
                portRangeMax=r.get("portRangeMax"),
            )
            for r in raw_rules
        ]

    return SecurityGroupResponse(
        name=metadata.get("name", ""),
        allow_same_group_traffic=spec.get("allowSameGroupTraffic", True),
        ingress_rules=_parse_rules(spec.get("ingressRules", [])),
        egress_rules=_parse_rules(spec.get("egressRules", [])),
    )


def _rules_to_spec(rules: list[SecurityGroupRule]) -> list[dict[str, Any]]:
    """Convert Pydantic rules to Kube-OVN spec format."""
    result = []
    for r in rules:
        entry: dict[str, Any] = {
            "ipVersion": r.ip_version,
            "protocol": r.protocol,
            "policy": r.policy,
            "priority": r.priority,
            "remoteAddress": r.remote_address,
            "remoteType": r.remote_type,
        }
        if r.port_range_min is not None:
            entry["portRangeMin"] = r.port_range_min
        if r.port_range_max is not None:
            entry["portRangeMax"] = r.port_range_max
        result.append(entry)
    return result


# ============================================================================
# SecurityGroup CRUD
# ============================================================================

@router.get("", response_model=SecurityGroupListResponse)
async def list_security_groups(request: Request, user: User = Depends(require_auth)) -> SecurityGroupListResponse:
    """List all SecurityGroups."""
    k8s = request.app.state.k8s_client

    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
        )
    except ApiException as e:
        if e.status == 404:
            return SecurityGroupListResponse(items=[], total=0)
        raise k8s_error_to_http(e, "security group operation")

    items = [_parse_sg(item) for item in result.get("items", [])]
    return SecurityGroupListResponse(items=items, total=len(items))


@router.post("", response_model=SecurityGroupResponse, status_code=201)
async def create_security_group(
    request: Request, data: SecurityGroupCreateRequest,
    user: User = Depends(require_auth),
) -> SecurityGroupResponse:
    """Create a SecurityGroup."""
    logger.info(f"Creating SecurityGroup: {data.name} with {len(data.ingress_rules)} ingress, {len(data.egress_rules)} egress rules")
    k8s = request.app.state.k8s_client

    manifest: dict[str, Any] = {
        "apiVersion": f"{KUBEOVN_GROUP}/{KUBEOVN_VERSION}",
        "kind": "SecurityGroup",
        "metadata": {
            "name": data.name,
            "labels": {"kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "allowSameGroupTraffic": data.allow_same_group_traffic,
            "ingressRules": _rules_to_spec(data.ingress_rules),
            "egressRules": _rules_to_spec(data.egress_rules),
        },
    }

    try:
        await k8s.custom_api.create_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
            body=manifest,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409,
                detail=f"SecurityGroup '{data.name}' already exists",
            )
        raise k8s_error_to_http(e, "security group operation")

    return SecurityGroupResponse(
        name=data.name,
        allow_same_group_traffic=data.allow_same_group_traffic,
        ingress_rules=data.ingress_rules,
        egress_rules=data.egress_rules,
    )


@router.get("/{name}", response_model=SecurityGroupResponse)
async def get_security_group(request: Request, name: str, user: User = Depends(require_auth)) -> SecurityGroupResponse:
    """Get a SecurityGroup by name."""
    k8s = request.app.state.k8s_client

    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"SecurityGroup '{name}' not found")
        raise k8s_error_to_http(e, "security group operation")

    return _parse_sg(item)


@router.put("/{name}", response_model=SecurityGroupResponse)
async def update_security_group(
    request: Request, name: str, data: SecurityGroupUpdateRequest,
    user: User = Depends(require_auth),
) -> SecurityGroupResponse:
    """Update a SecurityGroup (replace rules)."""
    k8s = request.app.state.k8s_client

    # Read current to merge partial updates
    try:
        item = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"SecurityGroup '{name}' not found")
        raise k8s_error_to_http(e, "security group operation")

    current = _parse_sg(item)
    patch_spec: dict[str, Any] = {}

    if data.allow_same_group_traffic is not None:
        patch_spec["allowSameGroupTraffic"] = data.allow_same_group_traffic
    if data.ingress_rules is not None:
        patch_spec["ingressRules"] = _rules_to_spec(data.ingress_rules)
    if data.egress_rules is not None:
        patch_spec["egressRules"] = _rules_to_spec(data.egress_rules)

    if patch_spec:
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
                name=name, body={"spec": patch_spec},
            )
        except ApiException as e:
            raise k8s_error_to_http(e, "security group operation")

    # Return merged response
    return SecurityGroupResponse(
        name=name,
        allow_same_group_traffic=(
            data.allow_same_group_traffic
            if data.allow_same_group_traffic is not None
            else current.allow_same_group_traffic
        ),
        ingress_rules=data.ingress_rules if data.ingress_rules is not None else current.ingress_rules,
        egress_rules=data.egress_rules if data.egress_rules is not None else current.egress_rules,
    )


@router.delete("/{name}")
async def delete_security_group(request: Request, name: str, user: User = Depends(require_auth)) -> dict:
    """Delete a SecurityGroup."""
    k8s = request.app.state.k8s_client

    try:
        await k8s.custom_api.delete_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"SecurityGroup '{name}' not found")
        raise k8s_error_to_http(e, "security group operation")

    return {"status": "deleted", "name": name}


# ============================================================================
# VM SecurityGroup Assignment
# ============================================================================

@router.get(
    "/vms/{namespace}/{vm_name}",
    response_model=VMSecurityGroupsResponse,
)
async def get_vm_security_groups(
    request: Request, namespace: str, vm_name: str,
    user: User = Depends(require_auth),
) -> VMSecurityGroupsResponse:
    """List SecurityGroups assigned to a VM."""
    k8s = request.app.state.k8s_client

    try:
        vm = await k8s.custom_api.get_namespaced_custom_object(
            group=KUBEVIRT_GROUP, version=KUBEVIRT_VERSION, plural="virtualmachines",
            namespace=namespace, name=vm_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found in '{namespace}'")
        raise k8s_error_to_http(e, "security group operation")

    # SG annotation is on pod template metadata
    pod_annotations = (
        vm.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
    )
    sg_value = pod_annotations.get(SG_ANNOTATION, "")
    sgs = [s.strip() for s in sg_value.split(",") if s.strip()] if sg_value else []

    return VMSecurityGroupsResponse(
        vm_name=vm_name, namespace=namespace, security_groups=sgs,
    )


@router.post("/vms/{namespace}/{vm_name}")
async def assign_security_group_to_vm(
    request: Request,
    namespace: str,
    vm_name: str,
    data: VMSecurityGroupAssignRequest,
    user: User = Depends(require_auth),
) -> VMSecurityGroupsResponse:
    """Assign a SecurityGroup to a VM."""
    k8s = request.app.state.k8s_client

    # Verify SecurityGroup exists
    try:
        await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_GROUP, version=KUBEOVN_VERSION, plural="security-groups",
            name=data.security_group,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"SecurityGroup '{data.security_group}' not found",
            )
        raise k8s_error_to_http(e, "security group operation")

    # Read current VM
    try:
        vm = await k8s.custom_api.get_namespaced_custom_object(
            group=KUBEVIRT_GROUP, version=KUBEVIRT_VERSION, plural="virtualmachines",
            namespace=namespace, name=vm_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found in '{namespace}'")
        raise k8s_error_to_http(e, "security group operation")

    # Get current SGs
    pod_annotations = (
        vm.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
    )
    sg_value = pod_annotations.get(SG_ANNOTATION, "")
    current_sgs = [s.strip() for s in sg_value.split(",") if s.strip()] if sg_value else []

    if data.security_group in current_sgs:
        return VMSecurityGroupsResponse(
            vm_name=vm_name, namespace=namespace, security_groups=current_sgs,
        )

    current_sgs.append(data.security_group)
    new_annotation = ",".join(current_sgs)

    # Patch VM pod template annotation
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {SG_ANNOTATION: new_annotation},
                },
            },
        },
    }

    try:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=KUBEVIRT_GROUP, version=KUBEVIRT_VERSION, plural="virtualmachines",
            namespace=namespace, name=vm_name, body=patch_body,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "security group operation")

    return VMSecurityGroupsResponse(
        vm_name=vm_name, namespace=namespace, security_groups=current_sgs,
    )


@router.delete("/vms/{namespace}/{vm_name}/{sg_name}")
async def remove_security_group_from_vm(
    request: Request, namespace: str, vm_name: str, sg_name: str,
    user: User = Depends(require_auth),
) -> VMSecurityGroupsResponse:
    """Remove a SecurityGroup from a VM."""
    k8s = request.app.state.k8s_client

    # Read current VM
    try:
        vm = await k8s.custom_api.get_namespaced_custom_object(
            group=KUBEVIRT_GROUP, version=KUBEVIRT_VERSION, plural="virtualmachines",
            namespace=namespace, name=vm_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found in '{namespace}'")
        raise k8s_error_to_http(e, "security group operation")

    pod_annotations = (
        vm.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
    )
    sg_value = pod_annotations.get(SG_ANNOTATION, "")
    current_sgs = [s.strip() for s in sg_value.split(",") if s.strip()] if sg_value else []

    if sg_name not in current_sgs:
        raise HTTPException(
            status_code=404,
            detail=f"SecurityGroup '{sg_name}' not assigned to VM '{vm_name}'",
        )

    current_sgs.remove(sg_name)
    new_annotation = ",".join(current_sgs)

    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {SG_ANNOTATION: new_annotation},
                },
            },
        },
    }

    try:
        await k8s.custom_api.patch_namespaced_custom_object(
            group=KUBEVIRT_GROUP, version=KUBEVIRT_VERSION, plural="virtualmachines",
            namespace=namespace, name=vm_name, body=patch_body,
        )
    except ApiException as e:
        raise k8s_error_to_http(e, "security group operation")

    return VMSecurityGroupsResponse(
        vm_name=vm_name, namespace=namespace, security_groups=current_sgs,
    )
