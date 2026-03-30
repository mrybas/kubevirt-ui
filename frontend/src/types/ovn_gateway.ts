/**
 * OVN Gateway Types
 * Matches backend app/models/ovn_gateway.py
 */

export interface OvnEipInfo {
  name: string;
  v4ip: string;
  type: string;
  external_subnet: string;
  ready: boolean;
  vpc: string;
}

export interface OvnSnatRuleInfo {
  name: string;
  ovn_eip: string;
  v4ip: string;
  vpc: string;
  vpc_subnet: string;
  internal_cidr: string;
  ready: boolean;
}

export interface OvnDnatRuleInfo {
  name: string;
  ovn_eip: string;
  v4ip: string;
  protocol: string;
  internal_port: string;
  external_port: string;
  ip_name: string;
  ready: boolean;
}

export interface OvnFipInfo {
  name: string;
  ovn_eip: string;
  v4ip: string;
  ip_name: string;
  ready: boolean;
}

export interface OvnGateway {
  name: string;
  vpc_name: string;
  subnet_name: string;
  external_subnet: string;
  eip: OvnEipInfo | null;
  snat_rules: OvnSnatRuleInfo[];
  dnat_rules: OvnDnatRuleInfo[];
  fips: OvnFipInfo[];
  lsp_patched: boolean;
  ready: boolean;
}

export interface OvnGatewayListResponse {
  items: OvnGateway[];
  total: number;
}

export interface CreateOvnGatewayRequest {
  name: string;
  vpc_name: string;
  subnet_name: string;
  external_subnet?: string;
  eip_address?: string;
  shared_eip?: string;
  auto_snat: boolean;
}

export interface CreateOvnDnatRuleRequest {
  ovn_eip: string;
  ip_name: string;
  protocol: string;
  internal_port: string;
  external_port: string;
}

export interface CreateOvnFipRequest {
  ovn_eip: string;
  ip_name: string;
  ip_type?: string;
}
