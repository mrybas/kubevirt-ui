"""VM Templates API endpoints."""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from app.core.auth import User, require_auth
from app.core.naming import generate_k8s_name, DISPLAY_NAME_ANNOTATION
from app.models.template import (
    VMTemplate,
    VMTemplateCreate,
    VMTemplateListResponse,
    VMTemplateUpdate,
    GoldenImage,
    GoldenImageCreate,
    GoldenImageListResponse,
    GoldenImageUpdate,
    CreateImageFromDiskRequest,
)

router = APIRouter()
images_router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
TEMPLATE_CONFIGMAP_NAME = "kubevirt-ui-templates"
TEMPLATE_NAMESPACE = "kubevirt-ui-system"
PROJECT_ENABLED_LABEL = "kubevirt-ui.io/enabled"

# Labels
MANAGED_LABEL = "kubevirt-ui.io/managed"

# Folder hierarchy constants
FOLDERS_CONFIGMAP = "kubevirt-ui-folders"
FOLDERS_NAMESPACE = "kubevirt-ui-system"


async def _resolve_folder_ancestors(k8s_client, folder_name: str) -> list[str]:
    """Walk up the folder tree, return ancestor folder names (root first)."""
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=FOLDERS_CONFIGMAP, namespace=FOLDERS_NAMESPACE,
        )
        data = cm.data or {}
    except Exception:
        return []

    import json as _json
    folders: dict[str, dict] = {}
    for name, raw in data.items():
        try:
            folders[name] = _json.loads(raw)
        except (ValueError, TypeError):
            folders[name] = {}

    chain: list[str] = []
    visited: set[str] = set()
    current = folder_name
    while True:
        meta = folders.get(current)
        if not meta:
            break
        parent = meta.get("parent_id")
        if not parent or parent in visited:
            break
        visited.add(parent)
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


# =============================================================================
# VM Templates API (stored as ConfigMap)
# =============================================================================


@router.get("", response_model=VMTemplateListResponse)
async def list_templates(
    request: Request,
    user: User = Depends(require_auth),
) -> VMTemplateListResponse:
    """List all VM templates."""
    k8s_client = request.app.state.k8s_client
    
    try:
        # Try to get the templates ConfigMap
        try:
            cm = await k8s_client.core_api.read_namespaced_config_map(
                name=TEMPLATE_CONFIGMAP_NAME,
                namespace=TEMPLATE_NAMESPACE,
            )
            templates_data = cm.data or {}
        except ApiException as e:
            if e.status == 404:
                # ConfigMap doesn't exist yet - return empty list
                templates_data = {}
            else:
                raise
        
        # Parse templates from ConfigMap
        import json
        templates = []
        for name, data in templates_data.items():
            try:
                template_dict = json.loads(data)
                template_dict["name"] = name
                templates.append(VMTemplate(**template_dict))
            except Exception as e:
                logger.warning(f"Failed to parse template {name}: {e}")
        
        return VMTemplateListResponse(items=templates, total=len(templates))
    
    except ApiException as e:
        logger.error(f"Failed to list templates: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list templates: {e.reason}",
        )


@router.get("/{name}", response_model=VMTemplate)
async def get_template(
    name: str,
    request: Request,
    user: User = Depends(require_auth),
) -> VMTemplate:
    """Get a specific VM template."""
    k8s_client = request.app.state.k8s_client
    
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME,
            namespace=TEMPLATE_NAMESPACE,
        )
        
        if not cm.data or name not in cm.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template {name} not found",
            )
        
        import json
        template_dict = json.loads(cm.data[name])
        template_dict["name"] = name
        return VMTemplate(**template_dict)
    
    except HTTPException:
        raise
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Templates not configured",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get template: {e.reason}",
        )


