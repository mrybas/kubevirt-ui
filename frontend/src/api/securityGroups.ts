/**
 * SecurityGroups API
 */

import { apiRequest } from './client';
import type {
  SecurityGroup,
  SecurityGroupListResponse,
  CreateSecurityGroupRequest,
  UpdateSecurityGroupRequest,
  VmSecurityGroupsResponse,
  AssignSecurityGroupRequest,
} from '../types/vpc';

export async function listSecurityGroups(): Promise<SecurityGroupListResponse> {
  return apiRequest<SecurityGroupListResponse>('/security-groups');
}

export async function getSecurityGroup(name: string): Promise<SecurityGroup> {
  return apiRequest<SecurityGroup>(`/security-groups/${name}`);
}

export async function createSecurityGroup(request: CreateSecurityGroupRequest): Promise<SecurityGroup> {
  return apiRequest<SecurityGroup>('/security-groups', { method: 'POST', body: request });
}

export async function updateSecurityGroup(name: string, request: UpdateSecurityGroupRequest): Promise<SecurityGroup> {
  return apiRequest<SecurityGroup>(`/security-groups/${name}`, { method: 'PUT', body: request });
}

export async function deleteSecurityGroup(name: string): Promise<void> {
  await apiRequest<void>(`/security-groups/${name}`, { method: 'DELETE' });
}

export async function getVmSecurityGroups(namespace: string, vm: string): Promise<VmSecurityGroupsResponse> {
  return apiRequest<VmSecurityGroupsResponse>(`/security-groups/vms/${namespace}/${vm}`);
}

export async function assignSecurityGroupToVm(
  namespace: string,
  vm: string,
  request: AssignSecurityGroupRequest
): Promise<void> {
  await apiRequest<void>(`/security-groups/vms/${namespace}/${vm}`, { method: 'POST', body: request });
}

export async function removeSecurityGroupFromVm(
  namespace: string,
  vm: string,
  sg: string
): Promise<void> {
  await apiRequest<void>(`/security-groups/vms/${namespace}/${vm}/${sg}`, { method: 'DELETE' });
}
