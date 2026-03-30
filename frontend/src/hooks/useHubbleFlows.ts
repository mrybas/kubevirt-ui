import { useQuery } from '@tanstack/react-query';
import { getHubbleFlows, getHubbleStatus } from '../api/hubble';
import type { HubbleFlowsParams } from '../types/hubble';

export function useHubbleFlows(params: HubbleFlowsParams, autoRefresh = false) {
  return useQuery({
    queryKey: ['hubble-flows', params],
    queryFn: () => getHubbleFlows(params),
    refetchInterval: autoRefresh ? 5000 : false,
    staleTime: 0,
  });
}

export function useHubbleStatus() {
  return useQuery({
    queryKey: ['hubble-status'],
    queryFn: getHubbleStatus,
    refetchInterval: 30000,
  });
}
