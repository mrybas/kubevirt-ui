"""Tenant Pydantic models.

Architecture:
  - Tenant = virtual K8s cluster (Kamaji control plane + CAPI worker nodes)
  - Each tenant lives in namespace `tenant-{name}` on host cluster
  - Addons deployed via Flux HelmRelease CRs per addon per tenant
  - Addon catalog read from ConfigMap `tenant-addon-catalog`
"""

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


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

    # Phase 2 folder/env binding (T1).
    # Required so tenants participate in folder-level authz (resolve_env →
    # folder/env → is_env_viewer per tenant). Validated at create time.
    folder: str = Field(
        ...,
        min_length=1,
        description="Folder name this tenant belongs to (must exist in folders ConfigMap)",
    )
    environment: str = Field(
        ...,
        min_length=1,
        description="Environment name within the folder (must exist as a folder env)",
    )

    # CAPK image tags actually published on quay.io/capk:
    #   ubuntu-2204-container-disk: v1.27.14, v1.28.10, v1.29.5, v1.30.1
    #   ubuntu-2404-container-disk: v1.31.5, v1.32.1, v1.33.5, v1.34.1
    # v1.30.1 is the most recent 2204 line and works with the broadest set of
    # addons in the catalog.
    kubernetes_version: str = "v1.30.1"
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

    # Names of pre-existing imagePullSecret Secrets in the tenant namespace.
    # Admin creates the Secrets out-of-band; we only stamp the references onto
    # the worker VM pod template so kubelet can pull the worker containerDisk.
    worker_image_pull_secrets: list[str] = Field(default_factory=list)

    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    # When True, tenant apiserver is configured with --oidc-* flags so
    # users can authenticate via the host Dex / kubelogin. When False,
    # tenant apiserver has no OIDC integration (admin kubeconfig with
    # certs is the only access path, group-based RBAC bindings are
    # ignored). Defaults to True for dev convenience.
    enable_oidc: bool = True
    admin_group: str = ""  # DEX group → cluster-admin in tenant
    viewer_group: str = ""  # DEX group → view role in tenant

    # T7 — Explicit VPC selection. The admin creates VPCs scoped to
    # (folder, env) via `kubevirt-ui.io/folder` + `kubevirt-ui.io/environment`
    # labels; the wizard renders an `GET /vpcs?folder=&environment=` dropdown
    # and posts the chosen name here. The backend validates that the named
    # VPC carries matching labels before patching its `spec.namespaces`.
    #
    # Omitted (None) → the tenant runs on the cluster default overlay (no VPC
    # attachment, no isolation).
    vpc_name: str | None = Field(
        default=None,
        description=(
            "Optional VPC to attach the tenant namespace to. Must be a managed "
            "VPC scoped to the same folder/env as the tenant. When omitted, "
            "the tenant runs on the cluster default overlay."
        ),
    )

    # T6 — Worker NIC binding: default 'bridge' so guests see their real
    # OVN/Kube-OVN IP (masquerade hides it behind QEMU SLIRP's 10.0.2.x).
    # Override with 'masquerade' for CNIs where bridge live-migration is iffy.
    worker_network_binding: Literal["bridge", "masquerade"] = "bridge"

    # --- Tenant persistent storage (kubevirt-csi pattern) -----------------
    # When enable_storage=True the tenant create flow provisions:
    #   - host-side SA `kubevirt-csi` + scoped Role/RoleBinding in tenant ns
    #   - host-side Secret `infra-cluster-credentials` with a kubeconfig that
    #     points at the host apiserver and authenticates as the SA
    #   - KubevirtCluster.spec.infraClusterSecretRef + infraClusterStorageClassName
    #     (so CAPK / kubevirt-csi can speak to the host cluster)
    #   - ResourceQuota in the tenant ns capping aggregate PVC count + GiB
    #   - kubevirt-csi-driver HelmRelease auto-added to the tenant CP addons
    #
    # quota fields are only meaningful when enable_storage=True; when False
    # they are accepted but ignored (avoids forcing the wizard to gate them).
    enable_storage: bool = Field(
        default=False,
        description="Provision kubevirt-csi for tenant persistent storage via host CDI",
    )
    storage_class: str | None = Field(
        default=None,
        description="Host StorageClass to back tenant DataVolumes (None = cluster default)",
    )
    storage_quota_gi: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="ResourceQuota for tenant ns total storage requests (GiB)",
    )
    storage_pvc_count: int = Field(
        default=20,
        ge=1,
        le=200,
        description="ResourceQuota for tenant ns total PVC count",
    )

    addons: list[TenantAddon] = Field(default_factory=list)

    @field_validator("kubernetes_version")
    @classmethod
    def _warn_on_nonstandard_version(cls, v: str) -> str:
        # Not blocking — quay.io/capk tags evolve and validating against the
        # live list is too brittle. Just log a warning so operators notice
        # typos like "1.30" or "v1.30" without trailing patch.
        if not re.match(r"^v\d+\.\d+\.\d+$", v):
            logger.warning(
                "kubernetes_version %r does not match the expected "
                "'vMAJOR.MINOR.PATCH' shape; CAPK image pull will likely fail",
                v,
            )
        return v

    @field_validator("storage_class")
    @classmethod
    def _normalize_blank_storage_class(cls, v: str | None) -> str | None:
        # Same trick as infra_subnet — wizard sends "" when the user picks
        # "cluster default"; collapse to None so the create flow has one shape
        # to branch on (None ⇒ omit infraClusterStorageClassName on KvCluster
        # which makes CAPK pick the cluster default SC).
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

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
