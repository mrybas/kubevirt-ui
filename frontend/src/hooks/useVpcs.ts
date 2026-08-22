/**
 * VPC hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listVpcs,
  getVpc,
  createVpc,
  deleteVpc,
  setVpcScope,
  addVpcPeering,
  removeVpcPeering,
  getVpcRoutes,
  updateVpcRoutes,
  getVpcDns,
  updateVpcDns,
  recreateVpcDns,
  getVpcDnsPolicy,
  updateVpcDnsPolicy,
  recreateVpcDnsPolicy,
  disableVpcDnsPolicy,
} from '../api/vpcs';
import type { CreateVpcRequest, AddVpcPeeringRequest, UpdateVpcRoutesRequest, UpdateVpcDnsRequest } from '../types/vpc';
import { ApiError } from '../api/client';
import { notify } from '../store/notifications';
import { settle } from './settle';

export function useVpcs(params?: { folder?: string; environment?: string }) {
  return useQuery({
    queryKey: ['vpcs', params?.folder ?? null, params?.environment ?? null],
    queryFn: () => listVpcs(params),
    refetchInterval: 30000,
  });
}

export function useVpc(name: string | undefined) {
  return useQuery({
    queryKey: ['vpcs', name],
    queryFn: () => getVpc(name!),
    enabled: !!name,
  });
}

// Creating or deleting a VPC also creates or deletes its default subnet, and
// the Subnets tab reads a different key. Without this it kept showing the
// list from before the VPC existed — "Total Subnets 2" with four on the
// cluster — until someone reloaded the page.
function invalidateVpcAndSubnets(queryClient: ReturnType<typeof useQueryClient>) {
  // And again over the next few seconds: the request writes a ManagedNetwork,
  // the operator builds the VPC and its subnet from it, and one invalidation
  // lands before any of that exists.
  settle(queryClient, [['vpcs'], ['network']]);
}

export function useCreateVpc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateVpcRequest) => createVpc(request),
    onSuccess: () => invalidateVpcAndSubnets(queryClient),
  });
}

export function useSetVpcScope(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: { folder: string | null; environment: string | null }) =>
      setVpcScope(vpcName, request),
    onSuccess: () => {
      invalidateVpcAndSubnets(queryClient);
      notify.success('VPC scope updated');
    },
    onError: (err: Error) => notify.error(err.message || 'Failed to update VPC scope'),
  });
}

export function useDeleteVpc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteVpc(name),
    onSuccess: () => invalidateVpcAndSubnets(queryClient),
  });
}

export function useAddVpcPeering(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AddVpcPeeringRequest) => addVpcPeering(vpcName, request),
    // "No peerings configured", with the ManagedNetworkPeering already
    // written and both legs in place — UAT run 4, G2.
    onSuccess: () => settle(queryClient, [['vpcs']]),
  });
}

export function useRemoveVpcPeering(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (remoteVpc: string) => removeVpcPeering(vpcName, remoteVpc),
    onSuccess: () => settle(queryClient, [['vpcs']]),
  });
}

export function useVpcRoutes(name: string | undefined) {
  return useQuery({
    queryKey: ['vpcs', name, 'routes'],
    queryFn: () => getVpcRoutes(name!),
    enabled: !!name,
  });
}

export function useUpdateVpcRoutes(name: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: UpdateVpcRoutesRequest) => updateVpcRoutes(name, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs', name, 'routes'] });
    },
  });
}

export function useVpcDns(vpcName: string | undefined) {
  return useQuery({
    queryKey: ['vpcs', vpcName, 'dns'],
    queryFn: () => getVpcDns(vpcName!),
    enabled: !!vpcName,
    retry: (failureCount, error) => {
      // Don't retry on 404 — absent VpcDns is a valid state, not a transient error
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 3;
    },
  });
}

export function useUpdateVpcDns(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: UpdateVpcDnsRequest) => updateVpcDns(vpcName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs', vpcName, 'dns'] });
    },
  });
}

export function useRecreateVpcDns(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => recreateVpcDns(vpcName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs', vpcName, 'dns'] });
    },
  });
}

export function useDisableVpcDnsPolicy(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => disableVpcDnsPolicy(vpcName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs', vpcName, 'dns-policy'] });
    },
  });
}

export function useVpcDnsPolicy(vpcName: string | undefined) {
  return useQuery({
    queryKey: ['vpcs', vpcName, 'dns-policy'],
    queryFn: () => getVpcDnsPolicy(vpcName!),
    enabled: !!vpcName,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 3;
    },
  });
}

export function useUpdateVpcDnsPolicy(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => updateVpcDnsPolicy(vpcName, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs', vpcName, 'dns-policy'] });
    },
  });
}

export function useRecreateVpcDnsPolicy(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => recreateVpcDnsPolicy(vpcName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs', vpcName, 'dns-policy'] });
    },
  });
}
