"""Image endpoints (CDI DataVolumes, and the Harbor catalogue).

Split out of templates.py, which had grown past 1800 lines. Templates and
images are separate concerns that happened to share a file.
"""

import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.images_catalog import (
    assert_catalogue_ref_visible,
    catalog_images,
    merge,
)
# One implementation of the namespace guard, not two. storage.py is where it
# was written and where its docstring records the bug class it exists for
# (a namespace in a query string is not an authorisation); importing it keeps
# a second copy from drifting away from that one.
from app.api.v1.storage import require_namespace_access
from app.core.auth import User, require_auth
from app.core.errors import (
    validate_harbor_project,
    validate_k8s_name,
    validate_oci_repository,
    validate_oci_tag,
)
from app.core.harbor_client import (
    HarborNotFound,
    HarborUnauthorized,
    HarborUnavailable,
    harbor_ca_configmap_name,
    harbor_registry_host,
    harbor_robot_secret_name,
)
from app.core.image_publish import assert_tag_is_free, publish_dependents, publish_job
from app.core.operator import (
    OPERATOR_GROUP,
    OPERATOR_VERSION,
    OWNER_KIND_LABEL,
    OWNER_NAME_LABEL,
    harbor_image_path_enabled,
    image_path_enabled,
)
from app.core.naming import DISPLAY_NAME_ANNOTATION, SLUG_LABEL, sanitize_display_name
from app.core.storage_headroom import assert_storage_headroom
from app.models.template import (
    GoldenImage,
    GoldenImageCreate,
    GoldenImageListResponse,
    GoldenImageUpdate,
    CreateImageFromDiskRequest,
    ImagePublishRequest,
)

images_router = APIRouter()
logger = logging.getLogger(__name__)

# Shared with templates.py, which still writes it on the legacy template-create
# path. Duplicated rather than imported across the two files to keep this
# module independent — same value, "kubevirt-ui.io/managed".
MANAGED_LABEL = "kubevirt-ui.io/managed"

# Folder hierarchy constants (used by list_golden_images to resolve ancestor
# folder namespaces when scoping images to a project/folder).
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

    NOT PAGED, deliberately. The obvious next step — `page`/`page_size` over
    the merged list — cannot be taken until search moves to the server: the
    UI's search (`frontend/src/pages/storageFilters.ts`) filters whatever rows
    it was given, so a paged response would make search find matches on the
    current page only. That is a worse failure than a large payload: it looks
    like the image simply is not there. Page the endpoint in the same change
    that moves search server-side, never before it.

    The Harbor client underneath DOES follow every page (see
    `harbor_client._get`), which is a separate and unconditional correctness
    fix — a truncated artifact list makes an occupied tag look free.
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
        # `catalog_available` defaults to True, so returning the bare response
        # here asserted that the catalogue was complete while Harbor had not
        # been asked at all — an empty list that sounds authoritative. A caller
        # with no enabled namespace, or one whose namespace listing failed
        # above (logged and continued), got exactly that.
        #
        # False is the honest answer: nothing was read, so nothing is claimed.
        # Serving the catalogue rows here instead would be better still —
        # they do not depend on a namespace — and is left out deliberately,
        # because doing it properly means moving the Harbor block above this
        # return and that is a change to the shape of this handler, not a fix.
        return GoldenImageListResponse(items=[], total=0, catalog_available=False)
    
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
                        progress=(status_obj.get("progress") or None),
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

        # --- catalogue half -------------------------------------------------
        # Off by default. With the flag unset nothing below runs, no Harbor
        # call is made, and `images` is exactly the cluster list built above.
        #
        # `catalog_available` is assigned here rather than inside the branch,
        # and the reason is worth being precise about because the comment that
        # used to sit here claimed a "byte-identical response" that the code
        # does not produce on its own: the field is always present in the
        # response body, flag or no flag. What makes that harmless is that
        # `VMImageListResponse.catalog_available` defaults to True, so the
        # value serialised with the flag off is the model's own default — the
        # same body a build without this assignment would emit. What the flag
        # actually guarantees is the absence of the Harbor round trip and of
        # any catalogue row, not the absence of the field.
        catalog_available = True
        if harbor_image_path_enabled():
            if not user.raw_token:
                # AUTH_TYPE=none (documented dev mode) returns a User with no
                # raw_token — get_current_user never had one to carry. Sending
                # an empty bearer would draw Harbor's 401 and log identically
                # to a rejected/expired token, sending whoever reads that log
                # chasing a token-expiry theory for a deployment that has no
                # auth configured at all. Different condition, different
                # message — and the round-trip can never succeed anyway, so
                # skip it rather than waste it.
                logger.info(
                    "no caller token to forward to harbor (unauthenticated "
                    "request or AUTH_TYPE=none); listing cluster images only"
                )
                catalog_available = False
            else:
                try:
                    # Inside the try, not above it. `app.state.harbor_client`
                    # is absent if the lifespan never ran or ran partially,
                    # and an AttributeError raised one line higher is not an
                    # ApiException — it escaped every handler here and became
                    # a 500 on the whole list, which is the one thing this
                    # block promises cannot happen.
                    harbor = request.app.state.harbor_client
                    catalog, complete = await catalog_images(harbor, user.raw_token)
                    images = merge(images, catalog)
                    catalog_available = complete
                except HarborUnauthorized:
                    logger.info("harbor rejected the caller's token; listing cluster images only")
                    catalog_available = False
                except HarborUnavailable as exc:
                    logger.warning("harbor unreachable (%s); listing cluster images only", exc)
                    catalog_available = False
                except Exception:
                    # Deliberately broad, and it is the promise this endpoint
                    # makes: "GET /images never fails because Harbor failed".
                    # Catching only the two designed exceptions kept that
                    # promise for the two failures we thought of — an
                    # AttributeError off a row Harbor returned as something
                    # other than a dict, or a ValueError out of a zip(),
                    # escaped past `except ApiException` below and 500'd the
                    # whole page, cluster rows and all. The catalogue is an
                    # enrichment; nothing about it is worth losing the disks
                    # the user already has.
                    logger.error(
                        "catalogue read failed unexpectedly; listing cluster "
                        "images only",
                        exc_info=True,
                    )
                    catalog_available = False

        return GoldenImageListResponse(
            items=images, total=len(images), catalog_available=catalog_available
        )

    except ApiException as e:
        logger.error(f"Failed to list images: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list images: {e.reason}",
        )


