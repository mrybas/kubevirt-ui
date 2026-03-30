/**
 * VPC & SecurityGroup Types
 */

// ---------------------------------------------------------------------------
// VPC
// ---------------------------------------------------------------------------

export interface VpcSubnet {
  name: string;
  cidr_block: string;
  gateway: string;
  available_ips: number;
  used_ips: number;
}

export interface VpcPeering {
  name: string;
  local_vpc: string;
  remote_vpc: string;
}

export interface VpcRoute {
  cidr: string;
  next_hop: string;
}

export interface Vpc {
  name: string;
  tenant: string | null;
  enable_nat_gateway: boolean;
  default_subnet: string | null;
  subnets: VpcSubnet[];
  peerings: VpcPeering[];
  static_routes: VpcRoute[];
  namespaces: string[];
  ready: boolean;
  conditions: Record<string, unknown>[];
}

export interface VpcListResponse {
  items: Vpc[];
  total: number;
}

export interface CreateVpcRequest {
  name: string;
  subnet_cidr?: string;
  enable_nat_gateway?: boolean;
}

export interface AddVpcPeeringRequest {
  remote_vpc: string;
}

export interface VpcRoutesResponse {
  routes: VpcRoute[];
}

export interface UpdateVpcRoutesRequest {
  routes: VpcRoute[];
}

// ---------------------------------------------------------------------------
// SecurityGroup
// ---------------------------------------------------------------------------

export type SgProtocol = 'tcp' | 'udp' | 'icmp' | 'all';
export type SgAction = 'allow' | 'drop';

export interface SecurityGroupRule {
  priority: number;
  protocol: SgProtocol;
  port_range: string;       // e.g. "80", "443-8443", "" for all
  remote_address: string;   // CIDR or IP
  action: SgAction;
}

export interface SecurityGroup {
  name: string;
  ingress_rules: SecurityGroupRule[];
  egress_rules: SecurityGroupRule[];
  created_at: string | null;
}

export interface SecurityGroupListResponse {
  items: SecurityGroup[];
  total: number;
}

export interface CreateSecurityGroupRequest {
  name: string;
  ingress_rules?: SecurityGroupRule[];
  egress_rules?: SecurityGroupRule[];
}

export interface UpdateSecurityGroupRequest {
  ingress_rules: SecurityGroupRule[];
  egress_rules: SecurityGroupRule[];
}

export interface VmSecurityGroupsResponse {
  security_groups: string[];
}

export interface AssignSecurityGroupRequest {
  security_group: string;
}
