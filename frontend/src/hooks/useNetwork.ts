import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getNetworkOverview,
  listProviderNetworks,
  getProviderNetwork,
  createProviderNetwork,
  deleteProviderNetwork,
  listVlans,
  createVlan,
  deleteVlan,
  listSubnets,
  getSubnetDetail,
  createSubnet,
  deleteSubnet,
  reserveIP,
  unreserveIP,
  getNodesNetworkInfo,
} from '../api/network';
import type {
  ProviderNetworkCreate,
  VlanCreate,
  SubnetCreate,
  ReserveIPRequest,
} from '../types/network';
import { notify } from '../store/notifications';

// ============================================================================
// Network Overview
// ============================================================================

export function useNetworkOverview() {
  return useQuery({
    queryKey: ['network', 'overview'],
    queryFn: getNetworkOverview,
  });
}

// ============================================================================
// Provider Networks
// ============================================================================

export function useProviderNetworks() {
  return useQuery({
    queryKey: ['network', 'provider-networks'],
    queryFn: listProviderNetworks,
  });
}

export function useProviderNetwork(name: string) {
  return useQuery({
    queryKey: ['network', 'provider-networks', name],
    queryFn: () => getProviderNetwork(name),
    enabled: !!name,
  });
}

export function useCreateProviderNetwork() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: ProviderNetworkCreate) => createProviderNetwork(data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['network'] });
      notify.success('Provider Network Created', `Provider network "${variables.name}" has been created`);
    },
    onError: (error: Error, variables) => {
      notify.error('Failed to Create Provider Network', error.message || `Failed to create provider network "${variables.name}"`);
    },
  });
}

export function useDeleteProviderNetwork() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteProviderNetwork(name),
    onSuccess: (_, name) => {
      queryClient.invalidateQueries({ queryKey: ['network'] });
      notify.success('Provider Network Deleted', `Provider network "${name}" has been deleted`);
    },
    onError: (error: Error, name) => {
      notify.error('Failed to Delete Provider Network', error.message || `Failed to delete provider network "${name}"`);
    },
  });
}

// ============================================================================
// VLANs
// ============================================================================

export function useVlans() {
  return useQuery({
    queryKey: ['network', 'vlans'],
    queryFn: listVlans,
  });
}

export function useCreateVlan() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: VlanCreate) => createVlan(data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['network'] });
      notify.success('VLAN Created', `VLAN "${variables.name}" (ID: ${variables.id}) has been created`);
    },
    onError: (error: Error, variables) => {
      notify.error('Failed to Create VLAN', error.message || `Failed to create VLAN "${variables.name}"`);
    },
  });
}

export function useDeleteVlan() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteVlan(name),
    onSuccess: (_, name) => {
      queryClient.invalidateQueries({ queryKey: ['network'] });
      notify.success('VLAN Deleted', `VLAN "${name}" has been deleted`);
    },
    onError: (error: Error, name) => {
      notify.error('Failed to Delete VLAN', error.message || `Failed to delete VLAN "${name}"`);
    },
  });
}

// ============================================================================
// Subnets
// ============================================================================

export function useSubnets() {
  return useQuery({
    queryKey: ['network', 'subnets'],
    queryFn: listSubnets,
  });
}

export function useSubnetDetail(name: string) {
  return useQuery({
    queryKey: ['network', 'subnets', name],
    queryFn: () => getSubnetDetail(name),
    enabled: !!name,
    refetchInterval: 10000, // Refresh every 10s for lease updates
  });
}

export function useCreateSubnet() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: SubnetCreate) => createSubnet(data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['network'] });
      notify.success('Subnet Created', `Subnet "${variables.name}" (${variables.cidr_block}) has been created`);
    },
    onError: (error: Error, variables) => {
      notify.error('Failed to Create Subnet', error.message || `Failed to create subnet "${variables.name}"`);
    },
  });
}

export function useDeleteSubnet() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteSubnet(name),
    onSuccess: (_, name) => {
      queryClient.invalidateQueries({ queryKey: ['network'] });
      notify.success('Subnet Deleted', `Subnet "${name}" has been deleted`);
    },
    onError: (error: Error, name) => {
      notify.error('Failed to Delete Subnet', error.message || `Failed to delete subnet "${name}"`);
    },
  });
}

// ============================================================================
// IP Reservation
// ============================================================================

export function useReserveIP(subnetName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: ReserveIPRequest) => reserveIP(subnetName, data),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['network', 'subnets', subnetName] });
      notify.success('IP Reserved', `Reserved ${result.count} IP(s): ${result.ip_or_range}`);
    },
    onError: (error: Error) => {
      notify.error('Failed to Reserve IP', error.message || 'Failed to reserve IP address');
    },
  });
}

export function useUnreserveIP(subnetName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (ipOrRange: string) => unreserveIP(subnetName, ipOrRange),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['network', 'subnets', subnetName] });
      notify.success('IP Unreserved', `Released reservation: ${result.ip_or_range}`);
    },
    onError: (error: Error) => {
      notify.error('Failed to Release IP Reservation', error.message || 'Failed to release IP reservation');
    },
  });
}

// ============================================================================
// Nodes Network Info
// ============================================================================

export function useNodesNetworkInfo() {
  return useQuery({
    queryKey: ['network', 'nodes'],
    queryFn: getNodesNetworkInfo,
  });
}