async def _refuse_a_duplicate_image(
    k8s_client: Any, namespace: str, display_name: str,
) -> None:
    """An image with this name in this namespace already exists.

    While images were named by the user, a second one with the same name was
    refused by Kubernetes for free. Moving to `generateName` — which fixed
    real collisions and let two people import the same distribution — removed
    that refusal and put nothing in its place, so importing the same image
    twice produced two objects with one display name, distinguishable only by
    a synthetic suffix nobody sees:

        UAT Ubuntu 22.04   uat-ubuntu-dkvpb   10Gi   InUse   1 VM
        UAT Ubuntu 22.04   uat-ubuntu-x8czz   10Gi   Ready   -

    Both appear in the image picker, identically, and the second one pulls the
    same gigabyte into Ceph again. Reported as D-2 in UAT run 4.

    Creating a tenant and creating a template both already answer this case in
    almost these words; images were the only one that did not.
    """
    if not display_name:
        return
    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    for plural, group, version in (
        ("datavolumes", "cdi.kubevirt.io", "v1beta1"),
        ("managedimages", OPERATOR_GROUP, OPERATOR_VERSION),
    ):
        try:
            existing = await custom_api.list_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural,
            )
        except ApiException as e:
            # A CRD that is not installed says nothing about duplicates.
            if e.status == 404:
                continue
            raise
        for item in existing.get("items", []):
            meta = item.get("metadata", {}) or {}
            spec = item.get("spec", {}) or {}
            name = (
                (meta.get("annotations") or {}).get(DISPLAY_NAME_ANNOTATION)
                or spec.get("displayName")
                or meta.get("name", "")
            )
            if name != display_name:
                continue
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"An image called '{display_name}' already exists in "
                    f"{namespace} (as '{meta.get('name')}'). Pick another "
                    f"name, or delete that one first — two images with one "
                    f"name cannot be told apart in the picker, and the second "
                    f"import downloads the same content again."
                ),
            )


def _unbuilt_source_url(source: dict[str, Any]) -> str | None:
    """The displayable source URL of a ManagedImage that has no disk yet.

    This was `source.get("url") or source.get("registry")`, and both halves
    were wrong. `_create_managed_image` writes the same nested dict CDI uses —
    `{"http": {"url": ...}}`, `{"registry": {"url": ..., "secretRef": ...}}`,
    `{"pvc": {...}}`, `{"blank": {}}` — so the first half never matched
    anything, and the second returned a **dict** for a registry source. Fed to
    `VMImage.source_url`, typed `str | None`, that is a Pydantic
    ValidationError, raised inside the list handler's `try` whose only
    `except` is `ApiException` — so it escaped as a 500 that took out the
    ENTIRE image list, for every namespace, for as long as one
    registry-sourced ManagedImage stayed unbuilt.

    It predates this wave but the wave makes it far likelier: `{"registry":
    {...}}` is now the guaranteed shape for every catalogue image, and the
    window it needs is exactly the seconds between materialising one and the
    operator building it.

    Returning the registry URL properly also fixes a second, quieter thing:
    `merge()` re-joins a row with its catalogue entry through
    `catalog_ref_from_source_url(source_url)`, so an unbuilt catalogue image
    with no URL appeared as a second row beside the catalogue row it came
    from — the same "one image, two rows" the frontend fix exists to prevent.

    The flat `{"url": ...}` form is still read last, because older resources
    (and this module's own tests) carry it.
    """
    for key in ("http", "registry"):
        nested = source.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("url"), str):
            return nested["url"]

    pvc = source.get("pvc")
    if isinstance(pvc, dict) and pvc.get("name"):
        # Same rendering the built row uses, so the two agree.
        return f"pvc:{pvc.get('namespace', '')}/{pvc['name']}"

    if source.get("blank") is not None:
        return "blank"

    flat = source.get("url")
    return flat if isinstance(flat, str) else None


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
                progress=(status_obj.get("progress") or None),
                name=name,
                namespace=ns,
                display_name=spec.get("displayName") or name,
                description=spec.get("description"),
                os_type=labels.get("kubevirt-ui.io/os-type"),
                os_version=labels.get("kubevirt-ui.io/os-version"),
                size=spec.get("size", "Unknown"),
                status=display,
                error_message=(message or reason) if display == "Error" else None,
                source_url=_unbuilt_source_url(source),
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


