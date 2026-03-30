import { apiRequest } from './client';
import type {
  AddonCatalog,
  DiscoveryResponse,
  Tenant,
  TenantAddon,
  TenantAddonStatus,
  TenantCreateRequest,
  TenantKubeconfigResponse,
  TenantListResponse,
  TenantScaleRequest,
} from '@/types/tenant';
import type { GoldenImageListResponse } from '@/types/template';

export async function listTenants(): Promise<TenantListResponse> {
  return apiRequest<TenantListResponse>('/tenants');
}

export async function getTenant(name: string): Promise<Tenant> {
  return apiRequest<Tenant>(`/tenants/${name}`);
}

export async function createTenant(request: TenantCreateRequest): Promise<Tenant> {
  return apiRequest<Tenant>('/tenants', {
    method: 'POST',
    body: request,
  });
}

export async function deleteTenant(name: string): Promise<void> {
  await apiRequest<void>(`/tenants/${name}`, { method: 'DELETE' });
}

export async function scaleTenant(name: string, request: TenantScaleRequest): Promise<Tenant> {
  return apiRequest<Tenant>(`/tenants/${name}/scale`, {
    method: 'POST',
    body: request,
  });
}

export async function getTenantKubeconfig(
  name: string,
  type: 'admin' | 'oidc' = 'admin',
): Promise<TenantKubeconfigResponse> {
  return apiRequest<TenantKubeconfigResponse>(`/tenants/${name}/kubeconfig?type=${type}`);
}

export async function getAddonCatalog(): Promise<AddonCatalog> {
  return apiRequest<AddonCatalog>('/tenants/catalog');
}

export async function getDiscovery(): Promise<DiscoveryResponse> {
  return apiRequest<DiscoveryResponse>('/tenants/discovery');
}

export async function enableAddon(tenantName: string, addon: TenantAddon): Promise<TenantAddonStatus> {
  return apiRequest<TenantAddonStatus>(`/tenants/${tenantName}/addons`, {
    method: 'POST',
    body: addon,
  });
}

export async function disableAddon(tenantName: string, addonId: string): Promise<void> {
  await apiRequest<void>(`/tenants/${tenantName}/addons/${addonId}`, {
    method: 'DELETE',
  });
}

export async function updateAddonParams(
  tenantName: string,
  addonId: string,
  params: Record<string, string>,
): Promise<TenantAddonStatus> {
  return apiRequest<TenantAddonStatus>(`/tenants/${tenantName}/addons/${addonId}`, {
    method: 'PATCH',
    body: params,
  });
}

export async function listTenantImages(tenantName: string): Promise<GoldenImageListResponse> {
  return apiRequest<GoldenImageListResponse>(`/tenants/${tenantName}/images`);
}

export async function deleteTenantImage(tenantName: string, imageName: string): Promise<void> {
  await apiRequest<void>(`/tenants/${tenantName}/images/${imageName}`, { method: 'DELETE' });
}
