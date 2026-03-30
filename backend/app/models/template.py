"""VM Template models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TemplateCompute(BaseModel):
    """Compute specifications for a VM template."""

    cpu_cores: int = Field(2, ge=1, le=128, description="Number of CPU cores")
    cpu_sockets: int = Field(1, ge=1, le=8, description="Number of CPU sockets")
    cpu_threads: int = Field(1, ge=1, le=2, description="Threads per core")
    memory: str = Field("4Gi", description="Memory size (e.g., 4Gi, 8Gi)")


class TemplateDisk(BaseModel):
    """Disk configuration for a VM template."""

    size: str = Field("50Gi", description="Disk size (e.g., 50Gi, 100Gi)")
    storage_class: str | None = Field(None, description="StorageClass name (optional)")


class TemplateNetwork(BaseModel):
    """Network configuration for a VM template."""

    type: Literal["default", "multus", "bridge"] = Field(
        "default", description="Network type"
    )
    multus_network: str | None = Field(
        None, description="Multus network name (for type=multus)"
    )


class TemplateCloudInit(BaseModel):
    """Cloud-init configuration for a VM template."""

    user_data: str | None = Field(None, description="Cloud-init user-data (YAML)")
    network_data: str | None = Field(None, description="Cloud-init network-data (YAML)")


class TemplateConsole(BaseModel):
    """Console configuration for a VM template."""

    vnc_enabled: bool = Field(True, description="Enable VNC console (default: true)")
    serial_console_enabled: bool = Field(False, description="Enable serial console (default: false)")


class VMTemplate(BaseModel):
    """VM Template model."""

    name: str = Field(..., description="Template name (Kubernetes resource name)")
    display_name: str = Field(..., description="Human-readable template name")
    description: str | None = Field(None, description="Template description")
    icon: str | None = Field(None, description="Icon name (e.g., ubuntu, centos, windows)")
    category: str = Field("linux", description="Template category")
    os_type: str = Field("linux", description="OS type (linux, windows)")
    
    # Source golden image
    golden_image_name: str = Field(..., description="Golden image DataVolume name")
    golden_image_namespace: str = Field(
        "golden-images", description="Golden image namespace"
    )
    
    # VM specifications
    compute: TemplateCompute = Field(default_factory=TemplateCompute)
    disk: TemplateDisk = Field(default_factory=TemplateDisk)
    network: TemplateNetwork = Field(default_factory=TemplateNetwork)
    cloud_init: TemplateCloudInit | None = Field(None)
    console: TemplateConsole = Field(default_factory=TemplateConsole)
    
    # Metadata
    created: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class VMTemplateCreate(BaseModel):
    """Model for creating a VM template."""

    name: str = Field(
        ...,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
        description="Template name",
    )
    display_name: str = Field(..., max_length=128)
    description: str | None = Field(None, max_length=512)
    icon: str | None = None
    category: str = "linux"
    os_type: str = "linux"
    
    golden_image_name: str
    golden_image_namespace: str  # Must be explicitly specified - same as project namespace
    
    compute: TemplateCompute = Field(default_factory=TemplateCompute)
    disk: TemplateDisk = Field(default_factory=TemplateDisk)
    network: TemplateNetwork = Field(default_factory=TemplateNetwork)
    cloud_init: TemplateCloudInit | None = None
    console: TemplateConsole = Field(default_factory=TemplateConsole)


class VMTemplateUpdate(BaseModel):
    """Model for updating a VM template."""

    display_name: str | None = None
    description: str | None = None
    icon: str | None = None
    category: str | None = None
    
    compute: TemplateCompute | None = None
    disk: TemplateDisk | None = None
    network: TemplateNetwork | None = None
    cloud_init: TemplateCloudInit | None = None
    console: TemplateConsole | None = None


class VMTemplateListResponse(BaseModel):
    """Response model for template list."""

    items: list[VMTemplate]
    total: int


# Golden Image models

class VMImage(BaseModel):
    """VM Image model (DataVolume for VM disks)."""

    name: str
    namespace: str
    display_name: str | None = None
    description: str | None = None
    os_type: str | None = None
    os_version: str | None = None
    size: str | None = None
    status: str  # Ready, InUse, Pending, Error
    error_message: str | None = None  # Error details from DV conditions
    source_url: str | None = None
    created: datetime | None = None
    used_by: list[str] | None = None  # List of VMs using this image
    disk_type: str = "image"  # image or data
    persistent: bool = False  # If true, disk is not cloned
    scope: str = "environment"  # "environment" (single ns) or "project" (all envs)
    project: str | None = None  # Project name (set for project-scoped images)
    environment: str | None = None  # Environment name (from namespace label)


class VMImageCreate(BaseModel):
    """Model for creating a VM image.
    
    If `name` is not provided, it will be auto-generated from `display_name`
    using the pattern {slug}-{uuid6} (e.g. "ubuntu-24-04-server-a7f3e2").
    """

    name: str | None = Field(
        None,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
        description="K8s-safe name. Auto-generated from display_name if not provided.",
    )
    display_name: str | None = Field(None, description="Human-friendly name (stored in annotation)")
    description: str | None = None
    os_type: str = "linux"
    os_version: str | None = None
    
    # Source - one of these
    source_url: str | None = Field(None, description="HTTP URL to download image")
    source_registry: str | None = Field(None, description="Container registry URL")
    source_pvc: str | None = Field(None, description="PVC name to clone from")
    source_pvc_namespace: str | None = Field(None, description="PVC namespace to clone from")
    
    size: str = Field("10Gi", description="Disk size")
    storage_class: str | None = None
    
    # Disk type and persistence
    disk_type: str = Field("image", description="Disk type: image or data")
    persistent: bool = Field(False, description="If true, disk is not cloned")
    
    # Scope: environment (single ns) or project (all envs in project)
    scope: str = Field("environment", description="'environment' or 'project'")
    project: str | None = Field(None, description="Project name (required when scope=project)")


class VMImageListResponse(BaseModel):
    """Response model for VM image list."""

    items: list[VMImage]
    total: int


class VMImageUpdate(BaseModel):
    """Model for updating a VM image (scope, display name, etc.)."""

    scope: str | None = Field(None, description="'environment' or 'project'")
    display_name: str | None = None
    description: str | None = None


# Aliases for backward compatibility
GoldenImage = VMImage
GoldenImageCreate = VMImageCreate
GoldenImageListResponse = VMImageListResponse
GoldenImageUpdate = VMImageUpdate


# Persistent Disk models

class PersistentDisk(BaseModel):
    """Persistent disk model (independent DataVolume)."""

    name: str
    namespace: str
    size: str
    storage_class: str | None = None
    status: str
    attached_to: str | None = None  # VM name if attached
    created: datetime | None = None


class PersistentDiskCreate(BaseModel):
    """Model for creating a persistent disk."""

    name: str = Field(
        ...,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
    )
    size: str = Field("50Gi")
    storage_class: str | None = None
    
    # Optional: clone from image in the same namespace
    source_image: str | None = None


class PersistentDiskListResponse(BaseModel):
    """Response model for persistent disk list."""

    items: list[PersistentDisk]
    total: int


class AttachDiskRequest(BaseModel):
    """Request to attach a disk to a VM."""

    disk_name: str
    vm_name: str
    hotplug: bool = False  # If true, attach without stopping VM


class CreateImageFromDiskRequest(BaseModel):
    """Request to create an image from an existing disk into a project namespace."""

    source_disk_name: str
    source_namespace: str
    
    # Target namespace for the new image (defaults to source_namespace if not provided)
    target_namespace: str | None = None
    
    # New image details
    name: str = Field(
        ...,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
    )
    display_name: str | None = None
    description: str | None = None
    os_type: str = "linux"
    os_version: str | None = None
