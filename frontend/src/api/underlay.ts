/**
 * VPC underlay fabric API.
 * Matches backend app/api/v1/vpc_underlay.py.
 */

import { apiRequest } from './client';

export interface UnderlayObject {
  kind: string;
  name: string;
  namespace: string;
  /** created | exists | missing | failed | skipped */
  state: string;
  detail: string;
  workaround: boolean;
}

export interface UnderlayStatus {
  objects: UnderlayObject[];
  ready: boolean;
  detail: string;
}

export interface EnsureUnderlayRequest {
  interface: string;
  external_cidr: string;
  external_gateway: string;
  vlan_id: number;
  exclude_nodes: string[];
  exclude_ips: string[];
  provider_network_name: string;
  vlan_name: string;
  subnet_name: string;
  link_watcher: boolean;
  cilium_source_ip_exempt: boolean | null;
  cilium_namespace: string;
}

/**
 * The object names are part of the query, not just the payload: the backend
 * looks up exactly the names it is asked about, so a cluster whose fabric was
 * built under different names reads as "missing" unless they are passed on.
 */
export async function getUnderlay(names?: {
  provider_network_name?: string;
  vlan_name?: string;
  subnet_name?: string;
}): Promise<UnderlayStatus> {
  const qs = new URLSearchParams();
  if (names?.provider_network_name) qs.set('provider_network_name', names.provider_network_name);
  if (names?.vlan_name) qs.set('vlan_name', names.vlan_name);
  if (names?.subnet_name) qs.set('subnet_name', names.subnet_name);
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return apiRequest<UnderlayStatus>(`/network/underlay${suffix}`);
}

export async function ensureUnderlay(request: EnsureUnderlayRequest): Promise<UnderlayStatus> {
  return apiRequest<UnderlayStatus>('/network/underlay', { method: 'POST', body: request });
}
