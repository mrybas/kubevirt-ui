import { apiRequest } from './client';
import type { HubbleFlowsResponse, HubbleStatusResponse, HubbleFlowsParams } from '../types/hubble';

const BASE = '/hubble';

export async function getHubbleFlows(params: HubbleFlowsParams = {}): Promise<HubbleFlowsResponse> {
  const query = new URLSearchParams();
  if (params.namespace) query.set('namespace', params.namespace);
  if (params.pod) query.set('pod', params.pod);
  if (params.verdict) query.set('verdict', params.verdict);
  if (params.protocol) query.set('protocol', params.protocol);
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.since) query.set('since', params.since);

  const qs = query.toString();
  return apiRequest<HubbleFlowsResponse>(`${BASE}/flows${qs ? `?${qs}` : ''}`);
}

export async function getHubbleStatus(): Promise<HubbleStatusResponse> {
  return apiRequest<HubbleStatusResponse>(`${BASE}/status`);
}
