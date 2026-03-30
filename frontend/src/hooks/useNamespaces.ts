import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import * as clusterApi from '@/api/cluster';
import type { ClusterSettings } from '@/types/cluster';

export function useNamespaces() {
  return useQuery({
    queryKey: ['namespaces'],
    queryFn: clusterApi.listNamespaces,
  });
}

export function useNodes() {
  return useQuery({
    queryKey: ['nodes'],
    queryFn: clusterApi.listNodes,
  });
}

export function useClusterStatus() {
  return useQuery({
    queryKey: ['cluster-status'],
    queryFn: clusterApi.getClusterStatus,
  });
}

export function useUserResources() {
  return useQuery({
    queryKey: ['user-resources'],
    queryFn: clusterApi.getUserResources,
    refetchInterval: 30000,
    placeholderData: (prev) => prev,
  });
}

export function useRecentActivity(limit: number = 10) {
  return useQuery({
    queryKey: ['recent-activity', limit],
    queryFn: () => clusterApi.getRecentActivity(limit),
    refetchInterval: 15000,
    placeholderData: (prev) => prev,
  });
}

export function useClusterSettings() {
  return useQuery({
    queryKey: ['cluster-settings-config'],
    queryFn: clusterApi.getClusterSettings,
  });
}

export function useUpdateClusterSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: ClusterSettings) => clusterApi.updateClusterSettings(settings),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cluster-settings-config'] });
    },
  });
}
