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