async def _configmap_exists(k8s_client: Any, namespace: str, name: str) -> bool:
    """Whether a ConfigMap by this name is in this namespace.

    Used only for the optional CA: `certConfigMap` naming something absent
    makes CDI refuse the import, so a convention-derived name has to be
    checked rather than assumed. Any error other than a clean 404 is treated
    as "not there" too — the CA is optional, and failing a materialise
    because a read of an optional object hiccuped would be worse than
    pulling without it, which either works (public CA) or fails with CDI's
    own TLS error.
    """
    try:
        await k8s_client.core_api.read_namespaced_config_map(
            name=name, namespace=namespace
        )
        return True
    except ApiException as exc:
        if exc.status != 404:
            logger.warning(
                "could not read ConfigMap %s/%s (%s); pulling without a CA",
                namespace, name, exc.status,
            )
        return False
    except Exception:
        # Deliberately broad, and the docstring above is the contract this
        # keeps: `except ApiException` alone let a transport error — a reset
        # connection, a DNS blip, a timeout — escape as a 500 that failed the
        # whole materialise over an OPTIONAL object. Logged rather than
        # swallowed silently, since a read that keeps failing is worth
        # noticing even when it is not worth refusing on.
        logger.warning(
            "could not read ConfigMap %s/%s; pulling without a CA",
            namespace, name, exc_info=True,
        )
        return False


def registry_host_of(url: str) -> str:
    """The bare host[:port] a `docker://host/path:tag` URL resolves against.

    Deliberately conservative. Anything that is not a `docker://` URL with a
    plain `host[:port]` authority returns "" — no scheme of another kind, no
    userinfo (`docker://harbor.example@attacker.tld/...` authenticates to
    attacker.tld, and reading the host off the wrong side of the `@` is the
    classic way to be fooled here), no empty authority. "" never equals a
    configured Harbor host, so anything unparseable gets no credential.
    """
    if not url or not url.startswith("docker://"):
        return ""
    parsed = urlparse(url)
    netloc = parsed.netloc
    if not netloc or "@" in netloc:
        return ""
    return netloc


async def _harbor_credentials_for(
    k8s_client: Any, namespace: str, registry_url: str, *, required: bool
) -> tuple[str | None, str | None]:
    """(secretRef, certConfigMap) for a registry URL — Harbor's host only.

    THE CREDENTIAL IS DERIVED FROM THE RESOLVED HOST, NEVER FROM THE REQUEST.
    A caller used to be able to send `source_registry_secret` alongside an
    arbitrary `source_registry`, which handed the tenant's Harbor robot
    password to whatever registry the caller named. There is no field to send
    any more, and this function is the only thing that attaches one: if the
    URL does not resolve to `harbor_registry_host()`, both halves are None and
    the pull is anonymous — the behaviour every registry source had before the
    catalogue existed.

    The Secret is named by convention (`harbor_robot_secret_name()`): one name,
    the same in every tenant namespace, and what makes it "the tenant's" is
    which namespace it is in — which is also the only namespace CDI resolves it
    in. Checked here, before anything is created: created with a `secretRef`
    that does not resolve, the DataVolume fails much later inside CDI as an
    import error that never mentions the Secret.

    `required` says what an absent Secret means. For a CATALOGUE selection it
    is a refusal (422 naming the Secret and the namespace) — the credential is
    the whole point, and silently pulling anonymously is the defect this
    replaces. For a caller-supplied `source_registry` that merely happens to
    point at Harbor it is not: that request pulled anonymously before this
    feature existed and still does, because a deployment with a public Harbor
    project and no robot Secret is not misconfigured.

    The CA is genuinely optional — a Harbor behind a publicly trusted
    certificate has no ConfigMap to name, and CDI refuses the import outright
    if `certConfigMap` points at nothing — so it is attached only when a
    ConfigMap by that name is really there.
    """
    harbor_host = harbor_registry_host()
    if not harbor_host or registry_host_of(registry_url) != harbor_host:
        return None, None

    secret: str | None = harbor_robot_secret_name() or None
    if secret and not await _secret_exists(k8s_client, namespace, secret):
        if required:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Secret '{secret}' not found in namespace "
                    f"'{namespace}'. CDI resolves secretRef in the "
                    "DataVolume's own namespace; the harbor-robots chart "
                    "provisions it there."
                ),
            )
        logger.info(
            "no %s Secret in %s; pulling %s anonymously",
            secret, namespace, registry_url,
        )
        secret = None

    # Independent of the credential: the CA is about trusting the registry's
    # TLS, which an anonymous pull needs just as much as an authenticated one.
    ca_name = harbor_ca_configmap_name()
    ca = (
        ca_name
        if ca_name and await _configmap_exists(k8s_client, namespace, ca_name)
        else None
    )
    return secret, ca


