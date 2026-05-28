"""Virtual Machine API endpoints — CRUD, events, YAML."""

import asyncio
import json
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from app.core.auth import User, require_auth, require_env_member, require_env_viewer
from app.core.groups import get_user_namespaces
from app.core.kubevirt import get_hotplug_mode
from app.core.naming import (
    DISPLAY_NAME_ANNOTATION,
    SLUG_LABEL,
    sanitize_display_name,
    with_synthetic_metadata,
)
from app.api.v1.cluster import get_cluster_settings
from app.models.vm import (
    VMCreateRequest,
    VMListResponse,
    VMResponse,
    VMUpdateDisplayNameRequest,
    VMUpdateRequest,
    vm_from_k8s,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _extract_kubeovn_ip(pod: dict[str, Any]) -> str | None:
    """Extract Kube-OVN assigned IP from pod annotations.

    Kube-OVN sets annotations like:
      vlan111.test-dev.ovn.kubernetes.io/ip_address: 192.168.203.106
    or the default:
      ovn.kubernetes.io/ip_address: 10.16.0.5
    """
    annotations = pod.get("metadata", {}).get("annotations", {})
    # Prefer provider-specific annotation (secondary/multus network)
    for key, val in annotations.items():
        if key.endswith(".ovn.kubernetes.io/ip_address") and key != "ovn.kubernetes.io/ip_address":
            return val
    # Fall back to default OVN annotation
    return annotations.get("ovn.kubernetes.io/ip_address")


class VMNetworkRequest(BaseModel):
    """Network configuration for VM creation."""
    
    subnet: str = Field(..., description="Kube-OVN subnet name")
    static_ip: str | None = Field(None, description="Static IP address (optional)")


class VMFromTemplateRequest(BaseModel):
    """Request to create VM from template."""

    display_name: str = Field(..., min_length=1, max_length=100)
    template_name: str

    # Optional overrides
    cpu_cores: int | None = None
    memory: str | None = None
    disk_size: str | None = None

    # Cloud-init
    ssh_key: str | None = None
    password: str | None = None
    user_data: str | None = None

    # Network configuration (Kube-OVN)
    # Single NIC (backward compat)
    network: VMNetworkRequest | None = None
    # Multiple NICs — if set, takes priority over `network`
    networks: list[VMNetworkRequest] | None = None

    # T6 — Primary-NIC binding. Default 'bridge' so guests see their real
    # OVN/Kube-OVN IP (masquerade → QEMU SLIRP hides it behind 10.0.2.x and
    # breaks multicast). Override with 'masquerade' on CNIs where bridge
    # live-migration is iffy.
    network_binding: Literal["bridge", "masquerade"] = "bridge"

    # Start immediately
    start: bool = True


@router.get("", response_model=VMListResponse)
async def list_vms(
    request: Request,
    namespace: str | None = None,
    search: str | None = Query(None, description="Case-insensitive substring filter on display_name"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user: User = Depends(require_auth),
) -> VMListResponse:
    """List VirtualMachines. If namespace is provided, list from that namespace only.
    Otherwise, list from all enabled project namespaces the user can access.

    When ``search`` is supplied, results are filtered in-memory by a
    case-insensitive substring match against the display name (falling back to
    ``metadata.name`` if the display-name annotation is missing). Substring
    semantics support incremental as-you-type filtering on the frontend.
    """
    k8s_client = request.app.state.k8s_client
    vm_cache = request.app.state.vm_cache

    try:
        # Determine which namespaces to query (RBAC-filtered)
        allowed_ns = await get_user_namespaces(k8s_client, user)
        if namespace:
            if namespace not in allowed_ns:
                return VMListResponse(items=[], total=0)
            namespaces_to_query = [namespace]
        else:
            namespaces_to_query = allowed_ns

        vm_responses = []

        # Pre-fetch namespace labels for project/environment enrichment
        ns_labels_map: dict[str, dict[str, str]] = {}
        try:
            all_ns = await k8s_client.list_namespaces()
            for ns_obj in all_ns:
                if ns_obj["name"] in namespaces_to_query:
                    ns_labels_map[ns_obj["name"]] = ns_obj.get("labels", {})
        except Exception:
            pass

        async def _fetch_ns_vms(ns: str) -> list[VMResponse]:
            """Fetch VMs (cached), VMIs, and pod IPs for a single namespace."""
            vms = await vm_cache.list_vms(ns)
            vmis = await k8s_client.list_virtual_machine_instances(namespace=ns)
            vmi_map = {vmi["metadata"]["name"]: vmi for vmi in vmis}

            pod_ip_map: dict[str, str] = {}
            try:
                core_api = k8s_client.core_api
                # vm.kubevirt.io/name is set by KubeVirt automatically on every
                # launcher pod and always equals the parent VM's metadata.name
                # — no client-side stamping required.
                pods = await core_api.list_namespaced_pod(
                    namespace=ns, label_selector="vm.kubevirt.io/name",
                )
                for pod in pods.items:
                    vm_name = pod.metadata.labels.get("vm.kubevirt.io/name", "")
                    ip = _extract_kubeovn_ip(pod.to_dict())
                    if vm_name and ip:
                        pod_ip_map[vm_name] = ip
            except Exception:
                pass

            ns_labels = ns_labels_map.get(ns, {})
            results = []
            for vm in vms:
                name = vm["metadata"]["name"]
                vmi = vmi_map.get(name)
                resp = vm_from_k8s(vm, vmi, pod_ip=pod_ip_map.get(name))
                if not resp.project:
                    resp.project = ns_labels.get("kubevirt-ui.io/project")
                if not resp.environment:
                    resp.environment = ns_labels.get("kubevirt-ui.io/environment")
                results.append(resp)
            return results

        # Fetch all namespaces in parallel
        tasks = []
        for ns in namespaces_to_query:
            tasks.append(_fetch_ns_vms(ns))
        ns_results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(ns_results):
            if isinstance(result, ApiException):
                if result.status != 403:
                    logger.warning(f"Failed to list VMs in namespace {namespaces_to_query[i]}: {result.reason}")
            elif isinstance(result, Exception):
                logger.warning(f"Failed to list VMs in namespace {namespaces_to_query[i]}: {result}")
            else:
                vm_responses.extend(result)

        # Case-insensitive substring filter on display_name. Applied before
        # pagination so total/pages reflect the filtered set.
        if search:
            needle = search.lower()
            vm_responses = [v for v in vm_responses if needle in v.display_name.lower()]

        total = len(vm_responses)
        start = (page - 1) * per_page
        paginated = vm_responses[start:start + per_page]
        return VMListResponse(
            items=paginated,
            total=total,
            page=page,
            per_page=per_page,
            pages=(total + per_page - 1) // per_page,
        )

    except ApiException as e:
        if e.status == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to namespace {namespace}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list VMs: {e.reason}",
        )


@router.get("/hotplug-capabilities", status_code=status.HTTP_200_OK)
async def get_hotplug_capabilities(
    request: Request,
    namespace: str,
    user: User = Depends(require_env_viewer()),
) -> dict[str, Any]:
    """Return hotplug capabilities based on KubeVirt feature gates."""
    k8s_client = request.app.state.k8s_client
    mode = await get_hotplug_mode(k8s_client)
    if mode == "declarative":
        return {
            "hotplug_supported": True,
            "mode": "declarative",
            "supported_bus_types": ["virtio", "scsi", "sata"],
            "restart_required": False,
        }
    elif mode == "legacy":
        return {
            "hotplug_supported": True,
            "mode": "legacy",
            "supported_bus_types": ["scsi"],
            "restart_required": True,
        }
    else:
        return {
            "hotplug_supported": False,
            "mode": "none",
            "supported_bus_types": [],
            "restart_required": True,
        }


@router.get("/{name}", response_model=VMResponse)
async def get_vm(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_env_viewer()),
) -> VMResponse:
    """Get a specific VirtualMachine."""
    k8s_client = request.app.state.k8s_client

    try:
        vm = await k8s_client.get_virtual_machine(name=name, namespace=namespace)

        # Try to get VMI for runtime status
        vmi = None
        try:
            vmi = await k8s_client.get_virtual_machine_instance(
                name=name, namespace=namespace
            )
        except ApiException:
            pass  # VMI might not exist if VM is stopped

        # Get Kube-OVN IP from virt-launcher pod (fallback when guest agent absent)
        pod_ip = None
        try:
            core_api = k8s_client.core_api
            pods = await core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"vm.kubevirt.io/name={name}",
            )
            for pod in pods.items:
                ip = _extract_kubeovn_ip(pod.to_dict())
                if ip:
                    pod_ip = ip
                    break
        except Exception:
            pass

        return vm_from_k8s(vm, vmi, pod_ip=pod_ip)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found in namespace {namespace}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get VM: {e.reason}",
        )


