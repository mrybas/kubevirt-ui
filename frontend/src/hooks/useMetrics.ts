import { useQuery } from '@tanstack/react-query';
import { useState, useCallback } from 'react';
import * as metricsApi from '@/api/metrics';

// Time range presets
export type TimeRange = '1h' | '6h' | '24h' | '7d';

export const RANGE_SECONDS: Record<TimeRange, number> = {
  '1h': 3600,
  '6h': 21600,
  '24h': 86400,
  '7d': 604800,
};

export function useMetricsStatus() {
  return useQuery({
    queryKey: ['metrics', 'status'],
    queryFn: metricsApi.getMetricsStatus,
    staleTime: 60_000,
  });
}

export function useMetricsRange(
  query: string,
  timeRange: TimeRange = '1h',
  enabled = true,
) {
  const now = Math.floor(Date.now() / 1000);
  const start = now - RANGE_SECONDS[timeRange];

  return useQuery({
    queryKey: ['metrics', 'range', query, timeRange],
    queryFn: () => metricsApi.queryRange(query, start, now),
    enabled: enabled && !!query,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

export function useMetricsInstant(query: string, enabled = true) {
  return useQuery({
    queryKey: ['metrics', 'instant', query],
    queryFn: () => metricsApi.queryInstant(query),
    enabled: enabled && !!query,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

/**
 * Hook that manages time range selection + manual refresh for a metrics panel.
 */
export function useMetricsPanel(defaultRange: TimeRange = '1h') {
  const [timeRange, setTimeRange] = useState<TimeRange>(defaultRange);
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  return { timeRange, setTimeRange, refreshKey, refresh };
}
