"""VM network interface operations: list, add (hotplug), remove."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request models ────────────────────────────────────────────────────────────

class AddNICRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63,
                      description="Interface name")
    network_name: str = Field(..., description="NetworkAttachmentDefinition name (namespace/name or just name)")
    binding: str = Field("bridge", description="Interface binding type: bridge or sriov")
    mac_address: str | None = Field(None, description="Optional MAC address")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/{name}/interfaces", status_code=status.HTTP_200_OK)
async def list_vm_interfaces(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_auth),
) -> list[dict[str, Any]]:
    """List network interfaces on a VM, including runtime info from VMI."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
        )

        # Get runtime info from VMI (if running)
        vmi_interfaces = {}
        try:
            vmi = await custom_api.get_namespaced_custom_object(
                group="kubevirt.io", version="v1", namespace=namespace,
                plural="virtualmachineinstances", name=name,
            )
            for iface in vmi.get("status", {}).get("interfaces", []):
                vmi_interfaces[iface.get("name", "")] = {
                    "ip_address": iface.get("ipAddress"),
                    "ip_addresses": iface.get("ipAddresses", []),
                    "mac": iface.get("mac"),
                    "interface_name": iface.get("interfaceName"),
                }
        except ApiException as e:
            if e.status != 404:
                raise

        # Get Kube-OVN IPs from virt-launcher pod annotations (fallback)
        pod_ips: dict[str, str] = {}  # provider_key -> ip
        try:
            core_api = client.CoreV1Api(k8s_client._api_client)
            pods = await core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"kubevirt.io/domain={name}",
            )
            for pod in pods.items:
                annotations = pod.metadata.annotations or {}
                for key, val in annotations.items():
                    if key.endswith(".ovn.kubernetes.io/ip_address"):
                        # key like "vlan111.test-dev.ovn.kubernetes.io/ip_address"
                        # extract provider prefix to match with NAD name
                        provider = key.replace(".kubernetes.io/ip_address", "").replace(".ovn", "")
                        pod_ips[provider] = val
                        # Also store the raw key for simpler fallback
                        pod_ips["_any"] = val
        except Exception:
            pass

        spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        interfaces = spec.get("domain", {}).get("devices", {}).get("interfaces", [])
        networks = spec.get("networks", [])

        # Build network lookup
        net_lookup = {}
        for net in networks:
            net_name = net.get("name", "")
            if "pod" in net:
                net_lookup[net_name] = {"type": "pod", "network": "Pod Network"}
            elif "multus" in net:
                nad = net["multus"].get("networkName", "")
                net_lookup[net_name] = {"type": "multus", "network": nad, "default": net["multus"].get("default", False)}

        result = []
        for iface in interfaces:
            iface_name = iface.get("name", "")
            binding = "masquerade" if "masquerade" in iface else "bridge" if "bridge" in iface else "sriov" if "sriov" in iface else "unknown"
            net_info = net_lookup.get(iface_name, {"type": "unknown", "network": "?"})
            runtime = vmi_interfaces.get(iface_name, {})

            state = iface.get("state")

            # IP: prefer guest agent, fall back to Kube-OVN pod annotation
            ip_addr = runtime.get("ip_address")
            ip_addrs = runtime.get("ip_addresses", [])
            if not ip_addr and pod_ips:
                # Try to match by NAD provider (e.g. "vlan111.test-dev")
                nad_name = net_info.get("network", "")
                # NAD ref is "namespace/name", provider key is "name.namespace"
                if "/" in nad_name:
                    ns_part, name_part = nad_name.split("/", 1)
                    provider_key = f"{name_part}.{ns_part}"
                    ip_addr = pod_ips.get(provider_key)
                if not ip_addr:
                    ip_addr = pod_ips.get("_any")
                if ip_addr:
                    ip_addrs = [ip_addr]

            result.append({
                "name": iface_name,
                "binding": binding,
                "network_type": net_info["type"],
                "network_name": net_info.get("network", ""),
                "is_default": net_info.get("default", False) or net_info["type"] == "pod",
                "mac": iface.get("macAddress") or runtime.get("mac"),
                "ip_address": ip_addr,
                "ip_addresses": ip_addrs,
                "interface_name": runtime.get("interface_name"),
                "state": state,
                "hotplugged": state is not None,
            })

        return result

    except ApiException as e:
        logger.error(f"Failed to list VM interfaces: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list VM interfaces: {e.reason}",
        )


@router.post("/{name}/interfaces", status_code=status.HTTP_200_OK)
async def add_vm_interface(
    request: Request,
    namespace: str,
    name: str,
    nic_request: AddNICRequest,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Hotplug a new network interface to a VM."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # 1. Get current VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
        )

        spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        interfaces = spec.get("domain", {}).get("devices", {}).get("interfaces", [])
        networks = spec.get("networks", [])

        # Check name collision
        existing_names = {i.get("name") for i in interfaces}
        if nic_request.name in existing_names:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Interface '{nic_request.name}' already exists on this VM",
            )

        # 2. Build new interface + network
        new_interface: dict[str, Any] = {"name": nic_request.name}
        if nic_request.binding == "bridge":
            new_interface["bridge"] = {}
        elif nic_request.binding == "sriov":
            new_interface["sriov"] = {}
        else:
            new_interface["bridge"] = {}

        if nic_request.mac_address:
            new_interface["macAddress"] = nic_request.mac_address

        new_network: dict[str, Any] = {
            "name": nic_request.name,
            "multus": {"networkName": nic_request.network_name},
        }

        # 3. Patch VM
        interfaces.append(new_interface)
        networks.append(new_network)

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "interfaces": interfaces,
                            },
                        },
                        "networks": networks,
                    },
                },
            },
        }

        await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
            body=patch_body,
            _content_type="application/merge-patch+json",
        )

        return {
            "status": "added",
            "interface": nic_request.name,
            "network": nic_request.network_name,
            "binding": nic_request.binding,
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to add NIC: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add NIC: {e.reason}",
        )


@router.delete("/{name}/interfaces/{iface_name}", status_code=status.HTTP_200_OK)
async def remove_vm_interface(
    request: Request,
    namespace: str,
    name: str,
    iface_name: str,
    user: User = Depends(require_auth),
) -> dict[str, Any]:
    """Remove a hotplugged network interface from a VM.

    Marks the interface state as 'absent' so KubeVirt removes it from the running VMI.
    Only hotplugged (non-default) interfaces can be removed.
    """
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # 1. Get current VM
        vm = await custom_api.get_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
        )

        spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        interfaces = spec.get("domain", {}).get("devices", {}).get("interfaces", [])
        networks = spec.get("networks", [])

        # Find the interface
        iface_idx = None
        for idx, iface in enumerate(interfaces):
            if iface.get("name") == iface_name:
                iface_idx = idx
                break

        if iface_idx is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Interface '{iface_name}' not found on VM",
            )

        # Check if this is a default/pod network (cannot remove)
        for net in networks:
            if net.get("name") == iface_name and "pod" in net:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot remove the default pod network interface",
                )

        # 2. Mark interface as absent
        interfaces[iface_idx]["state"] = "absent"

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "interfaces": interfaces,
                            },
                        },
                    },
                },
            },
        }

        await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace,
            plural="virtualmachines", name=name,
            body=patch_body,
            _content_type="application/merge-patch+json",
        )

        return {
            "status": "removing",
            "interface": iface_name,
            "message": f"Interface '{iface_name}' marked for removal. "
                       "It will be detached from the running VM.",
        }

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to remove NIC: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove NIC: {e.reason}",
        )