async def _secret_exists(k8s_client: Any, namespace: str, name: str) -> bool:
    """Whether a Secret by this name is in this namespace.

    Unlike the optional CA, only a clean 404 counts as "not there": anything
    else is propagated. Treating a transport error as absence here would
    downgrade an authenticated pull to an anonymous one on a hiccup, which is
    the silent failure this whole path exists to avoid.
    """
    try:
        await k8s_client.core_api.read_namespaced_secret(
            name=name, namespace=namespace
        )
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise


def build_registry_source(
    url: str, secret_ref: str | None, cert_config_map: str | None
) -> dict[str, Any]:
    """Render spec.source.registry for a DataVolume.

    secret_ref names a Secret in the DataVolume's own namespace holding the
    tenant's Harbor robot credential; CDI will not look in any other namespace.
    Pulling is a registry operation, which is the one place robot accounts do
    work — the user's own token is for browsing and is useless here.
    """
    registry: dict[str, Any] = {"url": url}
    if secret_ref:
        registry["secretRef"] = secret_ref
    if cert_config_map:
        registry["certConfigMap"] = cert_config_map
    return {"registry": registry}


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
    # The namespace arrives as a query parameter and is read with the UI's own
    # ServiceAccount, which can read every namespace. Until this line the only
    # thing between a caller and someone else's project was knowing its name.
    #
    # It predates the catalogue, and the catalogue is what made it load-bearing:
    # this handler now attaches THAT namespace's Harbor robot credential to the
    # pull, so an unchecked namespace spends another tenant's credential, hits
    # another tenant's quota, and leaves the disk where they can see it.
    #
    # 404 rather than 403, matching storage.py and the VM path: whether a
    # namespace exists is not this caller's business either.
    await require_namespace_access(request, user, namespace)

    k8s_client = request.app.state.k8s_client

    # Refused before anything is written, like a tenant and a template with a
    # name that is taken.
    await _refuse_a_duplicate_image(k8s_client, namespace, image.display_name)

    # Asked before the DataVolume exists, because the quota counts the PVC
    # that CDI makes from it and not the DataVolume itself — which is how a
    # 100Gi disk against a 12Gi quota came back 201 and then never appeared.
    await assert_storage_headroom(
        k8s_client, namespace, image.size,
        # The display name, not `name`: that one is an optional seed for the
        # kubernetes name and is empty on the ordinary path, so the refusal
        # read "None asks for 400Gi" — a message about a disk with no name.
        what=f"{image.display_name or image.name or 'this disk'!r}",
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
        
        # --- a catalogue selection becomes a registry source ---------------
        # The browser sends back the host-less `catalog_ref` GET /images gave
        # it ("project/repo:tag") and nothing else. Everything the pull
        # actually needs is added here:
        #
        #   the registry host — from harbor_registry_host(), the single place
        #     it is known, already used by the publish path. Sent host-less to
        #     CDI, "project/repo:tag" resolves against Docker Hub.
        #
        #   the docker:// scheme — required by CDI, and ALSO what
        #     catalog_ref_from_source_url() parses back out of the stored
        #     source_url to re-join this disk with its catalogue row. Without
        #     it the unified list shows one image as two rows, forever.
        #
        #   the credential — see _harbor_credentials_for below. It is derived
        #     from the RESOLVED host, never named by the caller.
        registry_url = image.source_registry
        if image.catalog_ref:
            # The catalogue path is the Harbor path. Gated with the same flag
            # the list and publish paths use, so "HARBOR_IMAGE_ENABLED unset
            # means Harbor is not reachable from this API" is true of all
            # three, not two of three.
            if not harbor_image_path_enabled():
                raise HTTPException(
                    status_code=501, detail="Harbor image path is disabled"
                )
            if registry_url:
                # Two different sources for one disk. Refused rather than
                # silently preferring one — the old code preferred
                # source_registry, which is how a caller-chosen registry came
                # to be paired with a Harbor credential in the first place.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "send either 'catalog_ref' or 'source_registry', not "
                        "both — they name two different images"
                    ),
                )
            host = harbor_registry_host()
            if not host:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "HARBOR_URL is not configured, so a catalogue image "
                        "cannot be turned into a disk"
                    ),
                )
            # The catalogue is read as the caller and pulled as the robot, and
            # until this line nothing joined the two: any `catalog_ref` that
            # matched the pattern was fetched with the namespace robot, whose
            # read covers the whole registry rather than the caller's slice of
            # it. Harbor answers per user, so the caller's own token is what
            # decides — and a rejected identity is refused rather than being
            # handed the disk anyway.
            raw_token = getattr(user, "raw_token", None)
            if not raw_token:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "a catalogue image needs your own identity to check "
                        "what you may see, and this request carries none"
                    ),
                )
            try:
                await assert_catalogue_ref_visible(
                    request.app.state.harbor_client, raw_token, image.catalog_ref,
                )
            except HarborUnauthorized as exc:
                raise HTTPException(
                    status_code=403, detail="Harbor rejected your identity",
                ) from exc
            except HarborNotFound as exc:
                # Same answer for "no such artifact" and "not yours", so the
                # catalogue cannot be mapped by elimination.
                raise HTTPException(
                    status_code=404,
                    detail=f"No catalogue image '{image.catalog_ref}'",
                ) from exc
            except HarborUnavailable as exc:
                # Never a pull on a guess: if Harbor cannot say, we do not act.
                raise HTTPException(
                    status_code=503,
                    detail="Harbor is unreachable, so this cannot be checked",
                ) from exc

            registry_url = f"docker://{host}/{image.catalog_ref}"

        # THE CREDENTIAL FOLLOWS THE HOST, AND ONLY THE HOST.
        #
        # `source_registry` is a caller-supplied URL and always has been. What
        # changed — and what had to change back — is that the request also used
        # to carry `source_registry_secret`, so `source_registry:
        # docker://attacker.tld/x:1` plus `source_registry_secret:
        # harbor-robot` made CDI authenticate to an attacker's registry with
        # the tenant's Harbor robot password. Deriving the credential from the
        # resolved host removes the class: a URL that does not resolve to the
        # configured Harbor gets no credential at all, which is an anonymous
        # pull — precisely what it got before this feature existed.
        registry_secret, registry_ca = (None, None)
        if registry_url:
            registry_secret, registry_ca = await _harbor_credentials_for(
                k8s_client, target_namespace, registry_url,
                required=bool(image.catalog_ref),
            )

        # Determine source
        source_url_display = None
        if image.source_url:
            source = {"http": {"url": image.source_url}}
            source_url_display = image.source_url
        elif registry_url:
            source = build_registry_source(
                registry_url,
                registry_secret,
                registry_ca,
            )
            source_url_display = registry_url
        elif image.source_pvc:
            # Clone from existing PVC
            pvc_ns = image.source_pvc_namespace or target_namespace
            source = {"pvc": {"name": image.source_pvc, "namespace": pvc_ns}}
            source_url_display = f"pvc:{pvc_ns}/{image.source_pvc}"
        else:
            # Blank disk (for data disks)
            source = {"blank": {}}
            source_url_display = "blank"

        # NB: the robot Secret's existence was already checked, above, by
        # `_harbor_credentials_for` — before `source` was built and therefore
        # before either writer runs, so the DataVolume path and the
        # ManagedImage path get the same refusal.

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
    # See create_golden_image: the namespace is a query parameter read with a
    # ServiceAccount that can see every namespace. Deleting is the reading
    # gap's louder half — it does not leak someone else's image, it removes it.
    await require_namespace_access(request, user, namespace)

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
    # See create_golden_image. Renaming someone else's image is quieter than
    # deleting it and harder to notice afterwards.
    await require_namespace_access(request, user, namespace)

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

    # Both ends, and both for their own reason: the source is what gets read
    # and copied, the target is where the copy lands and whose quota pays. A
    # check on one of them is a hole with extra steps — read someone else's
    # disk into your own namespace, or push a disk into theirs.
    await require_namespace_access(request, user, req.source_namespace)
    if target_namespace != req.source_namespace:
        await require_namespace_access(request, user, target_namespace)

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


