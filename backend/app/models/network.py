"""Network-related Pydantic models for Kube-OVN integration."""

import ipaddress

from pydantic import BaseModel, Field, field_validator
from typing import Optional


# ============================================================================
# Provider Network (Physical Network Connection)
# ============================================================================

class ProviderNetworkCreate(BaseModel):
    """Request model for creating a ProviderNetwork."""
    name: str = Field(..., min_length=1, max_length=63, description="Name of the provider network")
    default_interface: str = Field(..., description="Physical interface name (e.g., eno1, eth0)")
    # Optional: per-node interface mapping
    custom_interfaces: dict[str, str] = Field(
        default_factory=dict,
        description="Map of node name to interface name if different from default"
    )
    # CRITICAL: For single-interface setups - Kube-OVN auto-creates VLAN sub-interfaces
    # This keeps management traffic on base interface while VM traffic uses VLAN sub-interfaces
    auto_create_vlan_subinterfaces: bool = Field(
        default=True,
        description="Auto-create VLAN sub-interfaces (RECOMMENDED for single-NIC setups)"
    )
    # Legacy option - transfers IP to OVS bridge (DANGEROUS - can break cluster!)
    exchange_link_name: bool = Field(
        default=False,
        description="Transfer IP from physical interface to OVS bridge (DANGEROUS - use auto_create_vlan_subinterfaces instead)"
    )


class ProviderNetworkResponse(BaseModel):
    """Response model for ProviderNetwork."""
    name: str
    default_interface: str
    auto_create_vlan_subinterfaces: bool = False  # Whether VLAN sub-interfaces are auto-created
    exchange_link_name: bool = False  # Whether IP is transferred to bridge (legacy)
    ready: bool
    ready_nodes: list[str] = []
    not_ready_nodes: list[str] = []
    vlans: list[str] = []  # List of VLAN names using this provider
    conditions: list[dict] = []


# ============================================================================
# VLAN
# ============================================================================

class VlanCreate(BaseModel):
    """Request model for creating a VLAN."""
    name: str = Field(..., min_length=1, max_length=63)
    id: int = Field(..., ge=0, le=4094, description="VLAN ID (0 = untagged)")
    provider: str = Field(..., description="ProviderNetwork name")


class VlanResponse(BaseModel):
    """Response model for VLAN."""
    name: str
    id: int
    provider: str


# ============================================================================
# Subnet
# ============================================================================