@router.post("", response_model=VMTemplate, status_code=status.HTTP_201_CREATED)
async def create_template(
    template: VMTemplateCreate,
    request: Request,
    user: User = Depends(require_auth),
) -> VMTemplate:
    """Create a new VM template."""
    k8s_client = request.app.state.k8s_client
    
    try:
        import json
        from datetime import datetime
        
        # Ensure namespace exists
        try:
            await k8s_client.core_api.read_namespace(TEMPLATE_NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                # Create the namespace
                ns = client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=TEMPLATE_NAMESPACE,
                        labels={MANAGED_LABEL: "true"},
                    )
                )
                await k8s_client.core_api.create_namespace(ns)
        
        # Get or create ConfigMap
        try:
            cm = await k8s_client.core_api.read_namespaced_config_map(
                name=TEMPLATE_CONFIGMAP_NAME,
                namespace=TEMPLATE_NAMESPACE,
            )
            if cm.data is None:
                cm.data = {}
        except ApiException as e:
            if e.status == 404:
                # Create new ConfigMap
                cm = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(
                        name=TEMPLATE_CONFIGMAP_NAME,
                        namespace=TEMPLATE_NAMESPACE,
                        labels={MANAGED_LABEL: "true"},
                    ),
                    data={},
                )
                await k8s_client.core_api.create_namespaced_config_map(
                    namespace=TEMPLATE_NAMESPACE,
                    body=cm,
                )
                cm.data = {}
            else:
                raise
        
        # Check if template already exists
        if template.name in cm.data:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Template {template.name} already exists",
            )
        
        # Validate that the golden_image exists in the specified namespace
        # Templates can only use images from the same namespace
        if template.golden_image_name and template.golden_image_namespace:
            custom_api = client.CustomObjectsApi(k8s_client._api_client)
            try:
                await custom_api.get_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=template.golden_image_namespace,
                    plural="datavolumes",
                    name=template.golden_image_name,
                )
            except ApiException as e:
                if e.status == 404:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Image '{template.golden_image_name}' not found in project '{template.golden_image_namespace}'",
                    )
                raise
        
        # Create template data
        template_data = template.model_dump(exclude={"name"})
        template_data["created"] = datetime.utcnow().isoformat()
        
        # Update ConfigMap
        cm.data[template.name] = json.dumps(template_data)
        await k8s_client.core_api.replace_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME,
            namespace=TEMPLATE_NAMESPACE,
            body=cm,
        )
        
        return VMTemplate(name=template.name, **template_data)
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to create template: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create template: {e.reason}",
        )


@router.put("/{name}", response_model=VMTemplate)
async def update_template(
    name: str,
    template: VMTemplateCreate,
    request: Request,
    user: User = Depends(require_auth),
) -> VMTemplate:
    """Update an existing VM template."""
    k8s_client = request.app.state.k8s_client
    
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME,
            namespace=TEMPLATE_NAMESPACE,
        )
        
        if not cm.data or name not in cm.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template {name} not found",
            )
        
        # Get existing template data to preserve created timestamp
        existing_data = json.loads(cm.data[name])
        
        # Validate that the golden_image exists if changed
        if template.golden_image_name and template.golden_image_namespace:
            custom_api = client.CustomObjectsApi(k8s_client._api_client)
            try:
                await custom_api.get_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=template.golden_image_namespace,
                    plural="datavolumes",
                    name=template.golden_image_name,
                )
            except ApiException as e:
                if e.status == 404:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Image '{template.golden_image_name}' not found in project '{template.golden_image_namespace}'",
                    )
                raise
        
        # Update template data, preserving created timestamp
        template_data = template.model_dump(exclude={"name"})
        template_data["created"] = existing_data.get("created", datetime.utcnow().isoformat())
        template_data["updated"] = datetime.utcnow().isoformat()
        
        # Update ConfigMap
        cm.data[name] = json.dumps(template_data)
        await k8s_client.core_api.replace_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME,
            namespace=TEMPLATE_NAMESPACE,
            body=cm,
        )
        
        return VMTemplate(name=name, **template_data)
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to update template: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update template: {e.reason}",
        )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    name: str,
    request: Request,
    user: User = Depends(require_auth),
) -> None:
    """Delete a VM template."""
    k8s_client = request.app.state.k8s_client
    
    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME,
            namespace=TEMPLATE_NAMESPACE,
        )
        
        if not cm.data or name not in cm.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template {name} not found",
            )
        
        del cm.data[name]
        await k8s_client.core_api.replace_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME,
            namespace=TEMPLATE_NAMESPACE,
            body=cm,
        )
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to delete template: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete template: {e.reason}",
        )