# ---------------------------------------------------------------------------
# Publish (snapshot-then-publish a running VM's disk to the catalogue)
# ---------------------------------------------------------------------------

# The last-resort disk size, used only when a PVC reports neither a bound
# capacity nor a requested one — which should not happen, and did not stay
# hypothetical: `status.capacity` alone is empty for the whole of a Pending
# PVC's life, so a 200Gi disk waiting on WaitForFirstConsumer was published
# into a 10Gi temporary PVC and a 12Gi scratch. The failure arrives ~25
# minutes in, as an ENOSPC inside `dd`, on a Job nobody is watching.
_FALLBACK_DISK_CAPACITY = "10Gi"


def _source_pvc_capacity(source_pvc: Any) -> str:
    """The size of the disk being published, as a Kubernetes quantity.

    `status.capacity` is authoritative once a PVC is Bound, and empty until
    then. `spec.resources.requests.storage` is what was asked for and exists
    from creation, so it is the right fallback: for a Bound PVC the two agree
    (or status is larger, after an expand), and for a Pending one it is the
    only number there is.
    """
    status = getattr(source_pvc, "status", None)
    capacity = getattr(status, "capacity", None) or {}
    bound = capacity.get("storage") if hasattr(capacity, "get") else None
    if bound:
        return str(bound)

    spec = getattr(source_pvc, "spec", None)
    resources = getattr(spec, "resources", None)
    requests = getattr(resources, "requests", None) or {}
    requested = requests.get("storage") if hasattr(requests, "get") else None
    if requested:
        return str(requested)

    logger.warning(
        "publish: source PVC reports neither a bound nor a requested "
        "capacity; falling back to %s", _FALLBACK_DISK_CAPACITY,
    )
    return _FALLBACK_DISK_CAPACITY


