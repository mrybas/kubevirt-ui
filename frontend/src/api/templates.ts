/**
 * VM Templates and Golden Images API
 */

import { apiRequest } from './client';
import type {
  VMTemplate,
  VMTemplateCreate,
  VMTemplateListResponse,
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

// Golden Images now live in ./images (mirrors the backend's images.py split
// out of templates.py). Re-exported here so any existing import of these
// names from '@/api/templates' keeps working.
export {
  listImages,
  createImage,
  deleteImage,
  updateImage,
  createImageFromDisk,
  listGoldenImages,
  createGoldenImage,
  deleteGoldenImage,
  updateGoldenImage,
  createGoldenImageFromDisk,
} from './images';

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
