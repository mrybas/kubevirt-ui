"""OVN Gateway Pydantic models.

OVN-native NAT gateway using OvnEip, OvnSnatRule, OvnDnatRule, OvnFip CRDs.
Alternative to VpcEgressGateway — no extra pods, NAT handled by OVN logical router.
"""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field, field_validator
from typing import Optional


class OvnGatewayCreateRequest(BaseModel):
    """Request to enable OVN NAT for a VPC.

    The gateway is identified by vpc_name — one OVN NAT config per VPC.
    infra_subnet is required because auto-detection may not find a subnet
    labeled kubevirt-ui.io/purpose=infrastructure on all clusters.
    """
    vpc_name: str = Field(..., description="VPC to enable OVN NAT for")
    subnet_name: str = Field(..., description="VPC subnet to SNAT")
    infra_subnet: str = Field(
        ...,
        description="Infrastructure subnet name (e.g. 'alv111') — VLAN-backed subnet for external connectivity",
    )
    eip_address: Optional[str] = Field(
        None,
        description="Specific EIP address to use (auto-allocated if None)",
    )
    shared_eip: Optional[str] = Field(
        None,
        description="Name of existing shared OvnEip to reuse for SNAT",
    )
    auto_snat: bool = Field(
        True,
        description="Automatically create SNAT rule for the VPC subnet",
    )

    @field_validator("eip_address")
    @classmethod
    def validate_eip(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ipaddress.ip_address(v)
            except ValueError as e:
                raise ValueError(f"Invalid EIP address: {e}")
        return v


class OvnSnatRuleCreateRequest(BaseModel):
    """Request to create an OVN SNAT rule."""
    ovn_eip: str = Field(..., description="OvnEip name to use for SNAT")
    vpc_subnet: Optional[str] = Field(
        None,
        description="VPC subnet name to SNAT (mutually exclusive with internal_cidr)",
    )
    internal_cidr: Optional[str] = Field(
        None,
        description="Internal CIDR to SNAT (mutually exclusive with vpc_subnet)",
    )

    @field_validator("internal_cidr")
    @classmethod
    def validate_cidr(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ipaddress.ip_network(v, strict=False)
            except ValueError as e:
                raise ValueError(f"Invalid CIDR: {e}")
        return v


class OvnDnatRuleCreateRequest(BaseModel):
    """Request to create an OVN DNAT rule."""
    ovn_eip: str = Field(..., description="OvnEip name for external IP")
    ip_name: str = Field(..., description="OVN IP name of the internal endpoint")
    protocol: str = Field("tcp", pattern=r"^(tcp|udp)$", description="Protocol")
    internal_port: str = Field(..., description="Internal port")
    external_port: str = Field(..., description="External port")

    @field_validator("internal_port", "external_port")
    @classmethod
    def validate_port(cls, v: str) -> str:
        try:
            port = int(v)
            if not (1 <= port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f"Port must be an integer between 1 and 65535, got {v!r}")
        return v


class OvnFipCreateRequest(BaseModel):
    """Request to create an OVN Floating IP (1:1 NAT)."""
    ovn_eip: str = Field(..., description="Dedicated OvnEip for this FIP")
    ip_name: str = Field(..., description="OVN IP name of the internal endpoint")
    ip_type: Optional[str] = Field(None, description="IP type (e.g. 'virt' for KubeVirt VMs)")


class OvnEipInfo(BaseModel):
    """OVN EIP status info."""
    name: str
    v4ip: str = ""
    type: str = "nat"
    external_subnet: str = ""
    ready: bool = False
    vpc: str = ""


class OvnSnatRuleInfo(BaseModel):
    """OVN SNAT rule info."""
    name: str
    ovn_eip: str = ""
    v4ip: str = ""
    vpc: str = ""
    vpc_subnet: str = ""
    internal_cidr: str = ""
    ready: bool = False


class OvnDnatRuleInfo(BaseModel):
    """OVN DNAT rule info."""
    name: str
    ovn_eip: str = ""
    v4ip: str = ""
    protocol: str = ""
    internal_port: str = ""
    external_port: str = ""
    ip_name: str = ""
    ready: bool = False


class OvnFipInfo(BaseModel):
    """OVN Floating IP info."""
    name: str
    ovn_eip: str = ""
    v4ip: str = ""
    ip_name: str = ""
    ready: bool = False


class OvnGatewayResponse(BaseModel):
    """Response model for an OVN gateway."""
    name: str
    vpc_name: str = ""
    subnet_name: str = ""
    external_subnet: str = ""
    eip: Optional[OvnEipInfo] = None
    snat_rules: list[OvnSnatRuleInfo] = []
    dnat_rules: list[OvnDnatRuleInfo] = []
    fips: list[OvnFipInfo] = []
    lsp_patched: bool = False
    ready: bool = False


class OvnGatewayListResponse(BaseModel):
    """List of OVN gateways."""
    items: list[OvnGatewayResponse]
    total: int
