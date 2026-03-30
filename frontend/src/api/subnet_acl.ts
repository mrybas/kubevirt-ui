import { apiRequest } from './client';
import type {
  SubnetAclListResponse,
  SubnetAclUpdateRequest,
  SubnetAclAddRequest,
  AclPresetTemplate,
} from '../types/subnet_acl';

const BASE = '/subnets';

export async function getSubnetAcls(subnetName: string): Promise<SubnetAclListResponse> {
  return apiRequest<SubnetAclListResponse>(`${BASE}/${subnetName}/acls`);
}

export async function replaceSubnetAcls(
  subnetName: string,
  data: SubnetAclUpdateRequest,
): Promise<SubnetAclListResponse> {
  return apiRequest<SubnetAclListResponse>(`${BASE}/${subnetName}/acls`, {
    method: 'PUT',
    body: data,
  });
}

export async function addSubnetAcl(
  subnetName: string,
  data: SubnetAclAddRequest,
): Promise<SubnetAclListResponse> {
  return apiRequest<SubnetAclListResponse>(`${BASE}/${subnetName}/acls`, {
    method: 'POST',
    body: data,
  });
}

export async function deleteSubnetAcl(subnetName: string, index: number): Promise<void> {
  await apiRequest<{ status: string }>(`${BASE}/${subnetName}/acls/${index}`, {
    method: 'DELETE',
  });
}

export async function getAclPresets(subnetName: string): Promise<AclPresetTemplate[]> {
  return apiRequest<AclPresetTemplate[]>(`${BASE}/${subnetName}/acls/presets`);
}
