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

from pydantic import BaseModel, Field, field_validator, model_validator

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
    admin_group: str = ""  # DEX group → cluster-admin in tenant
    viewer_group: str = ""  # DEX group → view role in tenant

    # T3 — Network isolation modes:
    #   "shared":                     no VPC; tenant ns lands in ovn-default.
    #                                 Internet egress via cluster default.
    #                                 This is the default for our setup.
    #   "isolated_shared_egress":     VPC + subnet, but static route 0.0.0.0/0 →
    #                                 ovn-default gateway IP so the tenant uses
    #                                 the host's underlay for egress.
    #                                 NO infra_subnet required.
    #   "isolated_dedicated_egress":  VPC + subnet + EgressGateway pods bound
    #                                 to infra_subnet (provider VLAN). REQUIRES
    #                                 infra_subnet to be a real Kube-OVN subnet
    #                                 labeled kubevirt-ui.io/purpose=infrastructure.
    network_isolation_mode: Literal[
        "shared",
        "isolated_shared_egress",
        "isolated_dedicated_egress",
    ] = "shared"
    infra_subnet: str | None = Field(
        None,
        description=(
            "Infrastructure subnet for dedicated egress. "
            "Required iff network_isolation_mode == 'isolated_dedicated_egress'."
        ),
    )

    # T6 — Worker NIC binding: default 'bridge' so guests see their real
    # OVN/Kube-OVN IP (masquerade hides it behind QEMU SLIRP's 10.0.2.x).
    # Override with 'masquerade' for CNIs where bridge live-migration is iffy.
    worker_network_binding: Literal["bridge", "masquerade"] = "bridge"

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

    @field_validator("infra_subnet")
    @classmethod
    def _normalize_blank_infra_subnet(cls, v: str | None) -> str | None:
        # Treat "" the same as None so the wizard's empty-string default
        # collapses to None for the dedicated-egress validator below.
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _check_infra_subnet_required(self) -> "TenantCreateRequest":
        if self.network_isolation_mode == "isolated_dedicated_egress" and not self.infra_subnet:
            raise ValueError(
                "infra_subnet is required when network_isolation_mode is "
                "'isolated_dedicated_egress'"
            )
        # Shared / shared_egress modes don't use infra_subnet; null it out so
        # downstream code can rely on "set ⇒ dedicated egress".
        if self.network_isolation_mode != "isolated_dedicated_egress":
            self.infra_subnet = None
        return self

    @property
    def network_isolation(self) -> bool:
        """Back-compat alias: True iff a VPC needs to be provisioned for the tenant.

        Internal callers that read tenant networking should switch to checking
        `network_isolation_mode` directly. Kept so any legacy template / chart
        that imports the request still works.
        """
        return self.network_isolation_mode != "shared"


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
