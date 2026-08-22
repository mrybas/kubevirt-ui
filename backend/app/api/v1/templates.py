"""VM Templates API endpoints."""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from app.core.auth import User, require_auth
from app.core.operator import (
    OPERATOR_GROUP,
    OPERATOR_VERSION,
    OWNER_KIND_LABEL,
    OWNER_NAME_LABEL,
    image_path_enabled,
    template_path_enabled,
)
from app.core.naming import DISPLAY_NAME_ANNOTATION, SLUG_LABEL, sanitize_display_name
from app.core.storage_headroom import assert_storage_headroom
from app.models.template import (
    TemplateCloudInit,
    TemplateCompute,
    TemplateConsole,
    TemplateDisk,
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


# ---------------------------------------------------------------------------
# Templates as custom resources
# ---------------------------------------------------------------------------


def _template_from_cr(cr: dict[str, Any]) -> VMTemplate:
    """Present a ManagedVMTemplate in the shape the UI already reads.

    The image namespace is taken from status when the controller has resolved
    it, and from the spec otherwise, so a template is usable before its first
    reconcile rather than appearing to point nowhere.
    """
    meta = cr.get("metadata", {}) or {}
    spec = cr.get("spec", {}) or {}
    status = cr.get("status", {}) or {}
    image = spec.get("imageRef", {}) or {}
    compute = spec.get("compute", {}) or {}
    disk = spec.get("rootDisk", {}) or {}
    console = spec.get("console") or {}
    cloud_init = spec.get("cloudInit") or None

    return VMTemplate(
        name=meta.get("name", ""),
        display_name=spec.get("displayName") or meta.get("name", ""),
        description=spec.get("description"),
        category=spec.get("category", "linux"),
        os_type=spec.get("osType", "linux"),
        golden_image_name=image.get("name", ""),
        golden_image_namespace=(
            status.get("imageNamespace")
            or image.get("namespace")
            or meta.get("namespace", "")
        ),
        compute=TemplateCompute(
            cpu_cores=compute.get("cores", 2),
            cpu_sockets=compute.get("sockets", 1),
            cpu_threads=compute.get("threads", 1),
            memory=compute.get("memory", "4Gi"),
        ),
        disk=TemplateDisk(size=disk.get("size", "20Gi")),
        cloud_init=(
            TemplateCloudInit(user_data=cloud_init.get("userData"))
            if cloud_init else None
        ),
        console=TemplateConsole(
            vnc_enabled=console.get("vnc", True),
            serial_console_enabled=console.get("serial", False),
        ),
        created=meta.get("creationTimestamp"),
        labels=meta.get("labels", {}) or {},
        annotations=meta.get("annotations", {}) or {},
    )


async def _list_template_crs(custom_api: Any) -> list[dict[str, Any]]:
    """Every ManagedVMTemplate on the cluster, or none if the CRD is absent."""
    try:
        result = await custom_api.list_cluster_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION, plural="managedvmtemplates",
        )
    except ApiException as e:
        if e.status == 404:
            return []
        raise
    return result.get("items", []) or []


async def _find_template_cr(
    custom_api: Any, name: str, namespace: str | None = None,
) -> dict[str, Any] | None:
    """The ManagedVMTemplate with this name, if there is exactly one.

    Names are unique per namespace, not per cluster, so a name alone can be
    ambiguous. Rather than picking one, an ambiguous name is reported — the
    old store's collision behaviour was to answer 409 naming a template the
    user could not see, and guessing is not an improvement on that.
    """
    matches = [
        cr for cr in await _list_template_crs(custom_api)
        if cr.get("metadata", {}).get("name") == name
        and (namespace is None or cr.get("metadata", {}).get("namespace") == namespace)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        where = ", ".join(sorted(m["metadata"]["namespace"] for m in matches))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"template {name!r} exists in more than one namespace ({where}); "
                "name the namespace to say which one"
            ),
        )
    return matches[0]


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
        
        # Both stores are read during the migration, so a template written
        # either way is usable from either path. A resource shadows a legacy
        # entry of the same name: the migration copies one to the other, and
        # showing both would be showing the same template twice.
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        for cr in await _list_template_crs(custom_api):
            converted = _template_from_cr(cr)
            templates = [t for t in templates if t.name != converted.name]
            templates.append(converted)

        return VMTemplateListResponse(items=templates, total=len(templates))

    except HTTPException:
        raise
    except ApiException as e:
        logger.error(f"Failed to list templates: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list templates: {e.reason}",
        )


