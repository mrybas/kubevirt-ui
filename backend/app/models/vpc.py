"""VPC and SecurityGroup Pydantic models for Kube-OVN integration.

Covers:
  - Vpc (kubeovn.io/v1) — VPC with logical router, subnets, peerings, static routes
  - SecurityGroup (kubeovn.io/v1) — per-VM firewall rules (ingress/egress)
"""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional


# ============================================================================
# VPC Models
# ============================================================================

class VpcStaticRoute(BaseModel):
    """A static route entry in a VPC."""
    cidr: str = Field(..., description="Destination CIDR (e.g., '0.0.0.0/0')")
    next_hop_ip: str = Field(..., alias="nextHopIP", description="Next hop IP address")
    policy: str = Field("policyDst", description="Routing policy: policyDst or policySrc")

    model_config = {"populate_by_name": True}

    @field_validator("cidr")
    @classmethod
    def validate_cidr(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid CIDR: {e}")
        return v

    @field_validator("next_hop_ip")
    @classmethod
    def validate_next_hop(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v)
        except ValueError as e:
            raise ValueError(f"Invalid next hop IP: {e}")
        return v


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


class VpcCreateRequest(BaseModel):
    """Request to create a VPC with a default subnet."""
    name: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    subnet_cidr: Optional[str] = Field(
        None,
        description="CIDR for default subnet (auto-assigned from 10.{200+N}.0.0/24 if empty)",
    )
    tenant: Optional[str] = Field(None, description="Tenant name to bind this VPC to")
    enable_nat_gateway: bool = Field(
        False,
        description="Enable NAT gateway for internet access from VPC",
    )
    static_routes: list[VpcStaticRoute] = Field(
        default_factory=list,
        description="Initial static routes",
    )

    @field_validator("subnet_cidr")
    @classmethod
    def validate_cidr(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ipaddress.ip_network(v, strict=False)
            except ValueError as e:
                raise ValueError(f"Invalid subnet CIDR: {e}")
        return v


class VpcResponse(BaseModel):
    """Response model for a VPC."""
    name: str
    tenant: Optional[str] = None
    enable_nat_gateway: bool = False
    default_subnet: Optional[str] = None
    subnets: list[VpcSubnetInfo] = []
    peerings: list[VpcPeeringInfo] = []
    static_routes: list[VpcStaticRoute] = []
    namespaces: list[str] = []
    ready: bool = False
    conditions: list[dict] = []


class VpcListResponse(BaseModel):
    """Paginated list of VPCs."""
    items: list[VpcResponse]
    total: int


class VpcPeeringCreateRequest(BaseModel):
    """Request to create a VPC peering connection."""
    remote_vpc: str = Field(..., description="Remote VPC name to peer with")


class VpcPeeringDeleteRequest(BaseModel):
    """Not used as a body — remote_vpc comes from path."""
    pass


class VpcStaticRoutesUpdateRequest(BaseModel):
    """Request to replace all static routes on a VPC."""
    static_routes: list[VpcStaticRoute]


# ============================================================================
# SecurityGroup Models
# ============================================================================

class SecurityGroupRule(BaseModel):
    """A single firewall rule in a SecurityGroup.

    Accepts both camelCase (Kube-OVN native) and snake_case (frontend) field names.
    Frontend sends: action, port_range, remote_address, priority, protocol
    Kube-OVN uses: policy, portRangeMin/Max, remoteAddress, ipVersion, remoteType
    """
    ip_version: str = Field("ipv4", alias="ipVersion", description="ipv4 or ipv6")
    protocol: str = Field("all", description="all, tcp, udp, icmp")
    policy: str = Field("allow", description="allow or deny")
    action: Optional[str] = Field(None, description="Frontend alias for policy (allow/drop)")
    priority: int = Field(0, ge=0, le=3200, description="Rule priority (lower = higher)")
    remote_address: str = Field(
        "0.0.0.0/0",
        alias="remoteAddress",
        description="Source/destination CIDR or IP",
    )
    remote_type: str = Field("address", alias="remoteType", description="address or securityGroup")
    port_range_min: Optional[int] = Field(None, alias="portRangeMin", ge=1, le=65535)
    port_range_max: Optional[int] = Field(None, alias="portRangeMax", ge=1, le=65535)
    port_range: Optional[str] = Field(None, description="Frontend port range string, e.g. '80' or '443-8443'")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def normalize_frontend_fields(self) -> "SecurityGroupRule":
        """Normalize frontend fields to backend fields."""
        # action -> policy
        if self.action and self.policy == "allow":
            self.policy = "allow" if self.action == "allow" else "deny"
        # port_range -> portRangeMin/Max
        if self.port_range and self.port_range_min is None:
            parts = self.port_range.strip().split("-", 1)
            try:
                self.port_range_min = int(parts[0])
                self.port_range_max = int(parts[1]) if len(parts) > 1 else int(parts[0])
            except (ValueError, IndexError):
                pass
        return self


class SecurityGroupCreateRequest(BaseModel):
    """Request to create a SecurityGroup."""
    name: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    allow_same_group_traffic: bool = Field(
        True,
        description="Allow traffic between pods in the same SecurityGroup",
    )
    ingress_rules: list[SecurityGroupRule] = Field(
        default_factory=list,
        description="Ingress (incoming) firewall rules",
    )
    egress_rules: list[SecurityGroupRule] = Field(
        default_factory=list,
        description="Egress (outgoing) firewall rules",
    )


class SecurityGroupUpdateRequest(BaseModel):
    """Request to update a SecurityGroup (full replace of rules)."""
    allow_same_group_traffic: Optional[bool] = None
    ingress_rules: Optional[list[SecurityGroupRule]] = None
    egress_rules: Optional[list[SecurityGroupRule]] = None


class SecurityGroupResponse(BaseModel):
    """Response model for a SecurityGroup."""
    name: str
    allow_same_group_traffic: bool = True
    ingress_rules: list[SecurityGroupRule] = []
    egress_rules: list[SecurityGroupRule] = []


class SecurityGroupListResponse(BaseModel):
    """List of SecurityGroups."""
    items: list[SecurityGroupResponse]
    total: int


class VMSecurityGroupsResponse(BaseModel):
    """SecurityGroups assigned to a VM."""
    vm_name: str
    namespace: str
    security_groups: list[str] = []


class VMSecurityGroupAssignRequest(BaseModel):
    """Request to assign a SecurityGroup to a VM."""
    security_group: str = Field(..., description="SecurityGroup name to assign")
