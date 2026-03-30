/**
 * VPCs API
 */

import { apiRequest } from './client';
import type {
  Vpc,
  VpcListResponse,
  CreateVpcRequest,
  AddVpcPeeringRequest,
  VpcRoutesResponse,
  UpdateVpcRoutesRequest,
} from '../types/vpc';

export async function listVpcs(): Promise<VpcListResponse> {
  return apiRequest<VpcListResponse>('/vpcs');
}

export async function getVpc(name: string): Promise<Vpc> {
  return apiRequest<Vpc>(`/vpcs/${name}`);
}

export async function createVpc(request: CreateVpcRequest): Promise<Vpc> {
  return apiRequest<Vpc>('/vpcs', { method: 'POST', body: request });
}

export async function deleteVpc(name: string): Promise<void> {
  await apiRequest<void>(`/vpcs/${name}`, { method: 'DELETE' });
}

export async function addVpcPeering(name: string, request: AddVpcPeeringRequest): Promise<Vpc> {
  return apiRequest<Vpc>(`/vpcs/${name}/peerings`, { method: 'POST', body: request });
}

export async function removeVpcPeering(name: string, remoteVpc: string): Promise<void> {
  await apiRequest<void>(`/vpcs/${name}/peerings/${remoteVpc}`, { method: 'DELETE' });
}

export async function getVpcRoutes(name: string): Promise<VpcRoutesResponse> {
  return apiRequest<VpcRoutesResponse>(`/vpcs/${name}/routes`);
}

export async function updateVpcRoutes(name: string, request: UpdateVpcRoutesRequest): Promise<VpcRoutesResponse> {
  return apiRequest<VpcRoutesResponse>(`/vpcs/${name}/routes`, { method: 'PUT', body: request });
}
