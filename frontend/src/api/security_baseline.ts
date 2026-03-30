import { apiRequest } from './client';
import type {
  SecurityBaselineCreateRequest,
  SecurityBaselineResponse,
  SecurityBaselineListResponse,
} from '../types/cilium_policy';

const BASE = '/security-baseline';

export async function listSecurityBaselines(): Promise<SecurityBaselineListResponse> {
  return apiRequest<SecurityBaselineListResponse>(BASE);
}

export async function createSecurityBaseline(
  data: SecurityBaselineCreateRequest,
): Promise<SecurityBaselineResponse> {
  return apiRequest<SecurityBaselineResponse>(BASE, {
    method: 'POST',
    body: data,
  });
}

export async function deleteSecurityBaseline(name: string): Promise<void> {
  await apiRequest<{ status: string }>(`${BASE}/${name}`, {
    method: 'DELETE',
  });
}
