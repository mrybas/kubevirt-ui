"""Shared Kubernetes API constants and helpers."""

import os

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

# Kamaji control plane namespace.
# All KamajiControlPlane CRs (and their auto-generated TCP Services) live here
# so they share the default VPC with Postgres/Ingress/shared platform services.
# Worker resources stay in per-tenant namespaces (tenant VPC). Workers reach the
# TCP apiserver via the cluster Ingress; the TCP→worker path is the konnectivity
# tunnel (agent-initiated outbound HTTPS from each worker).
# Override via env when Kamaji is installed in a non-default namespace.
KAMAJI_NAMESPACE = os.getenv("TENANTS_KAMAJI_NAMESPACE", "kamaji-system")

# Kube-OVN system networking — default cluster VPC + system subnets that
# non-admin users must never see. Filtered out of VPC/Subnet list responses.
KUBEOVN_SYSTEM_VPC = "ovn-cluster"
KUBEOVN_SYSTEM_SUBNETS = frozenset({"join", "ovn-default"})


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
