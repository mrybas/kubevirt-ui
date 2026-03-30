/**
 * Egress Gateway Types
 * Matches backend app/models/egress_gateway.py
 */

export interface AttachedVpcInfo {
  vpc_name: string;
  subnet_name: string;
  cidr: string;
  transit_ip: string;
  peering_name: string;
}

export interface GatewayPodInfo {
  pod: string;
  node: string;
  internal_ip: string;
  external_ip: string;
}

export interface EgressGateway {
  name: string;
  gw_vpc_name: string;
  gw_vpc_cidr: string;
  transit_cidr: string;
  macvlan_subnet: string;
  replicas: number;
  bfd_enabled: boolean;
  node_selector: Record<string, string>;
  exclude_ips: string[];
  attached_vpcs: AttachedVpcInfo[];
  assigned_ips: GatewayPodInfo[];
  ready: boolean;
  status: Record<string, unknown> | null; // raw k8s VpcEgressGateway status
}

export interface EgressGatewayListResponse {
  items: EgressGateway[];
  total: number;
}

export interface CreateEgressGatewayRequest {
  name: string;
  gw_vpc_cidr: string;
  transit_cidr: string;
  // Option 1: existing subnet
  macvlan_subnet?: string;
  // Option 2: create new macvlan subnet
  external_interface?: string;
  external_cidr?: string;
  external_gateway?: string;
  replicas: number;
  bfd_enabled: boolean;
  node_selector: Record<string, string>;
  exclude_ips?: string[];
}

export interface AttachVpcRequest {
  vpc_name: string;
  subnet_name: string;
  cidr: string;
}

export interface DetachVpcRequest {
  vpc_name: string;
  subnet_name: string;
}

export interface DetachVpcResponse {
  status: string;
  gateway: string;
  vpc: string;
}
