"""Storage Pydantic models - DataVolume, PVC."""

from typing import Any

from pydantic import BaseModel, Field


class DataVolumeCreateRequest(BaseModel):
    """Request model for creating a DataVolume."""

    name: str = Field(
        ..., min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    )
    size: str = Field(default="10Gi", pattern=r"^\d+[KMGT]i?$")
    storage_class: str | None = None
    access_modes: list[str] = Field(default=["ReadWriteOnce"])

    # Source type
    source_type: str = Field(
        default="blank", pattern=r"^(blank|http|registry|pvc|s3|gcs)$"
    )

    # HTTP source
    source_url: str | None = None

    # Registry source
    registry_url: str | None = None

    # PVC clone source
    source_pvc_name: str | None = None
    source_pvc_namespace: str | None = None

    # S3 source
    s3_url: str | None = None
    s3_secret_ref: str | None = None

    # Labels
    labels: dict[str, str] = Field(default_factory=dict)

    def to_k8s_manifest(self, namespace: str) -> dict[str, Any]:
        """Convert to Kubernetes DataVolume manifest."""
        # Build source spec
        source: dict[str, Any] = {}

        if self.source_type == "blank":
            source["blank"] = {}
        elif self.source_type == "http" and self.source_url:
            source["http"] = {"url": self.source_url}
        elif self.source_type == "registry" and self.registry_url:
            source["registry"] = {"url": self.registry_url}
        elif self.source_type == "pvc" and self.source_pvc_name:
            source["pvc"] = {
                "name": self.source_pvc_name,
                "namespace": self.source_pvc_namespace or namespace,
            }
        elif self.source_type == "s3" and self.s3_url:
            s3_source: dict[str, Any] = {"url": self.s3_url}
            if self.s3_secret_ref:
                s3_source["secretRef"] = self.s3_secret_ref
            source["s3"] = s3_source
        else:
            source["blank"] = {}

        # Build storage spec
        storage: dict[str, Any] = {
            "accessModes": self.access_modes,
            "resources": {"requests": {"storage": self.size}},
            "volumeMode": "Block",
        }

        if self.storage_class:
            storage["storageClassName"] = self.storage_class

        manifest: dict[str, Any] = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": self.name,
                "namespace": namespace,
                "labels": {
                    "kubevirt-ui.io/created-by": "kubevirt-ui",
                    **self.labels,
                },
            },
            "spec": {
                "source": source,
                "storage": storage,
            },
        }

        return manifest


class DataVolumeResponse(BaseModel):
    """Response model for a DataVolume."""

    name: str
    namespace: str
    display_name: str | None = None
    phase: str | None = None
    progress: str | None = None
    size: str | None = None
    storage_class: str | None = None
    source_type: str | None = None
    created: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class DataVolumeListResponse(BaseModel):
    """Response model for listing DataVolumes."""

    items: list[DataVolumeResponse]
    total: int


class PVCResponse(BaseModel):
    """Response model for a PVC."""

    name: str
    namespace: str
    phase: str | None = None
    size: str | None = None
    storage_class: str | None = None
    access_modes: list[str] = Field(default_factory=list)
    volume_name: str | None = None
    created: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class PVCListResponse(BaseModel):
    """Response model for listing PVCs."""

    items: list[PVCResponse]
    total: int


class StorageClassResponse(BaseModel):
    """Response model for a StorageClass."""

    name: str
    provisioner: str
    reclaim_policy: str | None = None
    volume_binding_mode: str | None = None
    allow_volume_expansion: bool = False
    is_default: bool = False


class StorageClassDetailResponse(BaseModel):
    """Detailed storage class with capacity stats."""

    name: str
    provisioner: str
    reclaim_policy: str | None = None
    volume_binding_mode: str | None = None
    allow_volume_expansion: bool = False
    is_default: bool = False
    parameters: dict[str, str] = Field(default_factory=dict)
    pv_count: int = 0
    pvc_count: int = 0
    total_capacity_bytes: int = 0
    used_capacity_bytes: int = 0
    created: str | None = None


class StorageClassListResponse(BaseModel):
    """Response model for listing StorageClasses."""

    items: list[dict[str, Any]]
    total: int


def dv_from_k8s(dv: dict[str, Any]) -> DataVolumeResponse:
    """Convert Kubernetes DataVolume to response model."""
    metadata = dv.get("metadata", {})
    spec = dv.get("spec", {})
    dv_status = dv.get("status", {})

    # Determine source type
    source = spec.get("source", {})
    source_type = None
    if "http" in source:
        source_type = "http"
    elif "registry" in source:
        source_type = "registry"
    elif "pvc" in source:
        source_type = "pvc"
    elif "s3" in source:
        source_type = "s3"
    elif "blank" in source:
        source_type = "blank"

    # Get size from storage spec
    storage = spec.get("storage", {})
    size = storage.get("resources", {}).get("requests", {}).get("storage")

    # Read display name from annotation, fallback to metadata.name
    annotations = metadata.get("annotations") or {}
    display_name = annotations.get("kubevirt-ui.io/display-name") or metadata.get("name", "")

    return DataVolumeResponse(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace", ""),
        display_name=display_name,
        phase=dv_status.get("phase"),
        progress=dv_status.get("progress"),
        size=size,
        storage_class=storage.get("storageClassName"),
        source_type=source_type,
        created=metadata.get("creationTimestamp"),
        labels=metadata.get("labels", {}),
    )


def pvc_from_k8s(pvc: Any) -> PVCResponse:
    """Convert Kubernetes PVC to response model."""
    return PVCResponse(
        name=pvc.metadata.name,
        namespace=pvc.metadata.namespace,
        phase=pvc.status.phase if pvc.status else None,
        size=pvc.spec.resources.requests.get("storage") if pvc.spec.resources else None,
        storage_class=pvc.spec.storage_class_name,
        access_modes=pvc.spec.access_modes or [],
        volume_name=pvc.spec.volume_name,
        created=pvc.metadata.creation_timestamp.isoformat()
        if pvc.metadata.creation_timestamp
        else None,
        labels=pvc.metadata.labels or {},
    )
