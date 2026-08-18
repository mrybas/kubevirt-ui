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
  TenantStorageReconcileResponse,
  TenantStorageStatus,
  TalosVersionsResponse,
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

export async function getTenantStorageStatus(name: string): Promise<TenantStorageStatus> {
  return apiRequest<TenantStorageStatus>(`/tenants/${name}/storage/status`);
}

export async function reconcileTenantStorage(
  name: string,
): Promise<TenantStorageReconcileResponse> {
  return apiRequest<TenantStorageReconcileResponse>(`/tenants/${name}/storage/reconcile`, {
    method: 'POST',
  });
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

/** Talos releases this deployment offers, with the Kubernetes window each takes.
 *
 *  The wizard renders these and computes nothing: `is_compatible()` lives in
 *  one module on the server and is read by both the list below and the
 *  validator that accepts the create request. A client that decided
 *  compatibility for itself would be the second record for one fact — the
 *  shape that has bitten this codebase repeatedly, and which here would show
 *  as the wizard offering a pair the backend then refuses. */
export async function listTalosVersions(
  kubernetesVersion?: string,
): Promise<TalosVersionsResponse> {
  const q = kubernetesVersion
    ? `?kubernetes_version=${encodeURIComponent(kubernetesVersion)}`
    : '';
  return apiRequest<TalosVersionsResponse>(`/tenants/talos-versions${q}`);
}
