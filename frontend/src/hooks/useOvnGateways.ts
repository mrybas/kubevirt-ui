/**
 * OVN Gateway hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listOvnGateways,
  getOvnGateway,
  createOvnGateway,
  deleteOvnGateway,
  createDnatRule,
  deleteDnatRule,
  createFip,
  deleteFip,
} from '../api/ovn_gateway';
import type {
  CreateOvnGatewayRequest,
  CreateOvnDnatRuleRequest,
  CreateOvnFipRequest,
} from '../types/ovn_gateway';
import { notify } from '../store/notifications';

export function useOvnGateways() {
  return useQuery({
    queryKey: ['ovn-gateways'],
    queryFn: listOvnGateways,
    refetchInterval: 30000,
  });
}

export function useOvnGateway(name: string | undefined) {
  return useQuery({
    queryKey: ['ovn-gateways', name],
    queryFn: () => getOvnGateway(name!),
    enabled: !!name,
    refetchInterval: 15000,
  });
}

export function useCreateOvnGateway() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateOvnGatewayRequest) => createOvnGateway(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ovn-gateways'] });
      notify.success('OVN gateway created successfully');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create OVN gateway');
    },
  });
}

export function useDeleteOvnGateway() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteOvnGateway(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ovn-gateways'] });
      notify.success('OVN gateway deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete OVN gateway');
    },
  });
}

export function useCreateDnatRule(gatewayName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateOvnDnatRuleRequest) => createDnatRule(gatewayName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ovn-gateways'] });
      notify.success('DNAT rule created');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create DNAT rule');
    },
  });
}

export function useDeleteDnatRule(gatewayName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ruleName: string) => deleteDnatRule(gatewayName, ruleName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ovn-gateways'] });
      notify.success('DNAT rule deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete DNAT rule');
    },
  });
}

export function useCreateFip(gatewayName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateOvnFipRequest) => createFip(gatewayName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ovn-gateways'] });
      notify.success('Floating IP created');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create Floating IP');
    },
  });
}

export function useDeleteFip(gatewayName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (fipName: string) => deleteFip(gatewayName, fipName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ovn-gateways'] });
      notify.success('Floating IP deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete Floating IP');
    },
  });
}
