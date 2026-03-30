import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listSecurityBaselines,
  createSecurityBaseline,
  deleteSecurityBaseline,
} from '../api/security_baseline';
import type { SecurityBaselineCreateRequest } from '../types/cilium_policy';

export function useSecurityBaseline() {
  return useQuery({
    queryKey: ['security-baseline'],
    queryFn: listSecurityBaselines,
    refetchInterval: 30000,
  });
}

export function useCreateSecurityBaseline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: SecurityBaselineCreateRequest) => createSecurityBaseline(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['security-baseline'] });
    },
  });
}

export function useDeleteSecurityBaseline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteSecurityBaseline(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['security-baseline'] });
    },
  });
}