async def resolve_template(k8s_client: Any, name: str) -> dict[str, Any] | None:
    """A template by name, from whichever store holds it.

    One owner for the question "what is this template", because the answer was
    being worked out in three places and one of them only knew about the old
    store. `GET /templates` merged both, `GET /templates/{name}` read both, and
    `POST /vms/from-template` read the ConfigMap alone — so with
    OPERATOR_TEMPLATE_ENABLED on, a template appeared in the list, the wizard
    offered it, and creating a machine from it answered 404. The pairing of the
    two flags was unusable, and looked fine until the last click.

    Returns the plain-dict shape the ConfigMap has always held, because that is
    what the create path reads; a resource is presented in the same shape rather
    than the caller learning which store it came from.
    """
    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    existing = await _find_template_cr(custom_api, name)
    if existing is not None:
        return _template_from_cr(existing).model_dump()

    try:
        cm = await k8s_client.core_api.read_namespaced_config_map(
            name=TEMPLATE_CONFIGMAP_NAME, namespace=TEMPLATE_NAMESPACE,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    if not cm.data or name not in cm.data:
        return None
    template = json.loads(cm.data[name])
    template["name"] = name
    return template


@router.get("/{name}", response_model=VMTemplate)
async def get_template(
    name: str,
    request: Request,
    user: User = Depends(require_auth),
) -> VMTemplate:
    """Get a specific VM template."""
    k8s_client = request.app.state.k8s_client

    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    existing = await _find_template_cr(custom_api, name)
    if existing is not None:
        return _template_from_cr(existing)

    
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


def _template_spec(template: VMTemplateCreate) -> dict[str, Any]:
    """The resource's spec, as the request describes it.

    Shared by create and update so an edit writes the same shape a create does.
    They were separate once and only one of them knew this store existed.
    """
    spec: dict[str, Any] = {
        "displayName": template.display_name,
        "imageRef": {"name": template.golden_image_name},
        "compute": {
            "cores": template.compute.cpu_cores,
            "sockets": template.compute.cpu_sockets,
            "threads": template.compute.cpu_threads,
            "memory": template.compute.memory,
        },
        "rootDisk": {"size": template.disk.size},
        "category": template.category,
        "osType": template.os_type,
    }
    if template.description:
        spec["description"] = template.description
    if template.cloud_init and template.cloud_init.user_data:
        spec["cloudInit"] = {"userData": template.cloud_init.user_data}
    spec["console"] = {
        "vnc": template.console.vnc_enabled,
        "serial": template.console.serial_console_enabled,
    }
    return spec


async def _create_template_cr(k8s_client: Any, template: VMTemplateCreate) -> VMTemplate:
    """Write the template as its own object, next to the image it names.

    The namespace is the image's, because that is where the template is usable
    from and where the picker looks. The old store had no namespace at all —
    one cluster-wide map keyed by a user-chosen string, which is why a
    collision could name a template the user was not allowed to see.
    """
    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    namespace = template.golden_image_namespace
    spec = _template_spec(template)

    body = {
        "apiVersion": f"{OPERATOR_GROUP}/{OPERATOR_VERSION}",
        "kind": "ManagedVMTemplate",
        "metadata": {"name": template.name, "namespace": namespace},
        "spec": spec,
    }

    try:
        created = await custom_api.create_namespaced_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            namespace=namespace, plural="managedvmtemplates", body=body,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Template creation is routed to the operator "
                    "(OPERATOR_TEMPLATE_ENABLED), but the ManagedVMTemplate CRD is "
                    "not installed in this cluster"
                ),
            )
        if e.status == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Template {template.name!r} already exists in {namespace}",
            )
        raise HTTPException(
            status_code=e.status or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create template: {e.reason}",
        )

    return _template_from_cr(created)