async def _volume_snapshot_class_for(
    k8s_client: Any, storage_class_name: str
) -> str:
    """The VolumeSnapshotClass to snapshot a PVC on `storage_class_name` with.

    Omitting `volumeSnapshotClassName` only works where the cluster has a
    class annotated as default, and this codebase already knows better:
    `disks.py`'s create_disk_snapshot detects one explicitly rather than
    assuming. A snapshot created against no class is left Pending with a
    `no VolumeSnapshotClass found` event — no ApiException, nothing the
    publish handler can see — so the temporary PVC never binds and the Job
    runs out its deadline.

    Preference is a class whose `driver` matches the source StorageClass's
    provisioner, because a cluster with more than one CSI driver has more than
    one class and the first one alphabetically is a coin flip. Falls back to
    the first class, then to "" (leave the field off, and let a cluster-default
    class apply if there is one).
    """
    custom_api = client.CustomObjectsApi(k8s_client._api_client)
    try:
        listed = await custom_api.list_cluster_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            plural="volumesnapshotclasses",
        )
    except Exception:
        logger.warning(
            "publish: could not list VolumeSnapshotClasses; creating the "
            "snapshot without one", exc_info=True,
        )
        return ""

    classes = [c for c in listed.get("items", []) if isinstance(c, dict)]
    if not classes:
        return ""

    provisioner = ""
    if storage_class_name:
        try:
            storage_api = client.StorageV1Api(k8s_client._api_client)
            sc = await storage_api.read_storage_class(name=storage_class_name)
            provisioner = getattr(sc, "provisioner", "") or ""
        except Exception:
            logger.info(
                "publish: could not read StorageClass %r; picking a snapshot "
                "class without matching its provisioner", storage_class_name,
            )

    if provisioner:
        for candidate in classes:
            if candidate.get("driver") == provisioner:
                return candidate.get("metadata", {}).get("name", "") or ""

    return classes[0].get("metadata", {}).get("name", "") or ""


async def create_object(k8s_client: Any, obj: dict[str, Any]) -> dict[str, Any]:
    """Create a k8s object described as a plain dict, and return it as one.

    `publish_job`/`publish_dependents` describe three different kinds — Job,
    PersistentVolumeClaim, VolumeSnapshot — that live behind three different
    client APIs (BatchV1Api, CoreV1Api, CustomObjectsApi). Dispatching on
    `kind` here lets the handler treat all three the same way, and matters
    because the Job's UID has to be read back from the object this returns
    before the dependents that name it as their owner can be created.
    """
    kind = obj["kind"]
    namespace = obj["metadata"]["namespace"]

    if kind == "Job":
        batch_api = client.BatchV1Api(k8s_client._api_client)
        result = await batch_api.create_namespaced_job(namespace=namespace, body=obj)
        return result.to_dict()

    if kind == "PersistentVolumeClaim":
        result = await k8s_client.core_api.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=obj
        )
        return result.to_dict()

    if kind == "VolumeSnapshot":
        # Named literally (not read off obj["apiVersion"]) so the RBAC
        # contract scan (tests/test_helm_rbac_contract.py) can see this call
        # site and check the chart grants it.
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        return await custom_api.create_namespaced_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            namespace=namespace,
            plural="volumesnapshots",
            body=obj,
        )

    raise ValueError(f"create_object: unsupported kind {kind!r}")


async def unsuspend_job(k8s_client: Any, namespace: str, name: str) -> None:
    """Flip a suspended Job on, once its dependents exist and are owned by it."""
    batch_api = client.BatchV1Api(k8s_client._api_client)
    await batch_api.patch_namespaced_job(
        name=name, namespace=namespace, body={"spec": {"suspend": False}},
    )


