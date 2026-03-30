"""Shared Kubernetes API constants and helpers."""

# KubeVirt CRD coordinates
KUBEVIRT_API_GROUP = "kubevirt.io"
KUBEVIRT_API_VERSION = "v1"

# CDI (Containerized Data Importer) CRD coordinates
CDI_API_GROUP = "cdi.kubevirt.io"
CDI_API_VERSION = "v1beta1"

# Snapshot CRD coordinates
SNAPSHOT_API_GROUP = "snapshot.kubevirt.io"
SNAPSHOT_API_VERSION = "v1beta1"

# Volume snapshot CRD coordinates
VOLUME_SNAPSHOT_GROUP = "snapshot.storage.k8s.io"
VOLUME_SNAPSHOT_VERSION = "v1"

# Kube-OVN CRD coordinates
KUBEOVN_API_GROUP = "kubeovn.io"
KUBEOVN_API_VERSION = "v1"

# CAPI (Cluster API) CRD coordinates
CAPI_API_GROUP = "cluster.x-k8s.io"
CAPI_API_VERSION = "v1beta1"

# KubeVirt UI labels / annotations
LABEL_PREFIX = "kubevirt-ui.io"

# System namespace
SYSTEM_NAMESPACE = "kubevirt-ui-system"


def parse_k8s_capacity(cap: str) -> int:
    """Parse a Kubernetes capacity/quantity string like '100Gi' to bytes."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5}
    for suffix, mult in units.items():
        if cap.endswith(suffix):
            return int(float(cap[: -len(suffix)]) * mult)
    try:
        return int(cap)
    except (ValueError, TypeError):
        return 0