@router.post("", response_model=VMTemplate, status_code=status.HTTP_201_CREATED)
async def create_template(
    template: VMTemplateCreate,
    request: Request,
    user: User = Depends(require_auth),
) -> VMTemplate:
    """Create a new VM template."""
    k8s_client = request.app.state.k8s_client

    if template_path_enabled():
        return await _create_template_cr(k8s_client, template)

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
        #
        # Templates are keyed by name in one cluster-wide ConfigMap while the
        # wizard only shows those whose image lives in the selected project.
        # A collision is therefore usually with a template the user cannot
        # see — most often one left pointing at a namespace that is gone — so
        # the message says where the existing one points.
        if template.name in cm.data:
            try:
                existing = json.loads(cm.data[template.name])
                where = (
                    f" (it uses image '{existing.get('golden_image_name')}' in "
                    f"project '{existing.get('golden_image_namespace')}')"
                )
            except Exception:
                where = ""
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Template '{template.name}' already exists{where}. "
                    f"Pick another name, or delete that template first."
                ),
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
    """Update an existing VM template, in whichever store holds it.

    A template that exists as its own object is edited as one, whatever the flag
    says — the same rule delete already followed. Without it an edit read the
    ConfigMap alone and answered 404 for a template the list had just shown,
    which is how create-from-template failed too.
    """
    k8s_client = request.app.state.k8s_client

    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    existing_cr = await _find_template_cr(custom_api, name)
    if existing_cr is not None:
        patched = await custom_api.patch_namespaced_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            namespace=existing_cr["metadata"]["namespace"],
            plural="managedvmtemplates", name=name,
            body={"spec": _template_spec(template)},
            # A merge body sent as a JSON Patch answers 400, and the contract
            # test upstairs exists because that has happened.
            _content_type="application/merge-patch+json",
        )
        return _template_from_cr(patched)

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
    """Delete a VM template.

    A template that exists as its own object is deleted as one, whatever the
    flag says: the flag decides where new templates are written, not where the
    old ones live.
    """
    k8s_client = request.app.state.k8s_client

    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    existing = await _find_template_cr(custom_api, name)
    if existing is not None:
        await custom_api.delete_namespaced_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION,
            namespace=existing["metadata"]["namespace"],
            plural="managedvmtemplates", name=name,
        )
        return

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
                # 1) Scan VM-owned DataVolumes (cloned disks) to trace source golden image.
                # Owner is read from ownerReferences (set by KubeVirt for DVs created via
                # dataVolumeTemplates) — this is always the actual VM name regardless of
                # whether the VM was created with a user-supplied name or generateName.
                dvs_result = await custom_api.list_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="datavolumes",
                    label_selector="kubevirt-ui.io/vm-disk=true",
                )
                for dv in dvs_result.get("items", []):
                    owners = dv.get("metadata", {}).get("ownerReferences") or []
                    vm_name = next(
                        (o.get("name") for o in owners if o.get("kind") == "VirtualMachine"),
                        None,
                    )
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
                        # A quota refusal never reaches the Running condition:
                        # CDI cannot make the PVC at all, so the DataVolume
                        # sits Pending with `Bound=False` and the reason on an
                        # event. In UAT run 4 that read as a disk importing
                        # for ever, next to a quota that was counting it.
                        if (
                            cond.get("type") == "Bound"
                            and cond.get("status") == "False"
                            and "quota" in (cond.get("reason", "") + cond.get("message", "")).lower()
                        ):
                            has_error_condition = True
                            error_message = cond.get("message") or cond.get("reason")
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
        
        # And the ones that were asked for but not built.
        #
        # The list reads DataVolumes, which is what the operator *makes* from a
        # ManagedImage. Anything it has not made yet — or will never make,
        # because the request cannot be satisfied — appeared nowhere at all: no
        # row, no error, no trace. UAT run 4, G3: a disk refused by the
        # namespace quota was invisible while it consumed the quota that
        # refused it.
        images.extend(await _described_but_unbuilt_images(
            custom_api, namespaces_to_check, namespace,
            {f"{img.namespace}/{img.name}" for img in images},
            ns_labels_map,
        ))

        return GoldenImageListResponse(items=images, total=len(images))
    
    except ApiException as e:
        logger.error(f"Failed to list images: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list images: {e.reason}",
        )


