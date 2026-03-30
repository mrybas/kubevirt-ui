/**
 * VPC hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listVpcs,
  getVpc,
  createVpc,
  deleteVpc,
  addVpcPeering,
  removeVpcPeering,
  getVpcRoutes,
  updateVpcRoutes,
} from '../api/vpcs';
import type { CreateVpcRequest, AddVpcPeeringRequest, UpdateVpcRoutesRequest } from '../types/vpc';

export function useVpcs() {
  return useQuery({
    queryKey: ['vpcs'],
    queryFn: listVpcs,
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

export function useCreateVpc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateVpcRequest) => createVpc(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs'] });
    },
  });
}

export function useDeleteVpc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteVpc(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs'] });
    },
  });
}

export function useAddVpcPeering(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AddVpcPeeringRequest) => addVpcPeering(vpcName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs'] });
    },
  });
}

export function useRemoveVpcPeering(vpcName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (remoteVpc: string) => removeVpcPeering(vpcName, remoteVpc),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vpcs'] });
    },
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
