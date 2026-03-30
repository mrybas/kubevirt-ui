// Network types for Kube-OVN integration

// ============================================================================
// Provider Network (Physical Network Connection)
// ============================================================================

export interface ProviderNetworkCreate {
  name: string;
  default_interface: string;
  custom_interfaces?: Record<string, string>; // node -> interface mapping
  // RECOMMENDED: Auto-create VLAN sub-interfaces (safe for single-NIC setups)
  auto_create_vlan_subinterfaces?: boolean;
  // LEGACY/DANGEROUS: Transfers IP from physical interface to OVS bridge
  exchange_link_name?: boolean;
}

export interface ProviderNetwork {
  name: string;
  default_interface: string;
  auto_create_vlan_subinterfaces: boolean; // Whether VLAN sub-interfaces are auto-created
  exchange_link_name: boolean; // Whether IP is transferred to bridge (legacy)
  ready: boolean;
  ready_nodes: string[];
  not_ready_nodes: string[];
  vlans: string[]; // List of VLAN names using this provider
  conditions: NetworkCondition[];
}

export interface NetworkCondition {
  type: string;
  status: string;
  reason?: string;
  message?: string;
  lastTransitionTime?: string;
}

// ============================================================================
// VLAN
// ============================================================================

export interface VlanCreate {
  name: string;
  id: number; // 0 = untagged
  provider: string; // ProviderNetwork name
}

export interface Vlan {
  name: string;
  id: number;
  provider: string;
}

// ============================================================================
// Subnet
// ============================================================================

export interface SubnetCreate {
  name: string;
  cidr_block: string;
  gateway: string;
  exclude_ips?: string[];
  vlan?: string;
  namespace?: string; // Namespace where NAD will be created (not needed for infrastructure subnets)
  vpc?: string; // For overlay networks
  purpose?: 'vm' | 'infrastructure'; // "vm" = creates NAD for VM attachment, "infrastructure" = for VPC NAT gateway
  enable_dhcp?: boolean;
  disable_gateway_check?: boolean; // Disable gateway ARP check (e.g. VLAN sub-interface without IP)
}

export interface SubnetStatistics {
  total: number;
  available: number;
  used: number;
  reserved: number;
}

export interface Subnet {
  name: string;
  cidr_block: string;
  gateway: string;
  exclude_ips: string[];
  provider?: string;
  vlan?: string;
  vpc?: string;
  namespace?: string; // Namespace where NAD is created
  protocol: string;
  enable_dhcp: boolean;
  disable_gateway_check: boolean;
  purpose: 'vm' | 'infrastructure'; // "vm" = for VM attachment, "infrastructure" = for VPC NAT gateway
  statistics?: SubnetStatistics;
  ready: boolean;
}

// ============================================================================
// IP Lease
// ============================================================================

export interface IPLease {
  ip_address: string;
  mac_address?: string;
  pod_name?: string;
  namespace?: string;
  node_name?: string;
  subnet: string;
  resource_type: 'vm' | 'pod';
  resource_name?: string;
}

// ============================================================================
// Reserved IP
// ============================================================================

export interface ReserveIPRequest {
  ip_or_range: string;
  note?: string;
}

export interface ReservedIP {
  ip_or_range: string;
  count: number;
  note?: string;
}

// ============================================================================
// Subnet Detail (with leases)
// ============================================================================

export interface SubnetDetail {
  subnet: Subnet;
  leases: IPLease[];
  reserved: ReservedIP[];
}

// ============================================================================
// Node Network Info
// ============================================================================

export interface NodeNetworkInfo {
  name: string;
  internal_ip?: string;
  interfaces: string[];
  annotations: Record<string, string>;
}

// ============================================================================
// Network Overview
// ============================================================================

export interface NetworkOverview {
  provider_networks: number;
  vlans: number;
  subnets: number;
  vpcs: number;
  total_ips_used: number;
  total_ips_available: number;
}

// ============================================================================
// VPC (Virtual Private Cloud)
// ============================================================================

export interface VpcCreate {
  name: string;
  subnet_cidr?: string;
  tenant?: string;
  enable_nat_gateway?: boolean;
}

export interface VpcSubnetResponse {
  name: string;
  cidr_block: string;
  gateway: string;
  available_ips: number;
  used_ips: number;
}

export interface NetworkVpcPeering {
  name: string;
  local_vpc: string;
  remote_vpc: string;
}

export interface VpcResponse {
  name: string;
  tenant?: string;
  subnets: VpcSubnetResponse[];
  peerings: NetworkVpcPeering[];
  ready: boolean;
  conditions: NetworkCondition[];
}

// ============================================================================
// Network Type (for UI selection)
// ============================================================================

export type NetworkType = 'external' | 'overlay' | 'vpc';

export interface NetworkCreateWizardStep {
  title: string;
  description: string;
}
