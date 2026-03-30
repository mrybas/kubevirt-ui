"""Tenant Pydantic models.

Architecture:
  - Tenant = virtual K8s cluster (Kamaji control plane + CAPI worker nodes)
  - Each tenant lives in namespace `tenant-{name}` on host cluster
  - Addons deployed via Flux HelmRelease CRs per addon per tenant
  - Addon catalog read from ConfigMap `tenant-addon-catalog`
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Addon catalog (read from ConfigMap)
# ---------------------------------------------------------------------------

class AddonParameter(BaseModel):
    """Parameter definition for an addon component."""

    id: str
    name: str = ""
    type: str = "string"  # "string" or "select"
    default: str = ""
    options: list[str] = Field(default_factory=list)  # for type="select"
    auto_discover: bool = False  # filled from discovery endpoint
    valuesPath: str = ""  # dot-separated path in Helm values (e.g. "linstor-csi.controllerEndpoint")


class AddonComponent(BaseModel):
    """Component definition from addon catalog ConfigMap."""

    id: str
    name: str
    category: str = ""
    description: str = ""
    required: bool = False
    default: bool = False  # pre-selected in wizard
    chartPath: str = ""  # path relative to basePath (e.g. "networking/calico")
    namespace: str = ""  # target namespace inside tenant cluster
    discovery_type: str = ""  # "storage", "monitoring", etc. — links to discovery
    defaultValues: dict[str, Any] = Field(default_factory=dict)  # base Helm values
    parameters: list[AddonParameter] = Field(default_factory=list)


class AddonCatalog(BaseModel):
    """Full addon catalog parsed from ConfigMap."""

    git_repository_ref: dict = Field(default_factory=dict)  # {name, namespace}
    base_path: str = "tenant-charts"
    components: list[AddonComponent] = Field(default_factory=list)

    def get_component(self, addon_id: str) -> AddonComponent | None:
        for c in self.components:
            if c.id == addon_id:
                return c
        return None


# ---------------------------------------------------------------------------
# Tenant create / update
# ---------------------------------------------------------------------------

class TenantAddon(BaseModel):
    """Addon selection for tenant creation or enable/disable."""

    addon_id: str
    parameters: dict[str, str] = Field(default_factory=dict)


class TenantCreateRequest(BaseModel):
    """Request to create a new tenant cluster."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    display_name: str = Field(..., min_length=1, max_length=128)
    kubernetes_version: str = "v1.30.0"
    control_plane_replicas: int = Field(default=2, ge=1, le=3)

    # Worker type: "vm" creates KubeVirt VMs, "bare_metal" skips VM resources
    worker_type: Literal["vm", "bare_metal"] = "vm"
    worker_count: int = Field(default=2, ge=1, le=20)
    worker_vcpu: int = Field(default=2, ge=1, le=32)
    worker_memory: str = "2Gi"
    worker_disk: str = "20Gi"

    # Golden image import for worker VMs (used when worker_type="vm")
    worker_image_source_type: Literal["http", "registry"] = "http"
    worker_image_url: str = ""
    worker_image_size: str = "10Gi"
    worker_image_os_type: str = "linux"
    worker_image_display_name: str = ""

    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    admin_group: str = ""  # DEX group → cluster-admin in tenant
    viewer_group: str = ""  # DEX group → view role in tenant
    network_isolation: bool = Field(
        default=False,
        description="Create isolated VPC for this tenant (Kube-OVN VPC + Subnet + peering to default)"
    )
    addons: list[TenantAddon] = Field(default_factory=list)


class TenantScaleRequest(BaseModel):
    """Request to scale tenant workers."""

    worker_count: int = Field(..., ge=1, le=20)


# ---------------------------------------------------------------------------
# Tenant response
# ---------------------------------------------------------------------------

class TenantAddonStatus(BaseModel):
    """Status of a single addon deployed in a tenant."""

    addon_id: str
    name: str = ""
    ready: bool = False
    last_reconcile: str | None = None
    message: str | None = None


class TenantCondition(BaseModel):
    """K8s-style condition."""

    type: str
    status: str  # "True", "False", "Unknown"
    message: str = ""
    reason: str = ""
    last_transition_time: str | None = None


class TenantResponse(BaseModel):
    """Tenant detail response."""

    name: str
    display_name: str
    namespace: str  # tenant-{name}
    kubernetes_version: str
    status: str  # Provisioning, Ready, NotReady, Deleting
    phase: str | None = None  # CAPI Cluster phase
    endpoint: str | None = None  # tenant API URL
    control_plane_replicas: int = 0
    control_plane_ready: bool = False
    worker_type: str = "vm"
    worker_count: int = 0
    workers_ready: int = 0
    worker_vcpu: int = 0
    worker_memory: str = ""
    pod_cidr: str = ""
    service_cidr: str = ""
    created: str | None = None
    conditions: list[TenantCondition] = Field(default_factory=list)
    addons: list[TenantAddonStatus] = Field(default_factory=list)


class TenantListResponse(BaseModel):
    """List of tenants."""

    items: list[TenantResponse]
    total: int
    page: int = 1
    per_page: int = 50
    pages: int = 1


class TenantKubeconfigResponse(BaseModel):
    """Kubeconfig for a tenant."""

    kubeconfig: str  # raw kubeconfig YAML


# ---------------------------------------------------------------------------
# Host cluster discovery
# ---------------------------------------------------------------------------

class StoragePoolInfo(BaseModel):
    """Linstor storage pool discovered from host cluster."""

    name: str
    driver: str = ""  # LVM_THIN, ZFS, etc.
    free_gb: float = 0
    total_gb: float = 0
    node_count: int = 0


class StorageDiscovery(BaseModel):
    """Storage backends discovered on host cluster."""

    type: str  # "linstor"
    api_url: str
    pools: list[StoragePoolInfo] = Field(default_factory=list)


class MonitoringDiscovery(BaseModel):
    """Monitoring backends discovered on host cluster."""

    type: str  # "victoria-metrics"
    write_url: str
    query_url: str = ""


class LoggingDiscovery(BaseModel):
    """Logging backends discovered on host cluster."""

    type: str  # "loki"
    push_url: str


class RegistryDiscovery(BaseModel):
    """Image registries discovered on host cluster."""

    type: str  # "harbor", "registry"
    url: str


class DiscoveryResponse(BaseModel):
    """Auto-discovered infrastructure from host cluster."""

    storage: list[StorageDiscovery] = Field(default_factory=list)
    monitoring: list[MonitoringDiscovery] = Field(default_factory=list)
    logging: list[LoggingDiscovery] = Field(default_factory=list)
    registry: list[RegistryDiscovery] = Field(default_factory=list)
