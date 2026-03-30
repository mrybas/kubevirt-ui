import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listDataVolumes,
  createDataVolume,
  deleteDataVolume,
  listPVCs,
  listStorageClasses,
  listStorageClassDetails,
} from '../api/storage';
import type { DataVolumeCreateRequest } from '../types/storage';

export function useDataVolumes(namespace: string | null) {
  return useQuery({
    queryKey: ['datavolumes', namespace],
    queryFn: () => (namespace ? listDataVolumes(namespace) : Promise.resolve({ items: [], total: 0 })),
    enabled: !!namespace,
    refetchInterval: 5000,
  });
}

export function usePVCs(namespace: string | null) {
  return useQuery({
    queryKey: ['pvcs', namespace],
    queryFn: () => (namespace ? listPVCs(namespace) : Promise.resolve({ items: [], total: 0 })),
    enabled: !!namespace,
    refetchInterval: 10000,
  });
}

export function useStorageClasses() {
  return useQuery({
    queryKey: ['storageclasses'],
    queryFn: listStorageClasses,
    staleTime: 60000,
  });
}

export function useCreateDataVolume(namespace: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: DataVolumeCreateRequest) => createDataVolume(namespace, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['datavolumes', namespace] });
      queryClient.invalidateQueries({ queryKey: ['pvcs', namespace] });
    },
  });
}

export function useStorageClassDetails() {
  return useQuery({
    queryKey: ['storageclasses', 'details'],
    queryFn: listStorageClassDetails,
    staleTime: 30000,
  });
}

export function useDeleteDataVolume(namespace: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteDataVolume(namespace, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['datavolumes', namespace] });
      queryClient.invalidateQueries({ queryKey: ['pvcs', namespace] });
    },
  });
}
