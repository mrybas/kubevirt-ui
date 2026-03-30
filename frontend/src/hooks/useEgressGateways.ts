/**
 * Egress Gateway hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listEgressGateways,
  getEgressGateway,
  createEgressGateway,
  deleteEgressGateway,
  attachVpc,
  detachVpc,
} from '../api/egress';
import type { CreateEgressGatewayRequest, AttachVpcRequest, DetachVpcRequest } from '../types/egress';
import { notify } from '../store/notifications';

export function useEgressGateways() {
  return useQuery({
    queryKey: ['egress-gateways'],
    queryFn: listEgressGateways,
    refetchInterval: 30000,
  });
}

export function useEgressGateway(name: string | undefined) {
  return useQuery({
    queryKey: ['egress-gateways', name],
    queryFn: () => getEgressGateway(name!),
    enabled: !!name,
    refetchInterval: 15000,
  });
}

export function useCreateEgressGateway() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateEgressGatewayRequest) => createEgressGateway(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['egress-gateways'] });
      notify.success('Egress gateway created successfully');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create egress gateway');
    },
  });
}

export function useDeleteEgressGateway() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteEgressGateway(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['egress-gateways'] });
      notify.success('Egress gateway deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete egress gateway');
    },
  });
}

export function useAttachVpc(gatewayName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AttachVpcRequest) => attachVpc(gatewayName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['egress-gateways'] });
      notify.success('VPC attached to egress gateway');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to attach VPC');
    },
  });
}

export function useDetachVpc(gatewayName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: DetachVpcRequest) => detachVpc(gatewayName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['egress-gateways'] });
      notify.success('VPC detached from egress gateway');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to detach VPC');
    },
  });
}