# =============================================================================
# Golden Images API
# =============================================================================


@images_router.get("", response_model=GoldenImageListResponse)
async def list_golden_images(
    request: Request,
    user: User = Depends(require_auth),
    namespace: str | None = None,
) -> GoldenImageListResponse:
    """List all images (DataVolumes) from project namespaces.
    
    If namespace is provided, lists images from that namespace
    PLUS project-scoped images from sibling environments in the same project.
    If no namespace specified, lists images from all accessible project namespaces.
    """
    k8s_client = request.app.state.k8s_client
    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    
    images = []
    namespaces_to_check = set()
    
    # If a specific namespace is provided, check it + sibling/ancestor namespaces
    if namespace:
        namespaces_to_check.add(namespace)
        try:
            ns_obj = await k8s_client.core_api.read_namespace(namespace)
            ns_labels = ns_obj.metadata.labels or {}
            # Support both legacy project-based and new folder-based scoping
            folder_name = ns_labels.get("kubevirt-ui.io/folder")
            project_name = ns_labels.get("kubevirt-ui.io/project")
            if folder_name:
                # Folder-based: walk up folder tree, include all ancestor folder namespaces
                ancestor_folders = await _resolve_folder_ancestors(k8s_client, folder_name)
                all_folder_names = [folder_name] + ancestor_folders
                for fname in all_folder_names:
                    try:
                        folder_ns_list = await k8s_client.core_api.list_namespace(
                            label_selector=f"kubevirt-ui.io/folder={fname}"
                        )
                        for fns in folder_ns_list.items:
                            namespaces_to_check.add(fns.metadata.name)
                    except Exception:
                        pass
            elif project_name:
                # Legacy project-based: sibling namespaces in the same project
                sibling_ns_list = await k8s_client.list_namespaces(
                    label_selector=f"kubevirt-ui.io/project={project_name}"
                )
                for ns in sibling_ns_list:
                    namespaces_to_check.add(ns["name"])
        except Exception as e:
            logger.debug(f"Could not resolve sibling/ancestor namespaces: {e}")
    else:
        # Get all project namespaces (with kubevirt-ui.io/enabled=true label)
        try:
            ns_list = await k8s_client.list_namespaces(
                label_selector="kubevirt-ui.io/enabled=true"
            )
            for ns in ns_list:
                namespaces_to_check.add(ns["name"])
        except Exception as e:
            logger.warning(f"Failed to list project namespaces: {e}")
    
    if not namespaces_to_check:
        return GoldenImageListResponse(items=[], total=0)
    
    try:
        # Pre-fetch namespace labels for project/environment resolution
        ns_labels_map: dict[str, dict[str, str]] = {}
        for ns in namespaces_to_check:
            try:
                ns_obj = await k8s_client.core_api.read_namespace(ns)
                ns_labels_map[ns] = ns_obj.metadata.labels or {}
            except Exception:
                ns_labels_map[ns] = {}
        
        # Collect which golden images are used by VMs
        # golden_image_key (ns/name) -> list of VM names
        image_usage: dict[str, list[str]] = {}
        
        for ns in namespaces_to_check:
            try:
                # 1) Scan VM-owned DataVolumes (cloned disks) to trace source golden image
                dvs_result = await custom_api.list_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="datavolumes",
                    label_selector="kubevirt-ui.io/vm-disk=true",
                )
                for dv in dvs_result.get("items", []):
                    dv_labels = dv.get("metadata", {}).get("labels", {})
                    vm_name = dv_labels.get("kubevirt-ui.io/vm")
                    if not vm_name:
                        continue
                    vm_full_name = f"{ns}/{vm_name}"
                    # Trace source golden image from this cloned DV
                    source = dv.get("spec", {}).get("source", {})
                    if "pvc" in source:
                        src_ns = source["pvc"].get("namespace", ns)
                        src_name = source["pvc"].get("name")
                        if src_name:
                            key = f"{src_ns}/{src_name}"
                            if key not in image_usage:
                                image_usage[key] = []
                            if vm_full_name not in image_usage[key]:
                                image_usage[key].append(vm_full_name)
                
                # 2) Scan VMs for directly attached persistent disks / inline DV templates
                vms_result = await custom_api.list_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=ns,
                    plural="virtualmachines",
                )
                for vm in vms_result.get("items", []):
                    vm_name = vm["metadata"]["name"]
                    vm_ns = vm["metadata"]["namespace"]
                    vm_full_name = f"{vm_ns}/{vm_name}"
                    
                    # Check dataVolumeTemplates for inline clone sources
                    dv_templates = vm.get("spec", {}).get("dataVolumeTemplates", [])
                    for dv_template in dv_templates:
                        source = dv_template.get("spec", {}).get("source", {})
                        if "pvc" in source:
                            source_ns = source["pvc"].get("namespace", vm_ns)
                            source_name = source["pvc"].get("name")
                            if source_name:
                                key = f"{source_ns}/{source_name}"
                                if key not in image_usage:
                                    image_usage[key] = []
                                if vm_full_name not in image_usage[key]:
                                    image_usage[key].append(vm_full_name)
                    
                    # Check volumes for directly attached persistent disks
                    volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
                    for vol in volumes:
                        if "persistentVolumeClaim" in vol:
                            pvc_name = vol["persistentVolumeClaim"].get("claimName")
                            if pvc_name:
                                key = f"{vm_ns}/{pvc_name}"
                                if key not in image_usage:
                                    image_usage[key] = []
                                if vm_full_name not in image_usage[key]:
                                    image_usage[key].append(vm_full_name)
                        elif "dataVolume" in vol:
                            dv_name = vol["dataVolume"].get("name")
                            if dv_name:
                                is_template_dv = any(
                                    dvt.get("metadata", {}).get("name") == dv_name 
                                    for dvt in dv_templates
                                )
                                if not is_template_dv:
                                    key = f"{vm_ns}/{dv_name}"
                                    if key not in image_usage:
                                        image_usage[key] = []
                                    if vm_full_name not in image_usage[key]:
                                        image_usage[key].append(vm_full_name)
            except ApiException:
                continue
        
        # List all DataVolumes from project namespaces
        for ns in namespaces_to_check:
            try:
                result = await custom_api.list_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="datavolumes",
                )
                
                for dv in result.get("items", []):
                    metadata = dv.get("metadata", {})
                    spec = dv.get("spec", {})
                    status_obj = dv.get("status", {})
                    annotations = metadata.get("annotations", {})
                    labels = metadata.get("labels", {})
                    
                    dv_name = metadata.get("name")
                    dv_ns = metadata.get("namespace")
                    
                    # Skip DataVolumes that are owned by a VM (cloned disks for VMs)
                    owner_refs = metadata.get("ownerReferences", [])
                    if any(ref.get("kind") == "VirtualMachine" for ref in owner_refs):
                        continue
                    
                    # Skip DataVolumes marked as VM disks (backup filter)
                    if labels.get("kubevirt-ui.io/vm-disk") == "true":
                        continue
                    
                    # Determine source URL/type
                    source = spec.get("source", {})
                    source_url = None
                    if "http" in source:
                        source_url = source["http"].get("url")
                    elif "registry" in source:
                        source_url = source["registry"].get("url")
                    elif "pvc" in source:
                        source_url = f"pvc:{source['pvc'].get('namespace', dv_ns)}/{source['pvc'].get('name')}"
                    elif "blank" in source:
                        source_url = "blank"
                    
                    # Get size from PVC spec
                    pvc_spec = spec.get("pvc", spec.get("storage", {}))
                    size = pvc_spec.get("resources", {}).get("requests", {}).get("storage", "Unknown")
                    
                    # Determine status: Pending, Ready, Error, or InUse
                    phase = status_obj.get("phase", "Unknown")
                    image_key = f"{dv_ns}/{dv_name}"
                    used_by = image_usage.get(image_key, [])
                    
                    # Check conditions for errors (CDI keeps phase=Pending during retries
                    # but sets Running condition reason=Error)
                    has_error_condition = False
                    error_message = None
                    for cond in status_obj.get("conditions", []):
                        if cond.get("type") == "Running" and cond.get("status") == "False" and cond.get("reason") in ("Error", "TransferFailed"):
                            has_error_condition = True
                            error_message = cond.get("message")
                            break
                    
                    if phase in ("Failed", "Error") or has_error_condition:
                        display_status = "Error"
                    elif phase in ("ImportScheduled", "ImportInProgress", "CloneScheduled", "CloneInProgress", "Pending", "WaitForFirstConsumer", "N/A"):
                        display_status = "Pending"
                    elif used_by:
                        display_status = "InUse"
                    elif phase == "Succeeded":
                        display_status = "Ready"
                    else:
                        display_status = phase  # Show actual phase for other states
                    
                    # Get disk_type and persistent from labels
                    disk_type = labels.get("kubevirt-ui.io/disk-type", "image")
                    persistent_str = labels.get("kubevirt-ui.io/persistent", "false")
                    persistent = persistent_str.lower() == "true"
                    
                    # Get scope and project from labels
                    dv_scope = labels.get("kubevirt-ui.io/scope", "environment")
                    dv_project = labels.get("kubevirt-ui.io/project")
                    
                    # Resolve project/environment from namespace labels
                    dv_ns_labels = ns_labels_map.get(dv_ns, {})
                    resolved_project = dv_project or dv_ns_labels.get("kubevirt-ui.io/project")
                    resolved_env = dv_ns_labels.get("kubevirt-ui.io/environment")
                    
                    # When filtering by namespace: skip images from sibling namespaces
                    # unless they are project-scoped or folder-scoped
                    if namespace and dv_ns != namespace and dv_scope not in ("project", "folder"):
                        continue
                    
                    images.append(GoldenImage(
                        name=dv_name,
                        namespace=dv_ns,
                        display_name=annotations.get("kubevirt-ui.io/display-name", dv_name),
                        description=annotations.get("kubevirt-ui.io/description"),
                        os_type=labels.get("kubevirt-ui.io/os-type"),
                        os_version=labels.get("kubevirt-ui.io/os-version"),
                        size=size,
                        status=display_status,
                        error_message=error_message if display_status == "Error" else None,
                        source_url=source_url,
                        created=metadata.get("creationTimestamp"),
                        used_by=used_by if used_by else None,
                        disk_type=disk_type,
                        persistent=persistent,
                        scope=dv_scope,
                        project=resolved_project,
                        environment=resolved_env,
                    ))
            except ApiException as e:
                logger.warning(f"Failed to list DataVolumes in {ns}: {e}")
                continue
        
        return GoldenImageListResponse(items=images, total=len(images))
    
    except ApiException as e:
        logger.error(f"Failed to list images: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list images: {e.reason}",
        )


