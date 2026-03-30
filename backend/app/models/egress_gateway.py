"""Egress Gateway Pydantic models.

Covers the VPC Egress Gateway hub-and-spoke architecture:
  - Gateway CRUD (VPC + VpcEgressGateway + transit subnet)
  - Tenant attach/detach (VpcPeering + static routes + policy updates)
"""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field, field_validator
from typing import Optional


class EgressGatewayCreateRequest(BaseModel):
    """Request to create an egress gateway.

    External subnet can be provided in two ways:
    1. macvlan_subnet — name of an existing kube-ovn Subnet (VLAN-backed or provider-backed)
    2. external_* fields — create a new macvlan NAD + kube-ovn Subnet automatically
       (for using the node management network or any other underlay network)
    """
    name: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="Gateway name (used as VPC and resource prefix)",
    )
    gw_vpc_cidr: str = Field(
        "10.199.0.0/24",
        description="CIDR for internal gateway VPC subnet",
    )
    transit_cidr: str = Field(
        "10.255.0.0/24",
        description="Transit subnet CIDR for VPC peering",
    )
    # Option 1: use existing subnet
    macvlan_subnet: Optional[str] = Field(
        None,
        description="Name of existing kube-ovn Subnet (e.g. 'alv111'). Mutually exclusive with external_* fields.",
    )
    # Option 2: create new macvlan subnet
    external_interface: Optional[str] = Field(
        None,
        description="Physical NIC for macvlan (e.g. 'eth0', 'eno1'). Creates NAD + Subnet automatically.",
    )
    external_cidr: Optional[str] = Field(
        None,
        description="CIDR of the external network (e.g. '192.168.196.0/24')",
    )
    external_gateway: Optional[str] = Field(
        None,
        description="Gateway IP of the external network (e.g. '192.168.196.1')",
    )
    replicas: int = Field(2, ge=1, le=10, description="Number of egress gateway pod replicas")
    bfd_enabled: bool = Field(False, description="Enable BFD for fast failover (requires OVN BFD support)")
    node_selector: dict[str, str] = Field(
        default_factory=dict,
        description="Node selector for egress gateway pods",
    )
    exclude_ips: list[str] = Field(
        default_factory=list,
        description="IPs to exclude from external subnet allocation",
    )

    @field_validator('gw_vpc_cidr', 'transit_cidr')
    @classmethod
    def validate_cidr(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid CIDR: {e}")
        return v

    @field_validator('external_cidr')
    @classmethod
    def validate_external_cidr(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ipaddress.ip_network(v, strict=False)
            except ValueError as e:
                raise ValueError(f"Invalid external CIDR: {e}")
        return v

    @field_validator('exclude_ips')
    @classmethod
    def validate_exclude_ips(cls, v: list[str]) -> list[str]:
        for entry in v:
            if '..' in entry:
                parts = entry.split('..', 1)
                if len(parts) != 2:
                    raise ValueError(f"Invalid IP range '{entry}': expected format 'start..end'")
                try:
                    start = ipaddress.ip_address(parts[0])
                    end = ipaddress.ip_address(parts[1])
                except ValueError:
                    raise ValueError(f"Invalid IP range '{entry}': both sides must be valid IPs")
                if int(start) > int(end):
                    raise ValueError(f"Invalid IP range '{entry}': start must be <= end")
            else:
                try:
                    ipaddress.ip_address(entry)
                except ValueError:
                    raise ValueError(f"Invalid IP '{entry}': not a valid IP address")
        return v


class AttachTenantRequest(BaseModel):
    """Request to attach a tenant VPC to an egress gateway."""
    vpc_name: str = Field(..., description="Tenant VPC name")
    subnet_name: str = Field(..., description="Tenant VPC subnet name")
    cidr: str = Field(..., description="Tenant VPC subnet CIDR")


class DetachTenantRequest(BaseModel):
    """Request to detach a tenant VPC from an egress gateway."""
    vpc_name: str = Field(..., description="Tenant VPC name")
    subnet_name: str = Field(..., description="Tenant VPC subnet name")


class AttachedVpcInfo(BaseModel):
    """Info about a VPC attached to an egress gateway."""
    vpc_name: str
    subnet_name: str
    cidr: str
    transit_ip: str = ""
    peering_name: str = ""


class GatewayPodInfo(BaseModel):
    """Info about an egress gateway pod and its assigned IPs."""
    pod: str = ""
    node: str = ""
    internal_ip: str = ""
    external_ip: str = ""


class EgressGatewayResponse(BaseModel):
    """Response model for an egress gateway."""
    name: str
    gw_vpc_name: str = ""
    gw_vpc_cidr: str = ""
    transit_cidr: str = ""
    macvlan_subnet: str = ""
    replicas: int = 2
    bfd_enabled: bool = True
    node_selector: dict[str, str] = {}
    exclude_ips: list[str] = []
    attached_vpcs: list[AttachedVpcInfo] = []
    assigned_ips: list[GatewayPodInfo] = []
    ready: bool = False
    status: Optional[dict] = None


class EgressGatewayListResponse(BaseModel):
    """List of egress gateways."""
    items: list[EgressGatewayResponse]
    total: int
