/**
 * Egress Gateway API
 * Matches backend app/api/v1/egress_gateway.py endpoints
 */

import { apiRequest } from './client';
import type {
  EgressGateway,
  EgressGatewayListResponse,
  CreateEgressGatewayRequest,
  AttachVpcRequest,
  AttachedVpcInfo,
  DetachVpcRequest,
  DetachVpcResponse,
} from '../types/egress';

export async function listEgressGateways(): Promise<EgressGatewayListResponse> {
  return apiRequest<EgressGatewayListResponse>('/egress-gateways');
}

export async function getEgressGateway(name: string): Promise<EgressGateway> {
  return apiRequest<EgressGateway>(`/egress-gateways/${name}`);
}

export async function createEgressGateway(request: CreateEgressGatewayRequest): Promise<EgressGateway> {
  return apiRequest<EgressGateway>('/egress-gateways', { method: 'POST', body: request });
}

export async function deleteEgressGateway(name: string): Promise<void> {
  await apiRequest<void>(`/egress-gateways/${name}`, { method: 'DELETE' });
}

// attach returns AttachedVpcInfo (not EgressGateway)
export async function attachVpc(name: string, request: AttachVpcRequest): Promise<AttachedVpcInfo> {
  return apiRequest<AttachedVpcInfo>(`/egress-gateways/${name}/attach`, { method: 'POST', body: request });
}

// detach returns {status, gateway, vpc}
export async function detachVpc(name: string, request: DetachVpcRequest): Promise<DetachVpcResponse> {
  return apiRequest<DetachVpcResponse>(`/egress-gateways/${name}/detach`, { method: 'POST', body: request });
}

/**
 * Free CIDRs for a new gateway, checked against every subnet in the cluster.
 * The form used to start from a constant that overlapped the control-plane
 * transit on this deployment, and the overlap check then approved its own
 * suggestion.
 */
export async function suggestGatewayCidrs(): Promise<{ gw_vpc_cidr: string; transit_cidr: string }> {
  return apiRequest<{ gw_vpc_cidr: string; transit_cidr: string }>('/egress-gateways/suggest-cidrs');
}
