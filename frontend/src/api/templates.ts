/**
 * VM Templates and Golden Images API
 */

import { apiRequest } from './client';
import type {
  VMTemplate,
  VMTemplateCreate,
  VMTemplateListResponse,
  GoldenImage,
  GoldenImageCreate,
  GoldenImageUpdate,
  GoldenImageListResponse,
  CreateImageFromDiskRequest,
  PersistentDisk,
  PersistentDiskCreate,
  PersistentDiskListResponse,
  AttachDiskRequest,
  VMFromTemplateRequest,
} from '../types/template';
import type { VMResponse } from '../types/vm';

// =============================================================================
// VM Templates
// =============================================================================

export async function listTemplates(): Promise<VMTemplateListResponse> {
  return apiRequest<VMTemplateListResponse>('/templates');
}

export async function getTemplate(name: string): Promise<VMTemplate> {
  return apiRequest<VMTemplate>(`/templates/${name}`);
}

export async function createTemplate(data: VMTemplateCreate): Promise<VMTemplate> {
  return apiRequest<VMTemplate>('/templates', {
    method: 'POST',
    body: data,
  });
}

export async function updateTemplate(name: string, data: VMTemplateCreate): Promise<VMTemplate> {
  return apiRequest<VMTemplate>(`/templates/${name}`, {
    method: 'PUT',
    body: data,
  });
}

export async function deleteTemplate(name: string): Promise<void> {
  await apiRequest<void>(`/templates/${name}`, {
    method: 'DELETE',
  });
}

// =============================================================================
// Golden Images
// =============================================================================

export async function listImages(namespace?: string): Promise<GoldenImageListResponse> {
  const params = namespace ? `?namespace=${namespace}` : '';
  return apiRequest<GoldenImageListResponse>(`/images${params}`);
}

export async function createImage(data: GoldenImageCreate, namespace: string): Promise<GoldenImage> {
  return apiRequest<GoldenImage>(`/images?namespace=${namespace}`, {
    method: 'POST',
    body: data,
  });
}

export async function deleteImage(name: string, namespace: string): Promise<void> {
  await apiRequest<void>(`/images/${name}?namespace=${namespace}`, {
    method: 'DELETE',
  });
}

export async function updateImage(name: string, namespace: string, data: GoldenImageUpdate): Promise<GoldenImage> {
  return apiRequest<GoldenImage>(`/images/${name}?namespace=${namespace}`, {
    method: 'PATCH',
    body: data,
  });
}

export async function createImageFromDisk(
  data: CreateImageFromDiskRequest
): Promise<GoldenImage> {
  return apiRequest<GoldenImage>('/images/from-disk', {
    method: 'POST',
    body: data,
  });
}

// Aliases for backward compatibility
export const listGoldenImages = listImages;
export const createGoldenImage = createImage;
export const deleteGoldenImage = deleteImage;
export const updateGoldenImage = updateImage;
export const createGoldenImageFromDisk = createImageFromDisk;

// =============================================================================
// Persistent Disks
// =============================================================================

export async function listPersistentDisks(
  namespace: string
): Promise<PersistentDiskListResponse> {
  return apiRequest<PersistentDiskListResponse>(`/namespaces/${namespace}/disks`);
}

export async function createPersistentDisk(
  namespace: string,
  data: PersistentDiskCreate
): Promise<PersistentDisk> {
  return apiRequest<PersistentDisk>(`/namespaces/${namespace}/disks`, {
    method: 'POST',
    body: data,
  });
}

export async function deletePersistentDisk(
  namespace: string,
  name: string
): Promise<void> {
  await apiRequest<void>(`/namespaces/${namespace}/disks/${name}`, {
    method: 'DELETE',
  });
}

export async function attachDisk(
  namespace: string,
  diskName: string,
  data: AttachDiskRequest
): Promise<{ status: string; disk: string; vm: string }> {
  return apiRequest(`/namespaces/${namespace}/disks/${diskName}/attach`, {
    method: 'POST',
    body: data,
  });
}

export async function detachDisk(
  namespace: string,
  diskName: string
): Promise<{ status: string; disk: string; vm: string }> {
  return apiRequest(`/namespaces/${namespace}/disks/${diskName}/detach`, {
    method: 'POST',
  });
}

// =============================================================================
// VM from Template
// =============================================================================

export async function createVMFromTemplate(
  namespace: string,
  data: VMFromTemplateRequest
): Promise<VMResponse> {
  return apiRequest<VMResponse>(`/namespaces/${namespace}/vms/from-template`, {
    method: 'POST',
    body: data,
  });
}

export async function createImageFromVM(
  namespace: string,
  vmName: string,
  imageName: string,
  displayName?: string,
  description?: string
): Promise<{ status: string; name: string; namespace: string }> {
  const params = new URLSearchParams({ image_name: imageName });
  if (displayName) params.append('display_name', displayName);
  if (description) params.append('description', description);
  
  return apiRequest(`/namespaces/${namespace}/vms/${vmName}/create-image?${params}`, {
    method: 'POST',
  });
}
