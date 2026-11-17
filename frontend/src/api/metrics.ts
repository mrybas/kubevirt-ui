import { apiRequest } from './client';
import type { MetricsStatus, PromQLResult, PromQLSeries } from '@/types/metrics';

export type { MetricsStatus, PromQLResult, PromQLSeries };

export async function getMetricsStatus(): Promise<MetricsStatus> {
  return apiRequest<MetricsStatus>('/metrics/status');
}

export async function queryInstant(query: string, time?: number): Promise<PromQLResult> {
  const params = new URLSearchParams({ query });
  if (time) params.set('time', String(time));
  return apiRequest<PromQLResult>(`/metrics/query?${params}`);
}

export async function queryRange(
  query: string,
  start: number,
  end: number,
  step?: string,
): Promise<PromQLResult> {
  const params = new URLSearchParams({
    query,
    start: String(start),
    end: String(end),
  });
  if (step) params.set('step', step);
  return apiRequest<PromQLResult>(`/metrics/query_range?${params}`);
}

export type VMMetric =
  | 'cpu' | 'memory_used' | 'memory_total' | 'disk_read' | 'disk_write' | 'net_rx' | 'net_tx';
export type TopMetric = 'cpu' | 'memory';
export type ClusterMetric = 'cpu' | 'memory_used' | 'memory_total';

// The backend builds these queries with the caller's namespaces; raw PromQL is admin-only.
export async function queryVMRange(
  namespace: string,
  name: string,
  metric: VMMetric,
  start: number,
  end: number,
): Promise<PromQLResult> {
  const params = new URLSearchParams({ metric, start: String(start), end: String(end) });
  return apiRequest<PromQLResult>(
    `/metrics/vms/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}?${params}`,
  );
}

export async function queryTopVMs(metric: TopMetric): Promise<PromQLResult> {
  return apiRequest<PromQLResult>(`/metrics/top?${new URLSearchParams({ metric })}`);
}

export async function queryClusterRange(
  metric: ClusterMetric,
  start: number,
  end: number,
): Promise<PromQLResult> {
  const params = new URLSearchParams({ metric, start: String(start), end: String(end) });
  return apiRequest<PromQLResult>(`/metrics/cluster?${params}`);
}