async def _described_but_unbuilt_images(
    custom_api: Any, namespaces: list[str], filter_namespace: str | None,
    already: set[str], ns_labels_map: dict[str, dict],
) -> list[GoldenImage]:
    """Images that exist as a request and not yet as a disk.

    A ManagedImage is the thing the user asked for; the DataVolume is what the
    operator builds from it. Listing only the second means the seconds between
    them show nothing — and, when the build cannot happen at all, so does
    forever.

    The row carries whatever the resource says about itself, so "why is it not
    there" is answered on the page rather than by reading a controller's log.
    """
    out: list[GoldenImage] = []
    for ns in namespaces:
        try:
            described = await custom_api.list_namespaced_custom_object(
                group=OPERATOR_GROUP, version=OPERATOR_VERSION,
                namespace=ns, plural="managedimages",
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to list ManagedImages in {ns}: {e}")
            continue

        for item in described.get("items", []):
            meta = item.get("metadata", {}) or {}
            name = meta.get("name", "")
            if not name or f"{ns}/{name}" in already:
                continue
            spec = item.get("spec", {}) or {}
            status_obj = item.get("status", {}) or {}
            labels = meta.get("labels", {}) or {}
            scope = labels.get("kubevirt-ui.io/scope", "environment")
            if filter_namespace and ns != filter_namespace and scope not in (
                "project", "folder",
            ):
                continue

            # What the resource says, in the order it becomes knowable: a
            # refusal first, then a phase, then the honest default.
            reason, message = "", ""
            for cond in status_obj.get("conditions", []):
                if cond.get("status") == "False" and cond.get("reason"):
                    reason, message = cond["reason"], cond.get("message", "")
                    break
            display = "Error" if reason else (status_obj.get("phase") or "Pending")
            if display not in ("Error", "Ready", "Pending"):
                display = "Pending"

            ns_labels = ns_labels_map.get(ns, {})
            source = spec.get("source", {}) or {}
            out.append(GoldenImage(
                name=name,
                namespace=ns,
                display_name=spec.get("displayName") or name,
                description=spec.get("description"),
                os_type=labels.get("kubevirt-ui.io/os-type"),
                os_version=labels.get("kubevirt-ui.io/os-version"),
                size=spec.get("size", "Unknown"),
                status=display,
                error_message=(message or reason) if display == "Error" else None,
                source_url=source.get("url") or source.get("registry"),
                created=meta.get("creationTimestamp"),
                used_by=status_obj.get("usedBy") or None,
                disk_type=labels.get("kubevirt-ui.io/disk-type", "image"),
                persistent=labels.get("kubevirt-ui.io/persistent", "false").lower() == "true",
                scope=scope,
                project=labels.get("kubevirt-ui.io/project")
                or ns_labels.get("kubevirt-ui.io/project"),
                environment=ns_labels.get("kubevirt-ui.io/environment"),
            ))
    return out


async def _create_managed_image(
    *,
    k8s_client: Any,
    image: GoldenImageCreate,
    namespace: str,
    slug: str,
    display_name_value: str,
    source: dict[str, Any],
    source_url_display: str | None,
    scope: str,
    project_name: str | None,
) -> GoldenImage:
    """Create the image as a ManagedImage and let the operator build the disk.

    The response is deliberately the same shape the DataVolume path returns, so
    the UI cannot tell which side of the flag it is on. Status starts at Pending
    because nothing has been imported yet — the operator publishes progress and
    the terminal state on the resource, and the image lister picks it up from
    the disk's labels the same way it always has.
    """
    custom_api = client.CustomObjectsApi(k8s_client._api_client)

    spec: dict[str, Any] = {
        "displayName": display_name_value,
        "source": source,
        "size": image.size,
        "scope": scope,
        "diskType": image.disk_type or "image",
        "persistent": bool(image.persistent),
    }
    if image.storage_class:
        spec["storageClass"] = image.storage_class
    if image.description:
        spec["description"] = image.description
    if image.os_type:
        spec["osType"] = image.os_type
    if image.os_version:
        spec["osVersion"] = image.os_version

    body = {
        "apiVersion": f"{OPERATOR_GROUP}/{OPERATOR_VERSION}",
        "kind": "ManagedImage",
        "metadata": {
            "generateName": f"{slug}-",
            "namespace": namespace,
        },
        "spec": spec,
    }

    try:
        created = await custom_api.create_namespaced_custom_object(
            group=OPERATOR_GROUP,
            version=OPERATOR_VERSION,
            namespace=namespace,
            plural="managedimages",
            body=body,
        )
    except ApiException as e:
        if e.status == 404:
            # The flag is on and the CRD is not installed. Saying so beats a
            # generic 500 that reads as "the import failed".
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Image creation is routed to the operator "
                    "(OPERATOR_IMAGE_ENABLED), but the ManagedImage CRD is not "
                    "installed in this cluster"
                ),
            )
        raise HTTPException(
            status_code=e.status or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create image: {e.reason}",
        )

    return GoldenImage(
        name=created["metadata"]["name"],
        namespace=created["metadata"]["namespace"],
        display_name=display_name_value,
        description=image.description,
        os_type=image.os_type,
        os_version=image.os_version,
        disk_type=image.disk_type,
        persistent=image.persistent,
        size=image.size,
        status="Pending",
        source_url=source_url_display,
        created=created["metadata"].get("creationTimestamp"),
        scope=scope,
        project=project_name,
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

    # Asked before the DataVolume exists, because the quota counts the PVC
    # that CDI makes from it and not the DataVolume itself — which is how a
    # 100Gi disk against a 12Gi quota came back 201 and then never appeared.
    await assert_storage_headroom(
        k8s_client, namespace, image.size, what=f"{image.name!r}",
    )

    # At-least-one runtime check (soft contract — matches create_tenant_image).
    if not image.name and not image.display_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'name' or 'display_name' must be provided",
        )

    # Synthetic naming: seed comes from `name` if the client supplied it
    # (back-compat with Terraform/CLI), otherwise from display_name. The
    # display-name annotation falls back to `name` when only `name` was given.
    seed = image.name or image.display_name
    display_name_value = image.display_name or image.name
    slug = sanitize_display_name(seed)

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
        
        # With the operator owning images, the backend stops writing the disk
        # and writes the intent instead. Same request, same namespace, same
        # naming: generateName on our own resource, exactly as it was on the
        # DataVolume — so the disk still ends up named `<slug>-xxxxx` and every
        # caller that reads a name back keeps working.
        if image_path_enabled():
            return await _create_managed_image(
                k8s_client=k8s_client,
                image=image,
                namespace=target_namespace,
                slug=slug,
                display_name_value=display_name_value,
                source=source,
                source_url_display=source_url_display,
                scope=image.scope or "environment",
                project_name=project_name,
            )

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
            SLUG_LABEL: slug,
        }

        # Scope labels
        scope = image.scope or "environment"
        dv_labels["kubevirt-ui.io/scope"] = scope
        if scope == "project" and project_name:
            dv_labels["kubevirt-ui.io/project"] = project_name

        # Synthetic naming: generateName + display-name annotation + slug label.
        # Same manual stamping pattern as tenants_crud.create_tenant_image —
        # preserves seed-override semantics that with_synthetic_metadata lacks.
        dv = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "generateName": f"{slug}-",
                "namespace": target_namespace,
                "labels": dv_labels,
                "annotations": {
                    DISPLAY_NAME_ANNOTATION: display_name_value,
                },
            },
            "spec": {
                "source": source,
                "storage": image_storage,
            },
        }

        # Add optional labels/annotations
        if image.os_type:
            dv["metadata"]["labels"]["kubevirt-ui.io/os-type"] = image.os_type
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
            display_name=display_name_value,
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