class VMEvent(BaseModel):
    """A Kubernetes event related to a VM."""
    type: str  # Normal or Warning
    reason: str
    message: str
    source: str  # e.g. "VirtualMachine", "VirtualMachineInstance", "Pod"
    source_name: str
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    count: int = 1


class VMEventsResponse(BaseModel):
    """Response with VM-related events."""
    items: list[VMEvent]


@router.get("/{name}/events", response_model=VMEventsResponse)
async def get_vm_events(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_env_viewer()),
) -> VMEventsResponse:
    """Get Kubernetes events related to a VM (VM, VMI, and launcher pods)."""
    k8s_client = request.app.state.k8s_client

    try:
        events_api = k8s_client.core_api
        all_events = await events_api.list_namespaced_event(namespace=namespace)

        # Collect events for VM, VMI, and virt-launcher pods
        relevant_names = {name, f"{name}"}
        vm_events: list[VMEvent] = []

        for event in all_events.items:
            involved = event.involved_object
            if not involved or not involved.name:
                continue

            kind = involved.kind or ""
            obj_name = involved.name

            is_relevant = False
            if kind in ("VirtualMachine", "VirtualMachineInstance") and obj_name == name:
                is_relevant = True
            elif kind == "Pod" and obj_name.startswith(f"virt-launcher-{name}-"):
                is_relevant = True
            elif kind == "DataVolume" and obj_name.startswith(name):
                is_relevant = True

            if not is_relevant:
                continue

            first_ts = event.first_timestamp or event.event_time
            last_ts = event.last_timestamp or event.event_time

            vm_events.append(VMEvent(
                type=event.type or "Normal",
                reason=event.reason or "",
                message=event.message or "",
                source=kind,
                source_name=obj_name,
                first_timestamp=first_ts.isoformat() if first_ts else None,
                last_timestamp=last_ts.isoformat() if last_ts else None,
                count=event.count or 1,
            ))

        # Sort by last_timestamp descending (newest first)
        vm_events.sort(key=lambda e: e.last_timestamp or "", reverse=True)
        return VMEventsResponse(items=vm_events)

    except ApiException as e:
        if e.status == 403:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get VM events: {e.reason}",
        )