@images_router.post("", response_model=GoldenImage, status_code=status.HTTP_201_CREATED)
async def create_golden_image(
    image: GoldenImageCreate,
    request: Request,
    user: User = Depends(require_auth),
    namespace: str = "default",
) -> GoldenImage:
    """Create a new disk (image or data) in a project namespace.
    
    Sources supported:
    - HTTP URL (for importing images)
    - Registry URL (for container images)
    - PVC clone (for cloning existing disks)
    - Blank (for empty data disks)
    
    Scope:
    - environment (default): image lives in this namespace only
    - project: image is labeled as available to all envs in the project
    """
    k8s_client = request.app.state.k8s_client
    
    # Resolve name: auto-generate from display_name if not provided
    if not image.name and not image.display_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'name' or 'display_name' must be provided",
        )
    if not image.name:
        image.name = generate_k8s_name(image.display_name)
    
    # Use namespace from request parameter (disk lives in project namespace)
    target_namespace = namespace
    
    try:
        # Verify namespace exists and resolve project name
        try:
            ns_obj = await k8s_client.core_api.read_namespace(target_namespace)
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Namespace '{target_namespace}' not found",
                )
            raise
        
        # Resolve project name from namespace labels or from request
        ns_labels = ns_obj.metadata.labels or {}
        project_name = image.project or ns_labels.get("kubevirt-ui.io/project")
        
        # Determine source
        source_url_display = None
        if image.source_url:
            source = {"http": {"url": image.source_url}}
            source_url_display = image.source_url
        elif image.source_registry:
            source = {"registry": {"url": image.source_registry}}
            source_url_display = image.source_registry
        elif image.source_pvc:
            # Clone from existing PVC
            pvc_ns = image.source_pvc_namespace or target_namespace
            source = {"pvc": {"name": image.source_pvc, "namespace": pvc_ns}}
            source_url_display = f"pvc:{pvc_ns}/{image.source_pvc}"
        else:
            # Blank disk (for data disks)
            source = {"blank": {}}
            source_url_display = "blank"
        
        # Build storage spec (new CDI format)
        image_storage: dict[str, Any] = {
            "volumeMode": "Block",  # Required for snapshot-based cloning
            "resources": {
                "requests": {
                    "storage": image.size,
                }
            },
        }
        if image.storage_class:
            image_storage["storageClassName"] = image.storage_class
        
        # Build DataVolume
        dv_labels: dict[str, str] = {
            MANAGED_LABEL: "true",
            "kubevirt-ui.io/disk-type": image.disk_type or "image",
            "kubevirt-ui.io/persistent": str(image.persistent).lower(),
        }
        
        # Scope labels
        scope = image.scope or "environment"
        dv_labels["kubevirt-ui.io/scope"] = scope
        if scope == "project" and project_name:
            dv_labels["kubevirt-ui.io/project"] = project_name
        
        dv = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": image.name,
                "namespace": target_namespace,
                "labels": dv_labels,
                "annotations": {},
            },
            "spec": {
                "source": source,
                "storage": image_storage,
            },
        }
        
        # Add optional labels
        if image.os_type:
            dv["metadata"]["labels"]["kubevirt-ui.io/os-type"] = image.os_type
        if image.display_name:
            dv["metadata"]["annotations"]["kubevirt-ui.io/display-name"] = image.display_name
        if image.description:
            dv["metadata"]["annotations"]["kubevirt-ui.io/description"] = image.description
        if image.os_version:
            dv["metadata"]["labels"]["kubevirt-ui.io/os-version"] = image.os_version
        
        # Create DataVolume
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=target_namespace,
            plural="datavolumes",
            body=dv,
        )
        
        return GoldenImage(
            name=result["metadata"]["name"],
            namespace=result["metadata"]["namespace"],
            display_name=image.display_name,
            description=image.description,
            os_type=image.os_type,
            os_version=image.os_version,
            disk_type=image.disk_type,
            persistent=image.persistent,
            size=image.size,
            status=result.get("status", {}).get("phase", "Pending"),
            source_url=source_url_display,
            created=result["metadata"].get("creationTimestamp"),
            scope=scope,
            project=project_name,
        )
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to create golden image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create golden image: {e.reason}",
        )