class SubnetCreate(BaseModel):
    """Request model for creating a Subnet."""
    name: str = Field(..., min_length=1, max_length=63)
    cidr_block: str = Field(..., description="CIDR notation (e.g., 192.168.1.0/24)")
    gateway: str = Field(..., description="Gateway IP address")

    @field_validator("cidr_block")
    @classmethod
    def validate_cidr(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid CIDR: {e}")
        return v

    @field_validator("gateway")
    @classmethod
    def validate_gateway(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v)
        except ValueError as e:
            raise ValueError(f"Invalid gateway IP: {e}")
        return v
    exclude_ips: list[str] = Field(
        default_factory=list,
        description="IPs to exclude from allocation (e.g., ['192.168.1.1..192.168.1.10', '192.168.1.254'])"
    )
    # VLAN-based external network
    vlan: Optional[str] = Field(None, description="VLAN name")
    # Namespace where the NAD will be created (one subnet = one namespace)
    # Not required for infrastructure subnets (no NAD needed)
    namespace: Optional[str] = Field(None, description="Namespace for this subnet (NAD will be created here)")
    # For overlay networks (VPC)
    vpc: Optional[str] = Field(None, description="VPC name for overlay networks")
    # Purpose: "vm" (default) — creates NAD for VM attachment
    #          "infrastructure" — no NAD, used for VPC NAT gateway external connectivity
    purpose: str = Field("vm", description="Subnet purpose: 'vm' or 'infrastructure'")
    # Optional settings
    enable_dhcp: bool = Field(True, description="Enable DHCP for this subnet")
    disable_gateway_check: bool = Field(
        False,
        description="Disable gateway ARP check (enable if gateway doesn't respond to ARP, e.g. VLAN sub-interface without IP)"
    )


class SubnetStatistics(BaseModel):
    """Statistics for a subnet."""
    total: int
    available: int
    used: int
    reserved: int


class SubnetResponse(BaseModel):
    """Response model for Subnet."""
    name: str
    cidr_block: str
    gateway: str
    exclude_ips: list[str] = []
    provider: Optional[str] = None
    vlan: Optional[str] = None
    vpc: Optional[str] = None
    namespace: Optional[str] = None  # Namespace where NAD is created
    protocol: str = "IPv4"
    enable_dhcp: bool = True
    disable_gateway_check: bool = False
    purpose: str = "vm"  # "vm" or "infrastructure"
    statistics: Optional[SubnetStatistics] = None
    ready: bool = False


# ============================================================================
# IP Lease
# ============================================================================

class IPLease(BaseModel):
    """IP address lease information."""
    ip_address: str
    mac_address: Optional[str] = None
    pod_name: Optional[str] = None
    namespace: Optional[str] = None
    node_name: Optional[str] = None
    subnet: str
    # Derived
    resource_type: str = "pod"  # "vm" or "pod"
    resource_name: Optional[str] = None  # Clean VM/Pod name


# ============================================================================
# Reserve IP
# ============================================================================

class ReserveIPRequest(BaseModel):
    """Request to reserve an IP or IP range."""
    ip_or_range: str = Field(
        ..., 
        description="Single IP (192.168.1.50) or range (192.168.1.50..192.168.1.60)"
    )
    note: Optional[str] = Field(None, description="Optional note for the reservation")


class ReservedIP(BaseModel):
    """Reserved IP information."""
    ip_or_range: str
    count: int  # Number of IPs in this reservation
    note: Optional[str] = None


# ============================================================================
# Node Network Info
# ============================================================================

class NodeNetworkInfo(BaseModel):
    """Network information from a node."""
    name: str
    internal_ip: Optional[str] = None
    interfaces: list[str] = []  # Available network interfaces
    annotations: dict[str, str] = {}


# ============================================================================
# Subnet Detail (with leases)
# ============================================================================

class SubnetDetail(BaseModel):
    """Detailed subnet information including leases."""
    subnet: SubnetResponse
    leases: list[IPLease] = []
    reserved: list[ReservedIP] = []


# ============================================================================
# Network Overview
# ============================================================================

class NetworkOverview(BaseModel):
    """Overview of all network resources."""
    provider_networks: int = 0
    vlans: int = 0
    subnets: int = 0
    vpcs: int = 0
    total_ips_used: int = 0
    total_ips_available: int = 0


# ============================================================================
# VPC (Virtual Private Cloud — Kube-OVN overlay network isolation)
# ============================================================================

class VpcCreate(BaseModel):
    """Request model for creating a VPC with a default subnet."""
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    # Default subnet (auto-created with the VPC)
    subnet_cidr: Optional[str] = Field(
        None,
        description="CIDR for default subnet (auto-assigned from 10.{100+N}.0.0/16 if empty)"
    )
    # Tenant binding
    tenant: Optional[str] = Field(None, description="Tenant name to bind this VPC to")
    # NAT gateway
    enable_nat_gateway: bool = Field(
        False,
        description="Enable NAT gateway for internet access from VPC"
    )
    # Static routes for inter-VPC or external connectivity
    static_routes: list[dict[str, str]] = Field(
        default_factory=list,
        description="Static routes: [{'cidr': '0.0.0.0/0', 'nextHopIP': '10.x.x.1'}]"
    )


class VpcSubnetInfo(BaseModel):
    """Summary of a subnet belonging to a VPC."""
    name: str
    cidr_block: str
    gateway: str
    available_ips: int = 0
    used_ips: int = 0


class VpcPeeringInfo(BaseModel):
    """Summary of a VPC peering connection."""
    name: str
    local_vpc: str
    remote_vpc: str


class VpcResponse(BaseModel):
    """Response model for VPC."""
    name: str
    tenant: Optional[str] = None
    enable_nat_gateway: bool = False
    default_subnet: Optional[str] = None
    subnets: list[VpcSubnetInfo] = []
    peerings: list[VpcPeeringInfo] = []
    static_routes: list[dict[str, str]] = []
    ready: bool = False
    conditions: list[dict] = []


class VpcPeeringCreate(BaseModel):
    """Request model for creating a VPC peering connection."""
    local_vpc: str = Field(..., description="Local VPC name")
    remote_vpc: str = Field(..., description="Remote VPC name to peer with")