@router.put("/{name}", response_model=VMResponse)
async def update_vm(
    request: Request,
    namespace: str,
    name: str,
    update_data: VMUpdateRequest,
    user: User = Depends(require_env_member()),
) -> VMResponse:
    """Update a VirtualMachine's configuration.
    
    Note: CPU and memory changes require the VM to be stopped.
    """
    k8s_client = request.app.state.k8s_client
    
    try:
        # Get the current VM
        vm = await k8s_client.get_virtual_machine(name=name, namespace=namespace)
        vm_status = vm.get("status", {}).get("printableStatus", "Unknown")
        
        # Check if VM needs to be stopped for certain changes
        requires_stop = (
            update_data.cpu_cores is not None or 
            update_data.memory is not None or 
            update_data.console is not None
        )
        is_running = vm_status not in ["Stopped", "Halted", "Failed"]
        
        if requires_stop and is_running:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VM must be stopped to change CPU or memory. Please stop the VM first.",
            )
        
        # Build the patch
        patch: dict[str, Any] = {}
        
        # Update run strategy
        if update_data.run_strategy is not None:
            patch.setdefault("spec", {})["runStrategy"] = update_data.run_strategy
        
        # Update CPU
        if update_data.cpu_cores is not None:
            patch.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {}).setdefault("domain", {}).setdefault("cpu", {})["cores"] = update_data.cpu_cores
            # Also update limits for quota compliance
            patch["spec"]["template"]["spec"]["domain"].setdefault("resources", {}).setdefault("limits", {})["cpu"] = str(update_data.cpu_cores)
        
        # Update memory
        if update_data.memory is not None:
            domain_patch = patch.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {}).setdefault("domain", {})
            domain_patch.setdefault("memory", {})["guest"] = update_data.memory
            domain_patch.setdefault("resources", {}).setdefault("requests", {})["memory"] = update_data.memory
            domain_patch.setdefault("resources", {}).setdefault("limits", {})["memory"] = update_data.memory
        
        # Update console settings
        if update_data.console is not None:
            devices_patch = patch.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {}).setdefault("domain", {}).setdefault("devices", {})
            devices_patch["autoattachGraphicsDevice"] = update_data.console.vnc_enabled
            devices_patch["autoattachSerialConsole"] = update_data.console.serial_console_enabled
        
        # Update labels
        if update_data.labels is not None:
            patch.setdefault("metadata", {})["labels"] = {
                **vm.get("metadata", {}).get("labels", {}),
                **update_data.labels,
            }
        
        # Update annotations
        if update_data.annotations is not None:
            patch.setdefault("metadata", {})["annotations"] = {
                **vm.get("metadata", {}).get("annotations", {}),
                **update_data.annotations,
            }
        
        if not patch:
            # Nothing to update
            vmi = None
            try:
                vmi = await k8s_client.get_virtual_machine_instance(name=name, namespace=namespace)
            except ApiException:
                pass
            return vm_from_k8s(vm, vmi)
        
        # Apply the patch
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        updated_vm = await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
            body=patch,
            _content_type="application/merge-patch+json",
        )
        
        # Get VMI for runtime status
        vmi = None
        try:
            vmi = await k8s_client.get_virtual_machine_instance(name=name, namespace=namespace)
        except ApiException:
            pass
        
        logger.info(f"User {user.username} updated VM {namespace}/{name}")
        return vm_from_k8s(updated_vm, vmi)
    
    except HTTPException:
        raise
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found in namespace {namespace}",
            )
        logger.error(f"Failed to update VM {namespace}/{name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update VM: {e.reason}",
        )