@images_router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_golden_image(
    name: str,
    request: Request,
    user: User = Depends(require_auth),
    namespace: str = "default",
) -> None:
    """Delete an image from a project namespace."""
    k8s_client = request.app.state.k8s_client
    
    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        await custom_api.delete_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image {name} not found in namespace {namespace}",
            )
        logger.error(f"Failed to delete image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete image: {e.reason}",
        )


@images_router.patch("/{name}", response_model=GoldenImage)
async def update_golden_image(
    name: str,
    update: GoldenImageUpdate,
    request: Request,
    user: User = Depends(require_auth),
    namespace: str = "default",
) -> GoldenImage:
    """Update image metadata (scope, display name, description).
    
    Patches labels and annotations on the DataVolume in-place.
    """
    k8s_client = request.app.state.k8s_client
    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    
    try:
        # Get current DataVolume
        dv = await custom_api.get_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
        
        metadata = dv.get("metadata", {})
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        
        # Update scope labels
        if update.scope is not None:
            labels["kubevirt-ui.io/scope"] = update.scope
            if update.scope == "project":
                # Resolve project name from namespace labels
                try:
                    ns_obj = await k8s_client.core_api.read_namespace(namespace)
                    ns_labels = ns_obj.metadata.labels or {}
                    project_name = ns_labels.get("kubevirt-ui.io/project")
                    if project_name:
                        labels["kubevirt-ui.io/project"] = project_name
                except Exception:
                    pass
            elif update.scope == "environment":
                # Remove project label when scoping to environment
                labels.pop("kubevirt-ui.io/project", None)
        
        # Update display name / description
        if update.display_name is not None:
            annotations["kubevirt-ui.io/display-name"] = update.display_name
        if update.description is not None:
            annotations["kubevirt-ui.io/description"] = update.description
        
        # Patch the DataVolume
        patch_body = {
            "metadata": {
                "labels": labels,
                "annotations": annotations,
            }
        }
        
        result = await custom_api.patch_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
            body=patch_body,
        )
        
        result_meta = result.get("metadata", {})
        result_labels = result_meta.get("labels", {})
        result_annotations = result_meta.get("annotations", {})
        spec = result.get("spec", {})
        status_obj = result.get("status", {})
        
        # Determine source URL
        source = spec.get("source", {})
        source_url = None
        if "http" in source:
            source_url = source["http"].get("url")
        elif "registry" in source:
            source_url = source["registry"].get("url")
        elif "pvc" in source:
            source_url = f"pvc:{source['pvc'].get('namespace', namespace)}/{source['pvc'].get('name')}"
        elif "blank" in source:
            source_url = "blank"
        
        pvc_spec = spec.get("pvc", spec.get("storage", {}))
        size = pvc_spec.get("resources", {}).get("requests", {}).get("storage", "Unknown")
        
        # Determine display status with error condition check
        phase = status_obj.get("phase", "Unknown")
        has_error_condition = False
        error_message = None
        for cond in status_obj.get("conditions", []):
            if cond.get("type") == "Running" and cond.get("status") == "False" and cond.get("reason") in ("Error", "TransferFailed"):
                has_error_condition = True
                error_message = cond.get("message")
                break
        
        if phase in ("Failed", "Error") or has_error_condition:
            display_status = "Error"
        elif phase in ("ImportScheduled", "ImportInProgress", "CloneScheduled", "CloneInProgress", "Pending", "WaitForFirstConsumer", "N/A"):
            display_status = "Pending"
        elif phase == "Succeeded":
            display_status = "Ready"
        else:
            display_status = phase
        
        return GoldenImage(
            name=result_meta["name"],
            namespace=result_meta["namespace"],
            display_name=result_annotations.get("kubevirt-ui.io/display-name", result_meta["name"]),
            description=result_annotations.get("kubevirt-ui.io/description"),
            os_type=result_labels.get("kubevirt-ui.io/os-type"),
            os_version=result_labels.get("kubevirt-ui.io/os-version"),
            disk_type=result_labels.get("kubevirt-ui.io/disk-type", "image"),
            persistent=result_labels.get("kubevirt-ui.io/persistent", "false").lower() == "true",
            size=size,
            status=display_status,
            error_message=error_message if display_status == "Error" else None,
            source_url=source_url,
            created=result_meta.get("creationTimestamp"),
            scope=result_labels.get("kubevirt-ui.io/scope", "environment"),
            project=result_labels.get("kubevirt-ui.io/project"),
        )
    
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image {name} not found in namespace {namespace}",
            )
        logger.error(f"Failed to update image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update image: {e.reason}",
        )


