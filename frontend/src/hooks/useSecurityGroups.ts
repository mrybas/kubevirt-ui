/**
 * SecurityGroup hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listSecurityGroups,
  getSecurityGroup,
  createSecurityGroup,
  updateSecurityGroup,
  deleteSecurityGroup,
  getVmSecurityGroups,
  assignSecurityGroupToVm,
  removeSecurityGroupFromVm,
} from '../api/securityGroups';
import type { CreateSecurityGroupRequest, UpdateSecurityGroupRequest, AssignSecurityGroupRequest } from '../types/vpc';

export function useSecurityGroups() {
  return useQuery({
    queryKey: ['security-groups'],
    queryFn: listSecurityGroups,
    refetchInterval: 30000,
  });
}

export function useSecurityGroup(name: string | undefined) {
  return useQuery({
    queryKey: ['security-groups', name],
    queryFn: () => getSecurityGroup(name!),
    enabled: !!name,
  });
}

export function useCreateSecurityGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateSecurityGroupRequest) => createSecurityGroup(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['security-groups'] });
    },
  });
}

export function useUpdateSecurityGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, request }: { name: string; request: UpdateSecurityGroupRequest }) =>
      updateSecurityGroup(name, request),
    onSuccess: (_data, { name }) => {
      queryClient.invalidateQueries({ queryKey: ['security-groups', name] });
      queryClient.invalidateQueries({ queryKey: ['security-groups'] });
    },
  });
}

export function useDeleteSecurityGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteSecurityGroup(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['security-groups'] });
    },
  });
}

export function useVmSecurityGroups(namespace: string | undefined, vm: string | undefined) {
  return useQuery({
    queryKey: ['security-groups', 'vms', namespace, vm],
    queryFn: () => getVmSecurityGroups(namespace!, vm!),
    enabled: !!namespace && !!vm,
  });
}

export function useAssignSecurityGroupToVm(namespace: string, vm: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AssignSecurityGroupRequest) => assignSecurityGroupToVm(namespace, vm, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['security-groups', 'vms', namespace, vm] });
    },
  });
}

export function useRemoveSecurityGroupFromVm(namespace: string, vm: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sg: string) => removeSecurityGroupFromVm(namespace, vm, sg),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['security-groups', 'vms', namespace, vm] });
    },
  });
}