@router.patch("/{name}/display-name", response_model=VMResponse)
async def update_vm_display_name(
    request: Request,
    namespace: str,
    name: str,
    update: VMUpdateDisplayNameRequest,
    user: User = Depends(require_env_member()),
) -> VMResponse:
    """Update only a VM's display-name annotation and slug label.

    K8s resource identity (``metadata.name``) is unchanged.
    """
    k8s_client = request.app.state.k8s_client
    slug = sanitize_display_name(update.display_name)

    patch: dict[str, Any] = {
        "metadata": {
            "annotations": {DISPLAY_NAME_ANNOTATION: update.display_name},
            "labels": {SLUG_LABEL: slug},
        }
    }

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        updated_vm = await custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
            body=patch,
            _content_type="application/merge-patch+json",
        )

        vmi = None
        try:
            vmi = await k8s_client.get_virtual_machine_instance(
                name=name, namespace=namespace
            )
        except ApiException:
            pass

        logger.info(
            f"User {user.username} renamed VM {namespace}/{name} "
            f"display_name={update.display_name!r}"
        )
        return vm_from_k8s(updated_vm, vmi)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found in namespace {namespace}",
            ) from e
        logger.error(f"Failed to update VM display_name {namespace}/{name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update display name: {e.reason}",
        ) from e


