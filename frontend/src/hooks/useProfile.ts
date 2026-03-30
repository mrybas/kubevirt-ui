/**
 * Profile hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getProfile, updateSSHKeys } from '../api/profile';
import type { UpdateSSHKeysRequest } from '../api/profile';

export function useProfile() {
  return useQuery({
    queryKey: ['profile'],
    queryFn: getProfile,
  });
}

export function useUpdateSSHKeys() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: UpdateSSHKeysRequest) => updateSSHKeys(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profile'] });
    },
  });
}
