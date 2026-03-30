import { apiRequest } from './client';
import type {
  NetworkOverview,
  ProviderNetwork,
  ProviderNetworkCreate,
  Vlan,
  VlanCreate,
  Subnet,
  SubnetCreate,
  SubnetDetail,
  ReserveIPRequest,
  NodeNetworkInfo,
} from '../types/network';

const BASE = '/network';

// ============================================================================
// Network Overview
// ============================================================================

export async function getNetworkOverview(): Promise<NetworkOverview> {
  return apiRequest<NetworkOverview>(`${BASE}/overview`);
}

// ============================================================================
// Provider Networks
// ============================================================================

export async function listProviderNetworks(): Promise<ProviderNetwork[]> {
  return apiRequest<ProviderNetwork[]>(`${BASE}/provider-networks`);
}

export async function getProviderNetwork(name: string): Promise<ProviderNetwork> {
  return apiRequest<ProviderNetwork>(`${BASE}/provider-networks/${name}`);
}

export async function createProviderNetwork(data: ProviderNetworkCreate): Promise<ProviderNetwork> {
  return apiRequest<ProviderNetwork>(`${BASE}/provider-networks`, {
    method: 'POST',
    body: data,
  });
}

export async function deleteProviderNetwork(name: string): Promise<void> {
  await apiRequest<{ status: string }>(`${BASE}/provider-networks/${name}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// VLANs
// ============================================================================

export async function listVlans(): Promise<Vlan[]> {
  return apiRequest<Vlan[]>(`${BASE}/vlans`);
}

export async function createVlan(data: VlanCreate): Promise<Vlan> {
  return apiRequest<Vlan>(`${BASE}/vlans`, {
    method: 'POST',
    body: data,
  });
}

export async function deleteVlan(name: string): Promise<void> {
  await apiRequest<{ status: string }>(`${BASE}/vlans/${name}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// Subnets
// ============================================================================

export async function listSubnets(): Promise<Subnet[]> {
  return apiRequest<Subnet[]>(`${BASE}/subnets`);
}

export async function getSubnetDetail(name: string): Promise<SubnetDetail> {
  return apiRequest<SubnetDetail>(`${BASE}/subnets/${name}`);
}

export async function createSubnet(data: SubnetCreate): Promise<Subnet> {
  return apiRequest<Subnet>(`${BASE}/subnets`, {
    method: 'POST',
    body: data,
  });
}

export async function deleteSubnet(name: string): Promise<void> {
  await apiRequest<{ status: string }>(`${BASE}/subnets/${name}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// IP Reservation
// ============================================================================

export async function reserveIP(
  subnetName: string,
  data: ReserveIPRequest
): Promise<{ status: string; ip_or_range: string; count: number }> {
  return apiRequest(`${BASE}/subnets/${subnetName}/reserve`, {
    method: 'POST',
    body: data,
  });
}

export async function unreserveIP(
  subnetName: string,
  ipOrRange: string
): Promise<{ status: string; ip_or_range: string }> {
  // URL encode the IP range (handles .. in range)
  const encoded = encodeURIComponent(ipOrRange);
  return apiRequest(`${BASE}/subnets/${subnetName}/reserve/${encoded}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// Node Network Info
// ============================================================================

export async function getNodesNetworkInfo(): Promise<NodeNetworkInfo[]> {
  return apiRequest<NodeNetworkInfo[]>(`${BASE}/nodes`);
}
