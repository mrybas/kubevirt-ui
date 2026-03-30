/**
 * Tenants hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listTenants,
  getTenant,
  createTenant,
  deleteTenant,
  scaleTenant,
  getTenantKubeconfig,
  getAddonCatalog,
  getDiscovery,
  enableAddon,
  disableAddon,
  updateAddonParams,
  listTenantImages,
  deleteTenantImage,
} from '../api/tenants';
import type { TenantCreateRequest, TenantScaleRequest, TenantAddon } from '../types/tenant';

export function useTenants() {
  return useQuery({
    queryKey: ['tenants'],
    queryFn: listTenants,
    refetchInterval: 15000,
  });
}

export function useTenant(name: string | undefined) {
  return useQuery({
    queryKey: ['tenants', name],
    queryFn: () => getTenant(name!),
    enabled: !!name,
    refetchInterval: 10000,
  });
}

export function useCreateTenant() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: TenantCreateRequest) => createTenant(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
    },
  });
}

export function useDeleteTenant() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteTenant(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
    },
  });
}

export function useScaleTenant() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ name, request }: { name: string; request: TenantScaleRequest }) =>
      scaleTenant(name, request),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['tenants', variables.name] });
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
    },
  });
}

export function useTenantKubeconfig(name: string | undefined, type: 'admin' | 'oidc' = 'admin') {
  return useQuery({
    queryKey: ['tenants', name, 'kubeconfig', type],
    queryFn: () => getTenantKubeconfig(name!, type),
    enabled: false, // manual fetch only
  });
}

export function useAddonCatalog() {
  return useQuery({
    queryKey: ['addon-catalog'],
    queryFn: getAddonCatalog,
    staleTime: 5 * 60 * 1000, // 5 min
  });
}

export function useDiscovery() {
  return useQuery({
    queryKey: ['discovery'],
    queryFn: getDiscovery,
    staleTime: 5 * 60 * 1000, // 5 min
  });
}

export function useEnableAddon(tenantName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (addon: TenantAddon) => enableAddon(tenantName, addon),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants', tenantName] });
    },
  });
}

export function useDisableAddon(tenantName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (addonId: string) => disableAddon(tenantName, addonId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants', tenantName] });
    },
  });
}

export function useUpdateAddonParams(tenantName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ addonId, params }: { addonId: string; params: Record<string, string> }) =>
      updateAddonParams(tenantName, addonId, params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants', tenantName] });
    },
  });
}

export function useTenantImages(tenantName: string | undefined) {
  return useQuery({
    queryKey: ['tenants', tenantName, 'images'],
    queryFn: () => listTenantImages(tenantName!),
    enabled: !!tenantName,
    refetchInterval: 15000,
  });
}

export function useDeleteTenantImage(tenantName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (imageName: string) => deleteTenantImage(tenantName, imageName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants', tenantName, 'images'] });
    },
  });
}