async def delete_job(k8s_client: Any, namespace: str, name: str) -> None:
    """Delete the publish Job — its ownerReferences take the dependents with it."""
    batch_api = client.BatchV1Api(k8s_client._api_client)
    await batch_api.delete_namespaced_job(
        name=name, namespace=namespace, propagation_policy="Background",
    )


@images_router.post("/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish_image(
    request: Request,
    req: ImagePublishRequest,
    user: User = Depends(require_auth),
) -> dict[str, str]:
    """Publish a disk to the catalogue without stopping the VM using it."""
    if not harbor_image_path_enabled():
        raise HTTPException(status_code=501, detail="Harbor image path is disabled")

    validate_k8s_name(req.namespace, "namespace")
    validate_k8s_name(req.disk_name, "disk_name")
    # The Secret is named by the server, not by the caller — the same rule the
    # pull direction adopted, and the same reason: a credential a request can
    # name is a credential a request can pick. `namespace` is checked against
    # the caller's own bindings below, but any Secret in a namespace you hold
    # that happens to carry accessKeyId/secretKey could be selected and used
    # for `crane auth login`. One convention, one name, both directions.
    secret_name = harbor_robot_secret_name()
    validate_k8s_name(secret_name, "secret_name")
    # NOT validate_k8s_name. A Kubernetes name and an image coordinate are
    # different alphabets: the k8s rule has no dots, no underscores and no
    # uppercase, so `v1.0.0`, `24.04` and `ubuntu_22` were all refused with a
    # 422 blaming the caller for perfectly ordinary tags — while `project` and
    # `repository`, which are interpolated into Harbor API paths and into the
    # Job's push reference, were not checked at all.
    validate_harbor_project(req.project, "project")
    validate_oci_repository(req.repository, "repository")
    validate_oci_tag(req.tag, "tag")

    k8s_client = request.app.state.k8s_client

    # The namespace comes from the request body, and everything below reads or
    # writes in it with the UI's own ServiceAccount, which can reach every
    # namespace: a Secret is read, a PVC is read, and a Job is created that
    # exports that PVC's bytes to an external registry. `validate_k8s_name`
    # says the string is a legal name, not that it is the caller's.
    #
    # The pre-existing image endpoints never checked either, so this branch
    # regressed nothing — but publish is the one endpoint here that copies disk
    # contents OUT of the cluster, which makes it the wrong place to inherit
    # the weaker convention. Same check, same 404-not-403 reasoning, as
    # storage.py's routes.
    await require_namespace_access(request, user, req.namespace)

    # The Job pushes as the tenant robot, whose credential lives in a Secret
    # in this same namespace (the harbor-robots chart provisions it there —
    # the same Secret CDI pulls with). Refused here, before anything is
    # created, for the same reason Task 5's pull-direction check runs first:
    # a Job that dies for a missing Secret reports a container failure that
    # never names the Secret.
    try:
        await k8s_client.core_api.read_namespaced_secret(
            name=secret_name, namespace=req.namespace,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Secret '{secret_name}' not found in namespace "
                    f"'{req.namespace}'. Publishing pushes as the tenant "
                    "robot; the harbor-robots chart provisions its "
                    "credential there."
                ),
            ) from exc
        raise

    # Read the source PVC before anything is created. This both confirms the
    # disk exists (a 404 rather than a snapshot pointed at nothing) and
    # supplies the storage class / volume mode / access modes the temporary
    # PVC must match to have any real chance of binding — see
    # disks.py's rollback_snapshot, which restores from a snapshot the same
    # way.
    try:
        source_pvc = await k8s_client.core_api.read_namespaced_persistent_volume_claim(
            name=req.disk_name, namespace=req.namespace,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Disk '{req.disk_name}' not found in namespace '{req.namespace}'",
            ) from exc
        raise

    access_modes = source_pvc.spec.access_modes or ["ReadWriteOnce"]
    storage_class = source_pvc.spec.storage_class_name or ""
    volume_mode = source_pvc.spec.volume_mode or "Block"
    source_capacity = _source_pvc_capacity(source_pvc)

    # No token, no publish. `user.raw_token or ""` used to stand here, which
    # sent Harbor an empty bearer: against a public project Harbor answers
    # that 200, so the tag check "passed" while proving nothing, and against
    # a private one it drew a 401 that read exactly like an expired session.
    # Neither is a state this endpoint can proceed from — publishing needs a
    # real identity — so it is refused here, distinctly, the way the list
    # endpoint distinguishes it.
    if not user.raw_token:
        raise HTTPException(
            status_code=401,
            detail=(
                "no caller token to forward to Harbor (unauthenticated request "
                "or AUTH_TYPE=none); publishing needs a real Harbor identity"
            ),
        )

    harbor = request.app.state.harbor_client
    try:
        # Identity first, for the same reason the list endpoint checks it
        # first: Harbor's list endpoints answer 200 for any bearer, filtered
        # to what that identity can see, so a garbage token walking an empty
        # artifact list would find the tag "free" and publish over whatever
        # is actually there.
        await harbor.verify_identity(user.raw_token)
        await assert_tag_is_free(
            harbor, user.raw_token, req.project, req.repository, req.tag
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HarborUnauthorized as exc:
        # The caller's fault, and actionable: re-authenticate, or ask for
        # access to this project.
        raise HTTPException(
            status_code=403,
            detail=(
                "Harbor rejected your identity; the catalogue cannot be checked "
                "for this tag, so nothing was published"
            ),
        ) from exc
    except HarborUnavailable as exc:
        # NOT the caller's fault. Publishing without a working tag check would
        # mean pushing over a tag that may already exist, which CDI never
        # re-imports — so this fails rather than proceeding blind.
        logger.warning("publish: harbor unreachable (%s)", exc)
        raise HTTPException(
            status_code=502,
            detail=(
                "Harbor could not be reached to check whether the tag is free; "
                "nothing was published. Try again once it recovers."
            ),
        ) from exc

    # catalog_ref shape (no host) — matches images_catalog.py's merge key, and
    # is what the response and Harbor's own project/repository/tag naming use.
    # The registry host is added only for the Job's own push target, below.
    ref = f"{req.project}/{req.repository}:{req.tag}"
    registry = harbor_registry_host()

    # Suspended first, so its UID can own the snapshot and the temporary PVC.
    #
    # This create sits OUTSIDE the rollback try below — there is nothing to
    # roll back until it succeeds — so its own failures have to be answered
    # here or they escape as a 500. A 409 is the one worth naming: Job names
    # are unique per publish now, but `ttlSecondsAfterFinished` keeps a
    # finished Job for an hour and a same-name collision is still possible,
    # and "something is already publishing this" is a fact the caller can act
    # on, unlike a stack trace.
    try:
        job = await create_object(
            k8s_client,
            publish_job(
                req.namespace, req.disk_name, ref,
                registry=registry, secret_name=secret_name,
                volume_mode=volume_mode,
                # The deadline follows the disk: everything the Job does is
                # proportional to the number of bytes it has to move.
                source_size=source_capacity,
            ),
        )
    except ApiException as exc:
        if exc.status == 409:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a publish job for '{req.disk_name}' already exists in "
                    f"'{req.namespace}'; wait for it to finish before starting "
                    "another"
                ),
            ) from exc
        raise HTTPException(
            status_code=422, detail=f"publish could not start: {exc.reason}"
        ) from exc

    job_name = job["metadata"]["name"]
    job_uid = job["metadata"]["uid"]

    try:
        # [snapshot, temporary PVC] always; a third element — a Filesystem
        # scratch PVC the Job `dd`s the raw device into before tarring —
        # only when the source is Block. A Block PVC has no filesystem for
        # `volumeMounts` to attach to, so `publish_job` attaches it as a raw
        # device instead; see `image_publish.py` for the container-spec side
        # of this same branch.
        dependents = publish_dependents(
            req.namespace, req.disk_name, job_name, job_uid,
            storage_class=storage_class, volume_mode=volume_mode,
            access_modes=access_modes, storage_size=source_capacity,
            snapshot_class=await _volume_snapshot_class_for(
                k8s_client, storage_class
            ),
        )
        snapshot_obj, pvc_obj, *extra_dependents = dependents
        created_snapshot = await create_object(k8s_client, snapshot_obj)

        # The snapshot's own restoreSize when the storage backend already
        # reports one (rare, immediately after creation), the source disk's
        # own capacity otherwise — never "0", which most CSI provisioners
        # refuse by leaving the PVC Pending forever, a failure with no
        # ApiException this handler could ever see.
        restore_size = (
            (created_snapshot.get("status") or {}).get("restoreSize")
            or source_capacity
        )
        pvc_obj["spec"]["resources"]["requests"]["storage"] = restore_size

        await create_object(k8s_client, pvc_obj)
        for extra in extra_dependents:
            await create_object(k8s_client, extra)
        await unsuspend_job(k8s_client, req.namespace, job_name)
    except Exception as exc:
        # Cleanup here has to be unconditional, not just for ApiException: a
        # suspended Job never reaches a terminal phase, so a timeout or a
        # connection reset in this window would otherwise leave the Job —
        # and everything it owns — permanently un-reaped. That is the exact
        # orphan the whole suspended-Job-as-owner design exists to prevent,
        # arriving through the one door it didn't cover.
        try:
            await delete_job(k8s_client, req.namespace, job_name)
        except Exception:
            logger.error(
                "publish %s: failed to roll back the Job after %r",
                job_name, exc, exc_info=True,
            )
        if isinstance(exc, ApiException):
            raise HTTPException(
                status_code=422, detail=f"publish could not start: {exc.reason}"
            ) from exc
        raise

    return {"job": job_name, "ref": ref}