async def _managed_image_owner(
    custom_api: Any, namespace: str, dv_name: str,
) -> str | None:
    """Name of the ManagedImage owning this disk, or None if nothing owns it."""
    try:
        dv = await custom_api.get_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=dv_name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    labels = (dv.get("metadata", {}) or {}).get("labels", {}) or {}
    if labels.get(OWNER_KIND_LABEL) != "ManagedImage":
        return None
    return labels.get(OWNER_NAME_LABEL) or None


async def _delete_managed_image(
    custom_api: Any, namespace: str, name: str,
) -> None:
    """Delete the owning resource, refusing up front if the image is in use.

    The operator would refuse this deletion anyway — its finalizer holds while
    something is still cloning from the disk — but it refuses asynchronously,
    which from a browser looks like a delete that did nothing. Reading the
    holders and answering 409 with their names turns that into an answer.
    """
    try:
        mi = await custom_api.get_namespaced_custom_object(
            group=OPERATOR_GROUP,
            version=OPERATOR_VERSION,
            namespace=namespace,
            plural="managedimages",
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            # The disk claims an owner that is gone. Nothing left to release —
            # fall through to deleting the disk itself.
            mi = None
        else:
            raise

    if mi is not None:
        used_by = (mi.get("status", {}) or {}).get("usedBy") or []
        if used_by:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Image {name} is still in use by: {', '.join(used_by)}",
            )
        await custom_api.delete_namespaced_custom_object(
            group=OPERATOR_GROUP,
            version=OPERATOR_VERSION,
            namespace=namespace,
            plural="managedimages",
            name=name,
        )
        return

    await custom_api.delete_namespaced_custom_object(
        group="cdi.kubevirt.io",
        version="v1beta1",
        namespace=namespace,
        plural="datavolumes",
        name=name,
    )


