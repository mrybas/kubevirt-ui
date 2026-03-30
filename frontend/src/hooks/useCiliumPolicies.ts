import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listCiliumPolicies,
  getCiliumPolicy,
  createCiliumPolicy,
  deleteCiliumPolicy,
} from '../api/cilium_policy';
import type { CiliumPolicyCreateRequest } from '../types/cilium_policy';

export function useCiliumPolicies(namespace?: string) {
  return useQuery({
    queryKey: ['cilium-policies', namespace],
    queryFn: () => listCiliumPolicies(namespace),
    refetchInterval: 30000,
  });
}

export function useCiliumPolicy(namespace: string | undefined, name: string | undefined) {
  return useQuery({
    queryKey: ['cilium-policy', namespace, name],
    queryFn: () => getCiliumPolicy(namespace!, name!),
    enabled: !!namespace && !!name,
  });
}

export function useCreateCiliumPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CiliumPolicyCreateRequest) => createCiliumPolicy(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cilium-policies'] });
    },
  });
}

export function useDeleteCiliumPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      deleteCiliumPolicy(namespace, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cilium-policies'] });
    },
  });
}
