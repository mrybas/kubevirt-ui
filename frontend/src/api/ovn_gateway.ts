/**
 * OVN Gateway API
 * Matches backend app/api/v1/ovn_gateway.py endpoints
 */

import { apiRequest } from './client';
import type {
  OvnGateway,
  OvnGatewayListResponse,
  CreateOvnGatewayRequest,
  OvnDnatRuleInfo,
  OvnFipInfo,
  CreateOvnDnatRuleRequest,
  CreateOvnFipRequest,
} from '../types/ovn_gateway';

export async function listOvnGateways(): Promise<OvnGatewayListResponse> {
  return apiRequest<OvnGatewayListResponse>('/ovn-gateways');
}

export async function getOvnGateway(name: string): Promise<OvnGateway> {
  return apiRequest<OvnGateway>(`/ovn-gateways/${name}`);
}

export async function createOvnGateway(request: CreateOvnGatewayRequest): Promise<OvnGateway> {
  return apiRequest<OvnGateway>('/ovn-gateways', { method: 'POST', body: request });
}

export async function deleteOvnGateway(name: string): Promise<void> {
  await apiRequest<void>(`/ovn-gateways/${name}`, { method: 'DELETE' });
}

export async function createDnatRule(gatewayName: string, request: CreateOvnDnatRuleRequest): Promise<OvnDnatRuleInfo> {
  return apiRequest<OvnDnatRuleInfo>(`/ovn-gateways/${gatewayName}/dnat-rules`, { method: 'POST', body: request });
}

export async function deleteDnatRule(gatewayName: string, ruleName: string): Promise<void> {
  await apiRequest<void>(`/ovn-gateways/${gatewayName}/dnat-rules/${ruleName}`, { method: 'DELETE' });
}

export async function createFip(gatewayName: string, request: CreateOvnFipRequest): Promise<OvnFipInfo> {
  return apiRequest<OvnFipInfo>(`/ovn-gateways/${gatewayName}/fips`, { method: 'POST', body: request });
}

export async function deleteFip(gatewayName: string, fipName: string): Promise<void> {
  await apiRequest<void>(`/ovn-gateways/${gatewayName}/fips/${fipName}`, { method: 'DELETE' });
}
