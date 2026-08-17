"""Pydantic models for BGP speaker management."""

import ipaddress

from pydantic import BaseModel, Field, field_validator


class SpeakerDeployRequest(BaseModel):
    neighbor_address: str = Field(..., description="BGP neighbor IP (e.g. 192.168.196.200)")
    neighbor_as: int = Field(65000, ge=1, le=4294967295, description="Neighbor ASN")
    cluster_as: int = Field(65001, ge=1, le=4294967295, description="Cluster ASN")

    @field_validator("neighbor_address")
    @classmethod
    def validate_ip(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v)
        except ValueError as e:
            raise ValueError(f"Invalid neighbor IP address: {e}")
        return v
    announce_cluster_ip: bool = True
    node_names: list[str] = []  # nodes to label with ovn.kubernetes.io/bgp=true


class SpeakerStatusResponse(BaseModel):
    deployed: bool
    config: dict = {}  # current args
    pods: list[dict] = []  # pod name, node, status
    node_labels: list[str] = []  # nodes with bgp=true


class AnnouncementRequest(BaseModel):
    resource_type: str  # "subnet", "service", "eip", "pod"
    resource_name: str
    resource_namespace: str = ""  # for namespaced resources
    policy: str = "cluster"  # "cluster" or "local"


class AnnouncementResponse(BaseModel):
    resource_type: str
    resource_name: str
    resource_namespace: str = ""
    bgp_enabled: bool
    policy: str = ""


class BGPSessionResponse(BaseModel):
    peer_address: str
    peer_asn: int
    state: str  # Established, Active, Connect, etc.
    # The speaker exposes no BGP metrics — its /metrics endpoint carries only
    # Go runtime and klog counters — and its GoBGP API port is disabled, so
    # neither uptime nor a received-prefix count can be measured from here.
    # `announced` is what this cluster is configured to advertise; it is a
    # real number, unlike the 0 that used to sit under "Prefixes" while the
    # neighbour held five routes.
    announced: int = 0
    node: str = ""  # which speaker pod reports this


class GatewayConfigExample(BaseModel):
    name: str  # "frr" or "bird"
    title: str
    description: str
    config: str  # config file content


# ============================================================================
# BgpConf — FRR config for VpcEgressGateway
# ============================================================================
#
# Distinct from the SpeakerDeployRequest above: `kube-ovn-speaker` is a
# DaemonSet that announces pod/service/EIP routes from the nodes. `BgpConf` is
# a cluster-scoped CR that configures the FRR running *inside* a
# VpcEgressGateway, which is what announces a VPC's own subnets. A gateway
# with no `spec.bgpConf` never peers at all.
#
# One BgpConf serves every gateway: ASNs, neighbours and timers are properties
# of the upstream router, not of a VPC. Verified with four gateways sharing
# one config — all four sessions Established, all routes present.


class BgpConfRequest(BaseModel):
    """Create/update the shared BGP configuration for egress gateways."""

    name: str = Field(
        "lab-gateway-common",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="BgpConf resource name. One shared config serves every gateway.",
    )
    local_asn: int = Field(..., ge=1, le=4294967295, description="Our ASN")
    peer_asn: int = Field(..., ge=1, le=4294967295, description="Upstream router ASN")
    neighbours: list[str] = Field(
        ...,
        min_length=1,
        description="Upstream BGP neighbour addresses",
    )
    graceful_restart: bool = Field(True, description="Enable BGP graceful restart")
    hold_time: str = Field("30s", description="BGP hold timer")
    keepalive_time: str = Field("10s", description="BGP keepalive timer")

    # NOTE: no router_id. Left unset, FRR derives one per gateway from its
    # internal address, which is unique as long as VPC CIDRs do not overlap —
    # and they cannot, because allocation is centralised and explicit CIDRs
    # are checked for overlap on create. Pinning one here would give every
    # gateway the same id, which the peer rejects within a single AS.

    @field_validator("neighbours")
    @classmethod
    def validate_neighbours(cls, v: list[str]) -> list[str]:
        for addr in v:
            try:
                ipaddress.ip_address(addr)
            except ValueError as e:
                raise ValueError(f"Invalid neighbour address {addr!r}: {e}")
        return v


class BgpConfResponse(BaseModel):
    name: str
    local_asn: int = 0
    peer_asn: int = 0
    neighbours: list[str] = []
    graceful_restart: bool = True
    hold_time: str = ""
    keepalive_time: str = ""
    router_id: str = ""  # only set if somebody pinned one out-of-band


class BgpConfListResponse(BaseModel):
    items: list[BgpConfResponse]
    total: int


class RoutedAnnouncement(BaseModel):
    """One tenant prefix and the router leg the border sends it to."""

    vpc: str
    cidr: str
    next_hop: str


class RoutedSession(BaseModel):
    node: str
    peer: str
    status: str = ""
    bfd: str = ""


class RoutedEgressResponse(BaseModel):
    """State of the routed external plane, in the tiers that can be trusted.

    Deliberately does not claim to show what is *actually advertised*. Only
    `show bgp ... advertised-routes` knows that, and it needs an exec into the
    FRR pod; the configuration alone cannot answer it — a config missing
    `no bgp ebgp-requires-policy` reads as perfectly healthy while the session
    is Established and nothing at all is announced. Saying "announced" about
    intent would rebuild the exact illusion this plane was built to remove.
    """

    enabled: bool = False
    peer: str = ""
    local_asn: int = 0
    nodes: list[str] = []
    intended: list[RoutedAnnouncement] = []
    sessions: list[RoutedSession] = []
    # node -> what FRR said when it refused the generated configuration
    config_errors: dict[str, str] = {}
