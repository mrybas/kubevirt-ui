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
    # The /30 carrying the link, and this side's address on it. Empty for
    # peerings created before the link was modelled.
    link_cidr: str = ""
    local_connect_ip: str = ""
    remote_connect_ip: str = ""


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
    # Default ON: every tenant prefix is announced to the same upstream
    # router, which forwards between them, so an un-isolated VPC is reachable
    # from every other tenant the moment it exists. That has to be closed at
    # creation, not remembered later.
    isolated: bool = Field(
        True,
        description=(
            "Write tenant-isolation ACLs on the default subnet: reachable from "
            "the internet and from `shared_cidrs`, but not from other tenant "
            "VPCs. Requires TENANT_SUPERNET to be configured on the backend; "
            "without it the VPC is created un-isolated and says so in the "
            "response."
        ),
    )
    shared_cidrs: list[str] = Field(
        default_factory=list,
        description=(
            "Prefixes this VPC may still reach while isolated — shared "
            "services such as corporate git. Written as higher-priority allows."
        ),
    )
    tenant: Optional[str] = Field(None, description="Tenant name to bind this VPC to")
    # T7 — folder/env scoping. Stamped onto VPC.metadata.labels + default
    # subnet labels so the tenant-create wizard can list VPCs eligible for
    # a given (folder, env) pair via a label selector.
    folder: Optional[str] = Field(
        None,
        description=(
            "Folder this VPC is scoped to. Stamped as "
            "`kubevirt-ui.io/folder` label. Required when `environment` is set."
        ),
    )
    environment: Optional[str] = Field(
        None,
        description=(
            "Environment within the folder this VPC is scoped to. Stamped as "
            "`kubevirt-ui.io/environment` label. When omitted, the VPC is "
            "folder-wide (eligible for all envs under `folder`)."
        ),
    )
    namespaces: list[str] = Field(
        default_factory=list,
        description=(
            "Namespaces bound to this VPC (becomes spec.namespaces on the Vpc and "
            "default subnet). Mutually independent of 'tenant': when both are "
            "supplied, 'namespaces' wins. When neither is supplied, the VPC is "
            "created with no namespace binding."
        ),
    )
    enable_nat_gateway: bool = Field(
        False,
        description="Enable NAT gateway for internet access from VPC",
    )
    static_routes: list[VpcStaticRoute] = Field(
        default_factory=list,
        description="Initial static routes",
    )

    @model_validator(mode="after")
    def _env_requires_folder(self) -> "VpcCreateRequest":
        if self.environment and not self.folder:
            raise ValueError(
                "`environment` requires `folder` (an env is always scoped under a folder)"
            )
        return self

    @field_validator("namespaces")
    @classmethod
    def validate_namespace_names(cls, v: list[str]) -> list[str]:
        """Surface-level format check; existence + managed-label is enforced in the handler."""
        import re
        ns_re = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        for ns in v:
            if not ns or len(ns) > 253 or not ns_re.match(ns):
                raise ValueError(
                    f"Invalid namespace name: {ns!r} (must match "
                    f"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$, max 253 chars)"
                )
        # De-dup while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for ns in v:
            if ns not in seen:
                seen.add(ns)
                out.append(ns)
        return out

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
    # T7 — folder/env scoping (sourced from labels). None means the VPC is
    # not scoped to a specific folder/env (cluster-wide).
    folder: Optional[str] = None
    environment: Optional[str] = None
    enable_nat_gateway: bool = False
    default_subnet: Optional[str] = None
    subnets: list[VpcSubnetInfo] = []
    peerings: list[VpcPeeringInfo] = []
    static_routes: list[VpcStaticRoute] = []
    namespaces: list[str] = []
    ready: bool = False
    conditions: list[dict] = []
    # Whether tenant-isolation ACLs were actually written on the default
    # subnet. Reports what happened, not what was asked for: requesting
    # isolation without a configured TENANT_SUPERNET leaves this false.
    isolated: bool = False
    # Where the object came from: "ui" (we created it), "system" (kube-ovn's
    # own `ovn-cluster`), "external" (CLI, GitOps, another operator). This used
    # to be a visibility filter, which hid half of a brownfield cluster from
    # the console; it is a badge now.
    origin: str = "external"


class VpcListResponse(BaseModel):
    """Paginated list of VPCs."""
    items: list[VpcResponse]
    total: int


class VpcPeeringCreateRequest(BaseModel):
    """Request to create a VPC peering connection."""
    remote_vpc: str = Field(..., description="Remote VPC name to peer with")
    link_cidr: Optional[str] = Field(
        None,
        description=(
            "The /30 carrying the peering link. Allocated from link-local "
            "space (169.254.101.0/24) when omitted — these addresses exist "
            "only between the two VPC routers and are never announced."
        ),
    )


class VpcPeeringDeleteRequest(BaseModel):
    """Not used as a body — remote_vpc comes from path."""
    pass


class VpcStaticRoutesUpdateRequest(BaseModel):
    """Request to replace all static routes on a VPC."""
    static_routes: list[VpcStaticRoute]


# ============================================================================
# VpcDns Models (T6 — per-VPC DNS forwarder)
# ============================================================================

class VpcDnsStatus(BaseModel):
    """Reconciliation status of a per-VPC DNS deployment.

    `phase`:
      - ``ready``: VpcDns deployment has at least one active pod and the
        Active condition is True.
      - ``pending``: CR exists but pods aren't running yet.
      - ``error``: a condition reports a non-recoverable error.
      - ``absent``: no VpcDns CR exists for the VPC (auto-create failed,
        or admin pre-dates T2 auto-create). The handler returns 404 in
        this case, so callers won't normally see this value.
    """
    phase: str = Field(
        "absent",
        description="ready / pending / error / absent",
    )
    active_pods: int = Field(
        0,
        description="Number of active VpcDns deployment pods",
    )
    message: Optional[str] = Field(
        None,
        description="Status message (typically from the latest Active condition)",
    )


class VpcDnsResponse(BaseModel):
    """Response model for a VpcDns CR.

    `coredns_vip` is the cluster-wide shared VIP every VpcDns instance
    listens on (autodiscovered from the cluster config — see
    ``tenants_common._vpcdns_vip``). The VIP is consumed by VPC workloads
    via the subnet's ``spec.dhcpV4Options.dns_server``; surfacing it here
    lets the UI show admins what DNS IP pods will receive.
    """
    name: str = Field(..., description="VpcDns CR name (always `<vpc>-dns`)")
    vpc: str
    subnet: str
    replicas: int = Field(1, description="Desired VpcDns deployment replicas")
    coredns_vip: str = Field(
        ...,
        description="Cluster-wide shared VIP that every VpcDns instance listens on",
    )
    status: VpcDnsStatus = Field(default_factory=VpcDnsStatus)


class VpcDnsUpdateRequest(BaseModel):
    """Request to patch a VpcDns CR.

    All fields optional; only provided fields are patched. `subnet` (if
    provided) must exist and belong to the VPC — validated server-side.
    `replicas` must be >= 1.
    """
    subnet: Optional[str] = Field(
        None,
        description="Subnet within the VPC the VpcDns deployment binds to",
    )
    replicas: Optional[int] = Field(
        None,
        ge=1,
        description="Desired VpcDns deployment replicas (>= 1)",
    )


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