@router.post("", response_model=VMResponse, status_code=status.HTTP_201_CREATED)
async def create_vm(
    request: Request, namespace: str, vm_request: VMCreateRequest,
    user: User = Depends(require_env_member()),
) -> VMResponse:
    """Create a new VirtualMachine.

    Uses K8s ``generateName`` for collision-free server-side name assignment.
    Display name is stored in the ``kubevirt-ui.io/display-name`` annotation.
    """
    k8s_client = request.app.state.k8s_client

    # Build VM body (no metadata.name; placeholder DV refs based on slug).
    vm_manifest = vm_request.to_k8s_manifest(namespace)

    # Inject generateName, display-name annotation, slug label.
    with_synthetic_metadata(vm_manifest, vm_request.display_name, namespace=namespace)

    # Stamp owner annotation (preserve display-name annotation set above).
    vm_manifest["metadata"].setdefault("annotations", {})[
        "kubevirt-ui.io/owner"
    ] = user.email or user.username

    try:
        created_vm = await k8s_client.create_virtual_machine(
            namespace=namespace, body=vm_manifest
        )
        actual_name = created_vm.get("metadata", {}).get("name", "")
        logger.info(
            f"User {user.username} created VM {namespace}/{actual_name} "
            f"(display_name={vm_request.display_name!r})"
        )
        return vm_from_k8s(created_vm, None)

    except ApiException as e:
        if e.status == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to create VM",
            )
        logger.error(f"Failed to create VM: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create VM: {e.reason}",
        )


