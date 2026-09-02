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
    template_path_enabled,
)
from app.models.template import (
    TemplateCloudInit,
    TemplateCompute,
    TemplateConsole,
    TemplateDisk,
    VMTemplate,
    VMTemplateCreate,
    VMTemplateListResponse,
    VMTemplateUpdate,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
TEMPLATE_CONFIGMAP_NAME = "kubevirt-ui-templates"
TEMPLATE_NAMESPACE = "kubevirt-ui-system"
PROJECT_ENABLED_LABEL = "kubevirt-ui.io/enabled"

# Labels
MANAGED_LABEL = "kubevirt-ui.io/managed"


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
