/**
 * React Query hooks for templates and golden images
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import * as templatesApi from '@/api/templates';
import type {

  VMTemplateCreate,

  GoldenImageCreate,
  GoldenImageUpdate,
  CreateImageFromDiskRequest,

  PersistentDiskCreate,
  AttachDiskRequest,
  VMFromTemplateRequest,
} from '@/types/template';

// =============================================================================
// VM Templates
// =============================================================================

export function useTemplates() {
  return useQuery({
    queryKey: ['templates'],
    queryFn: templatesApi.listTemplates,
  });
}

export function useTemplate(name: string) {
  return useQuery({
    queryKey: ['template', name],
    queryFn: () => templatesApi.getTemplate(name),
    enabled: !!name,
  });
}

export function useCreateTemplate() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: VMTemplateCreate) => templatesApi.createTemplate(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['templates'] });
    },
  });
}

export function useUpdateTemplate() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, data }: { name: string; data: VMTemplateCreate }) => 
      templatesApi.updateTemplate(name, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['templates'] });
    },
  });
}

export function useDeleteTemplate() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (name: string) => templatesApi.deleteTemplate(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['templates'] });
    },
  });
}

// =============================================================================
// Golden Images
// =============================================================================

export function useImages(namespace?: string) {
  return useQuery({
    queryKey: ['images', namespace],
    queryFn: () => templatesApi.listImages(namespace),
  });
}

export function useCreateImage() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, namespace }: { data: GoldenImageCreate; namespace: string }) => 
      templatesApi.createImage(data, namespace),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['images'] });
    },
  });
}

export function useDeleteImage() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, namespace }: { name: string; namespace: string }) => 
      templatesApi.deleteImage(name, namespace),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['images'] });
    },
  });
}

export function useUpdateImage() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, namespace, data }: { name: string; namespace: string; data: GoldenImageUpdate }) =>
      templatesApi.updateImage(name, namespace, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['images'] });
    },
  });
}

export function useCreateImageFromDisk() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: CreateImageFromDiskRequest) =>
      templatesApi.createImageFromDisk(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['images'] });
    },
  });
}

// Aliases for backward compatibility
export const useGoldenImages = useImages;
export const useCreateGoldenImage = useCreateImage;
export const useDeleteGoldenImage = useDeleteImage;
export const useUpdateGoldenImage = useUpdateImage;
export const useCreateGoldenImageFromDisk = useCreateImageFromDisk;

// =============================================================================
// Persistent Disks
// =============================================================================

export function usePersistentDisks(namespace: string) {
  return useQuery({
    queryKey: ['persistent-disks', namespace],
    queryFn: () => templatesApi.listPersistentDisks(namespace),
    enabled: !!namespace,
  });
}

export function useCreatePersistentDisk() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ namespace, data }: { namespace: string; data: PersistentDiskCreate }) =>
      templatesApi.createPersistentDisk(namespace, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['persistent-disks', variables.namespace] });
    },
  });
}

export function useDeletePersistentDisk() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      templatesApi.deletePersistentDisk(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['persistent-disks', variables.namespace] });
    },
  });
}

export function useAttachTemplateDisk() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      diskName,
      data,
    }: {
      namespace: string;
      diskName: string;
      data: AttachDiskRequest;
    }) => templatesApi.attachDisk(namespace, diskName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['persistent-disks', variables.namespace] });
      queryClient.invalidateQueries({ queryKey: ['vms', variables.namespace] });
    },
  });
}

export function useDetachTemplateDisk() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, diskName }: { namespace: string; diskName: string }) =>
      templatesApi.detachDisk(namespace, diskName),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['persistent-disks', variables.namespace] });
      queryClient.invalidateQueries({ queryKey: ['vms', variables.namespace] });
    },
  });
}

// =============================================================================
// VM from Template
// =============================================================================

export function useCreateVMFromTemplate() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ namespace, data }: { namespace: string; data: VMFromTemplateRequest }) =>
      templatesApi.createVMFromTemplate(namespace, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms', variables.namespace] });
    },
  });
}

export function useCreateImageFromVM() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      imageName,
      displayName,
      description,
    }: {
      namespace: string;
      vmName: string;
      imageName: string;
      displayName?: string;
      description?: string;
    }) => templatesApi.createImageFromVM(namespace, vmName, imageName, displayName, description),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['images'] });
    },
  });
}