@images_router.post("/from-disk", response_model=GoldenImage, status_code=status.HTTP_201_CREATED)
async def create_golden_image_from_disk(
    req: CreateImageFromDiskRequest,
    request: Request,
    user: User = Depends(require_auth),
) -> GoldenImage:
    """Create an image by cloning an existing disk (snapshot) into a project namespace."""
    k8s_client = request.app.state.k8s_client
    
    # Target namespace - use namespace from request, default to source_namespace
    target_namespace = req.target_namespace or req.source_namespace
    
    try:
        # Verify target namespace exists
        try:
            await k8s_client.core_api.read_namespace(target_namespace)
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Target namespace '{target_namespace}' not found",
                )
            raise
        
        # Get source PVC to determine size
        source_pvc = await k8s_client.core_api.read_namespaced_persistent_volume_claim(
            name=req.source_disk_name,
            namespace=req.source_namespace,
        )
        
        size = source_pvc.spec.resources.requests.get("storage", "50Gi")
        storage_class = source_pvc.spec.storage_class_name
        
        # Build storage spec (new CDI format)
        clone_storage: dict[str, Any] = {
            "volumeMode": "Block",  # Required for snapshot-based cloning
            "resources": {
                "requests": {
                    "storage": size,
                }
            },
        }
        if storage_class:
            clone_storage["storageClassName"] = storage_class
        
        # Build DataVolume with PVC clone source
        dv = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": req.name,
                "namespace": target_namespace,
                "labels": {
                    MANAGED_LABEL: "true",
                    "kubevirt-ui.io/os-type": req.os_type,
                    "kubevirt-ui.io/cloned-from": f"{req.source_namespace}/{req.source_disk_name}",
                },
                "annotations": {},
            },
            "spec": {
                "source": {
                    "pvc": {
                        "name": req.source_disk_name,
                        "namespace": req.source_namespace,
                    }
                },
                "storage": clone_storage,
            },
        }
        
        if req.display_name:
            dv["metadata"]["annotations"]["kubevirt-ui.io/display-name"] = req.display_name
        if req.description:
            dv["metadata"]["annotations"]["kubevirt-ui.io/description"] = req.description
        if req.os_version:
            dv["metadata"]["labels"]["kubevirt-ui.io/os-version"] = req.os_version
        
        # Create DataVolume
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=target_namespace,
            plural="datavolumes",
            body=dv,
        )
        
        return GoldenImage(
            name=result["metadata"]["name"],
            namespace=result["metadata"]["namespace"],
            display_name=req.display_name,
            description=req.description,
            os_type=req.os_type,
            os_version=req.os_version,
            size=size,
            status=result.get("status", {}).get("phase", "Pending"),
            source_url=f"pvc://{req.source_namespace}/{req.source_disk_name}",
            created=result["metadata"].get("creationTimestamp"),
        )
    
    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to create image from disk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create image from disk: {e.reason}",
        )
