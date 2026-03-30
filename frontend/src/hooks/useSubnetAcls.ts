import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getSubnetAcls,
  replaceSubnetAcls,
  addSubnetAcl,
  deleteSubnetAcl,
  getAclPresets,
} from '../api/subnet_acl';
import type { SubnetAclUpdateRequest, SubnetAclAddRequest } from '../types/subnet_acl';

export function useSubnetAcls(subnetName: string | undefined) {
  return useQuery({
    queryKey: ['subnet-acls', subnetName],
    queryFn: () => getSubnetAcls(subnetName!),
    enabled: !!subnetName,
  });
}

export function useAclPresets(subnetName: string | undefined) {
  return useQuery({
    queryKey: ['subnet-acl-presets', subnetName],
    queryFn: () => getAclPresets(subnetName!),
    enabled: !!subnetName,
  });
}

export function useReplaceSubnetAcls(subnetName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: SubnetAclUpdateRequest) => replaceSubnetAcls(subnetName, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['subnet-acls', subnetName] });
    },
  });
}

export function useAddSubnetAcl(subnetName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: SubnetAclAddRequest) => addSubnetAcl(subnetName, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['subnet-acls', subnetName] });
    },
  });
}

export function useDeleteSubnetAcl(subnetName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (index: number) => deleteSubnetAcl(subnetName, index),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['subnet-acls', subnetName] });
    },
  });
}
