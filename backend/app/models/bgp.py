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
    uptime: str = ""
    prefixes_received: int = 0
    node: str = ""  # which speaker pod reports this


class GatewayConfigExample(BaseModel):
    name: str  # "frr" or "bird"
    title: str
    description: str
    config: str  # config file content