@images_router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_golden_image(
    name: str,
    request: Request,
    user: User = Depends(require_auth),
    namespace: str = "default",
) -> None:
    """Delete an image from a project namespace.

    Ownership decides what gets deleted, not the feature flag. A disk the
    operator owns has to be released by deleting its ManagedImage: deleting the
    disk directly would only make the controller build it again, and the user
    would watch a deleted image reappear. Ownership is stamped on the object, so
    it keeps being true after the flag is turned back off.
    """
    k8s_client = request.app.state.k8s_client

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)

        owner = await _managed_image_owner(custom_api, namespace, name)
        if owner:
            await _delete_managed_image(custom_api, namespace, owner)
            return

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
            _content_type="application/merge-patch+json",
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

    # At-least-one runtime check (soft contract — matches create_tenant_image).
    if not req.name and not req.display_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'name' or 'display_name' must be provided",
        )

    # Synthetic naming: seed comes from `name` if the client supplied it
    # (back-compat), else from display_name. Display-name annotation falls
    # back to `name` when only `name` was given.
    seed = req.name or req.display_name
    display_name_value = req.display_name or req.name
    slug = sanitize_display_name(seed)
    
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
        # Explicit target class wins; otherwise leave it unset so the cluster
        # default applies. Inheriting the source's class is what pinned every
        # image to whichever tier its source VM happened to sit on.
        storage_class = req.storage_class

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
        
        # Build DataVolume with PVC clone source.
        # Synthetic naming: generateName + display-name annotation + slug label.
        dv = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "generateName": f"{slug}-",
                "namespace": target_namespace,
                "labels": {
                    MANAGED_LABEL: "true",
                    "kubevirt-ui.io/os-type": req.os_type,
                    "kubevirt-ui.io/cloned-from": f"{req.source_namespace}/{req.source_disk_name}",
                    SLUG_LABEL: slug,
                },
                "annotations": {
                    DISPLAY_NAME_ANNOTATION: display_name_value,
                },
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
            display_name=display_name_value,
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
