import { apiRequest } from './client';
import type {
  CiliumPolicyCreateRequest,
  CiliumPolicyResponse,
  CiliumPolicyListResponse,
} from '../types/cilium_policy';

const BASE = '/cilium-policies';

export async function listCiliumPolicies(namespace?: string): Promise<CiliumPolicyListResponse> {
  const qs = namespace ? `?namespace=${encodeURIComponent(namespace)}` : '';
  return apiRequest<CiliumPolicyListResponse>(`${BASE}${qs}`);
}

export async function getCiliumPolicy(
  namespace: string,
  name: string,
): Promise<CiliumPolicyResponse> {
  return apiRequest<CiliumPolicyResponse>(`${BASE}/${namespace}/${name}`);
}

export async function createCiliumPolicy(
  data: CiliumPolicyCreateRequest,
): Promise<CiliumPolicyResponse> {
  return apiRequest<CiliumPolicyResponse>(BASE, {
    method: 'POST',
    body: data,
  });
}

export async function deleteCiliumPolicy(namespace: string, name: string): Promise<void> {
  await apiRequest<{ status: string }>(`${BASE}/${namespace}/${name}`, {
    method: 'DELETE',
  });
}