@router.post("/from-template", response_model=VMResponse, status_code=status.HTTP_201_CREATED)
async def create_vm_from_template(
    request: Request,
    namespace: str,
    vm_request: VMFromTemplateRequest,
    user: User = Depends(require_env_member()),
) -> VMResponse:
    """Create a VM from a template using dataVolumeTemplates.
    
    KubeVirt manages the DV lifecycle: creates the DV, waits for it to be ready,
    sets ownerReference, and only then starts the VMI. No polling needed.
    """
    k8s_client = request.app.state.k8s_client
    
    try:
        # 1. Get the template
        try:
            cm = await k8s_client.core_api.read_namespaced_config_map(
                name="kubevirt-ui-templates",
                namespace="kubevirt-ui-system",
            )
            if not cm.data or vm_request.template_name not in cm.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Template {vm_request.template_name} not found",
                )
            template = json.loads(cm.data[vm_request.template_name])
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Templates not configured",
                )
            raise
        
        # 2. Extract template values with overrides
        compute = template.get("compute", {})
        disk_config = template.get("disk", {})
        network_config = template.get("network", {})
        
        cpu_cores = vm_request.cpu_cores or compute.get("cpu_cores", 2)
        vcpu = compute.get("vcpu", cpu_cores)  # vCPUs visible to VM (defaults to cpu_cores)
        memory = vm_request.memory or compute.get("memory", "4Gi")
        disk_size = vm_request.disk_size or disk_config.get("size", "50Gi")
        storage_class = disk_config.get("storage_class")
        
        golden_image_name = template.get("golden_image_name")
        golden_image_namespace = template.get("golden_image_namespace", "golden-images")
        
        if not golden_image_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Template has no golden image configured",
            )
        
        # 3. Build DataVolume spec for dataVolumeTemplates
        # DV name must be literal (volumes[].dataVolume.name references it by exact
        # match), so we can't use K8s generateName for the DV. Use a client-side
        # unique suffix to avoid collisions between VMs sharing the same display name.
        slug = sanitize_display_name(vm_request.display_name)
        disk_name = f"{slug}-root-{uuid.uuid4().hex[:6]}"

        dv_storage: dict[str, Any] = {
            "resources": {
                "requests": {
                    "storage": disk_size,
                }
            },
        }
        if storage_class:
            dv_storage["storageClassName"] = storage_class
        
        # DV → VM ownership is set automatically by KubeVirt via ownerReferences
        # when using dataVolumeTemplates — no client-side label is needed to track
        # which VM owns the DV.
        dv_template = {
            "metadata": {
                "name": disk_name,
                "labels": {
                    "kubevirt-ui.io/managed": "true",
                    "kubevirt-ui.io/vm-disk": "true",
                },
            },
            "spec": {
                "source": {
                    "pvc": {
                        "name": golden_image_name,
                        "namespace": golden_image_namespace,
                    }
                },
                "storage": dv_storage,
            },
        }
        
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        
        # 4. Build cloud-init
        cloud_init_data = None
        template_cloud_init = template.get("cloud_init") or {}
        
        # Collect all SSH keys: from profile + from request
        from app.api.v1.profile import get_user_ssh_keys
        all_ssh_keys: list[str] = []
        
        # User's profile SSH keys (always injected)
        profile_keys = await get_user_ssh_keys(k8s_client, user)
        all_ssh_keys.extend(profile_keys)
        
        # SSH key from request (explicit per-VM key)
        if vm_request.ssh_key:
            all_ssh_keys.append(vm_request.ssh_key)
        
        if vm_request.user_data:
            # If raw user_data provided, append profile keys if any
            cloud_init_data = vm_request.user_data
            if all_ssh_keys and "ssh_authorized_keys" not in cloud_init_data:
                keys_yaml = "\nssh_authorized_keys:\n" + "".join(
                    f"  - {k}\n" for k in all_ssh_keys
                )
                cloud_init_data += keys_yaml
        elif all_ssh_keys or vm_request.password or template_cloud_init.get("user_data"):
            cloud_config = "#cloud-config\n"
            if template_cloud_init.get("user_data"):
                cloud_config = template_cloud_init["user_data"]
            
            # Merge SSH keys (template may already have some in user_data)
            if all_ssh_keys:
                # Check if template already has ssh_authorized_keys section
                if "ssh_authorized_keys:" in cloud_config:
                    # Append keys to existing section
                    for key in all_ssh_keys:
                        cloud_config += f"  - {key}\n"
                else:
                    cloud_config += "\nssh_authorized_keys:\n" + "".join(
                        f"  - {k}\n" for k in all_ssh_keys
                    )
            
            # Add password if provided
            if vm_request.password:
                cloud_config += f"\nchpasswd:\n  expire: false\npassword: {vm_request.password}\n"
            
            cloud_init_data = cloud_config
        
        # 5. Build VM manifest
        # cpu_cores = real cores for scheduler (limits/requests)
        # vcpu = virtual CPUs visible to the VM (domain.cpu.cores)
        sockets = compute.get("cpu_sockets", 1)
        threads = compute.get("cpu_threads", 1)
        total_real_cpus = cpu_cores * sockets * threads
        
        # CPU overcommit: read global cluster setting, applies to real cores
        cluster_settings = await get_cluster_settings(k8s_client.core_api)
        cpu_overcommit = cluster_settings.cpu_overcommit
        if cpu_overcommit and cpu_overcommit > 1:
            cpu_requests = max(total_real_cpus / cpu_overcommit, 0.1)
            cpu_request_str = f"{int(cpu_requests * 1000)}m"
        else:
            cpu_request_str = str(total_real_cpus)
        
        # Get console settings from template
        console_config = template.get("console", {})
        vnc_enabled = console_config.get("vnc_enabled", True)
        serial_console_enabled = console_config.get("serial_console_enabled", False)
        
        vm_spec = {
            "domain": {
                "cpu": {
                    "cores": vcpu,
                    "sockets": sockets,
                    "threads": threads,
                },
                "memory": {
                    "guest": memory,
                },
                "resources": {
                    "requests": {
                        "cpu": cpu_request_str,
                        "memory": memory,
                    },
                    "limits": {
                        "cpu": str(total_real_cpus),
                        "memory": memory,
                    },
                },
                "devices": {
                    "disks": [
                        {
                            "name": "rootdisk",
                            "disk": {"bus": "virtio"},
                        }
                    ],
                    "interfaces": [
                        {"name": "default", "masquerade": {}}
                    ],
                    # Console settings
                    "autoattachGraphicsDevice": vnc_enabled,
                    "autoattachSerialConsole": serial_console_enabled,
                },
            },
            "networks": [
                {"name": "default", "pod": {}}
            ],
            "volumes": [
                {
                    "name": "rootdisk",
                    "dataVolume": {"name": disk_name},
                }
            ],
        }
        
        # Add cloud-init volume if configured
        if cloud_init_data:
            vm_spec["domain"]["devices"]["disks"].append({
                "name": "cloudinit",
                "disk": {"bus": "virtio"},
            })
            vm_spec["volumes"].append({
                "name": "cloudinit",
                "cloudInitNoCloud": {
                    "userData": cloud_init_data,
                },
            })
        
        # Handle network configuration
        # Priority: request.networks (multi-NIC) > request.network (single, backward compat) > template multus > default pod network
        nic_list: list[VMNetworkRequest] = []
        if vm_request.networks:
            nic_list = vm_request.networks
        elif vm_request.network:
            nic_list = [vm_request.network]

        template_annotations: dict[str, str] = {}

        if nic_list:
            net_specs: list[dict[str, Any]] = []
            iface_specs: list[dict[str, Any]] = []
            static_ips: list[str] = []
            has_bridge = False  # tracks whether any NIC uses bridge binding

            for idx, nic in enumerate(nic_list):
                subnet_name = nic.subnet
                # Look up Subnet CR to decide how to attach this NIC:
                #   - spec.vlan present → VLAN-backed underlay → multus + bridge (existing path)
                #   - spec.vlan absent  → VPC overlay         → default pod network + masquerade +
                #                                                ovn.kubernetes.io/logical_switch annotation
                # The VPC pattern mirrors tenants_capi._build_kubevirt_machine_template_cr.
                vlan_name: str | None = None
                try:
                    subnet_obj = await custom_api.get_cluster_custom_object(
                        group="kubeovn.io",
                        version="v1",
                        plural="subnets",
                        name=subnet_name,
                    )
                    vlan_name = subnet_obj.get("spec", {}).get("vlan") or None
                except Exception:
                    # Failed lookup → assume the caller knows; treat as VLAN
                    # with subnet_name as the NAD name (legacy behavior).
                    vlan_name = subnet_name

                if vlan_name:
                    # VLAN-backed underlay: multus + bridge binding.
                    iface_name = vlan_name if idx == 0 else f"{vlan_name}-{idx}"
                    nad_ref = f"{namespace}/{vlan_name}"
                    multus_entry: dict[str, Any] = {"networkName": nad_ref}
                    if idx == 0:
                        multus_entry["default"] = True
                    net_specs.append({"name": iface_name, "multus": multus_entry})
                    iface_specs.append({"name": iface_name, "bridge": {}})
                    has_bridge = True
                else:
                    # VPC overlay: default pod network. Bind defaults to
                    # `bridge` (T6) so the guest reports its real OVN IP;
                    # masquerade is the legacy opt-out. The logical_switch
                    # annotation (stamped below) steers the primary OVN
                    # interface to this subnet's logical switch.
                    # Secondary VPC NICs would need per-subnet NADs wrapping
                    # the OVN CNI — out of scope for now.
                    if idx != 0:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                "VPC subnets can only be the primary NIC. Use "
                                "VLAN-backed subnets for additional NICs."
                            ),
                        )
                    iface_name = "default"
                    net_specs.append({"name": iface_name, "pod": {}})
                    vpc_binding = (
                        "masquerade"
                        if vm_request.network_binding == "masquerade"
                        else "bridge"
                    )
                    iface_specs.append({"name": iface_name, vpc_binding: {}})
                    if vpc_binding == "bridge":
                        has_bridge = True

                if nic.static_ip:
                    static_ips.append(nic.static_ip)

            vm_spec["networks"] = net_specs
            vm_spec["domain"]["devices"]["interfaces"] = iface_specs

            # Live-migration annotation is only required when a bridge-bound
            # interface is in play; pure pod-network VMs migrate fine without it.
            if has_bridge:
                template_annotations["kubevirt.io/allow-pod-bridge-network-live-migration"] = "true"

            # Kube-OVN: set logical_switch for the primary NIC. For VPC overlays
            # this is what attaches the pod to the right logical switch; for
            # VLAN-backed primaries it also gives DHCP/IPAM scoping. Works for both.
            first_subnet = nic_list[0].subnet
            template_annotations["ovn.kubernetes.io/logical_switch"] = first_subnet

            # Static IPs via Kube-OVN annotation (comma-separated for multi-NIC)
            if static_ips:
                template_annotations["ovn.kubernetes.io/ip_address"] = ",".join(static_ips)

        else:
            # Check template network config
            network_type = network_config.get("type", "default")
            if network_type == "multus" and network_config.get("multus_network"):
                vm_spec["networks"] = [
                    {"name": "default", "multus": {"networkName": network_config["multus_network"]}}
                ]
                vm_spec["domain"]["devices"]["interfaces"] = [
                    {"name": "default", "bridge": {}}
                ]
        
        # Build template metadata. We deliberately do NOT set
        # ``kubevirt.io/domain`` — KubeVirt stamps each launcher pod with
        # ``vm.kubevirt.io/name=<actual_vm_name>`` automatically, which is what
        # list_vms/get_vm use to find pods.
        template_metadata: dict[str, Any] = {}
        if template_annotations:
            template_metadata["annotations"] = template_annotations

        # 6. Build and create VM manifest with dataVolumeTemplates
        # KubeVirt manages the DV lifecycle: creates DV, waits for ready, sets ownerRef

        # Read namespace labels for project/environment. The ``app`` label uses
        # the slug — it's informational only (k8s-style "app" grouping), not a
        # lookup key for anything that needs the actual VM name.
        vm_labels: dict[str, str] = {
            "app": slug,
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/template": vm_request.template_name,
        }
        try:
            ns_obj = await k8s_client.core_api.read_namespace(name=namespace)
            ns_labels = ns_obj.metadata.labels or {}
            if ns_labels.get("kubevirt-ui.io/project"):
                vm_labels["kubevirt-ui.io/project"] = ns_labels["kubevirt-ui.io/project"]
            if ns_labels.get("kubevirt-ui.io/environment"):
                vm_labels["kubevirt-ui.io/environment"] = ns_labels["kubevirt-ui.io/environment"]
        except Exception:
            pass

        vm_manifest: dict[str, Any] = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "namespace": namespace,
                "labels": vm_labels,
                "annotations": {
                    "kubevirt-ui.io/owner": user.email or user.username,
                },
            },
            "spec": {
                "runStrategy": "Always" if vm_request.start else "Halted",
                "dataVolumeTemplates": [dv_template],
                "template": {
                    "metadata": template_metadata,
                    "spec": vm_spec,
                },
            },
        }

        # Inject generateName + display-name annotation + slug label.
        with_synthetic_metadata(vm_manifest, vm_request.display_name, namespace=namespace)

        created_vm = await custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=vm_manifest,
        )
        actual_name = created_vm.get("metadata", {}).get("name", "")

        logger.info(
            f"User {user.username} created VM from template {namespace}/{actual_name} "
            f"(display_name={vm_request.display_name!r}, template={vm_request.template_name})"
        )
        return vm_from_k8s(created_vm, None)

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to create VM from template: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create VM from template: {e.reason}",
        )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vm(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_env_member()),
) -> None:
    """Delete a VirtualMachine and its associated snapshots."""
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        # 1. Delete associated VirtualMachineSnapshots
        try:
            snapshots = await custom_api.list_namespaced_custom_object(
                group="snapshot.kubevirt.io", version="v1beta1",
                namespace=namespace, plural="virtualmachinesnapshots",
            )
            for snap in snapshots.get("items", []):
                source = snap.get("spec", {}).get("source", {})
                if source.get("kind") == "VirtualMachine" and source.get("name") == name:
                    snap_name = snap["metadata"]["name"]
                    try:
                        await custom_api.delete_namespaced_custom_object(
                            group="snapshot.kubevirt.io", version="v1beta1",
                            namespace=namespace, plural="virtualmachinesnapshots",
                            name=snap_name,
                        )
                        logger.info(f"Deleted snapshot {snap_name} for VM {name}")
                    except ApiException:
                        logger.warning(f"Failed to delete snapshot {snap_name}")
        except ApiException:
            pass  # Snapshot CRD may not exist

        # 2. Delete the VM
        await k8s_client.delete_virtual_machine(name=name, namespace=namespace)

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete VM: {e.reason}",
        )


@router.get("/{name}/yaml")
async def get_vm_yaml(
    request: Request,
    namespace: str,
    name: str,
    user: User = Depends(require_env_viewer()),
) -> dict[str, Any]:
    """Get VirtualMachine as raw YAML/JSON."""
    k8s_client = request.app.state.k8s_client

    try:
        vm = await k8s_client.get_virtual_machine(name=name, namespace=namespace)
        # Remove managed fields for cleaner output
        if "managedFields" in vm.get("metadata", {}):
            del vm["metadata"]["managedFields"]
        return vm

    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"VM {name} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get VM: {e.reason}",
        )
