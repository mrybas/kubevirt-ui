/**
 * Golden Images API
 *
 * Split out of templates.ts, unchanged, to match the backend's
 * `app/api/v1/images.py` (moved out of `templates.py` in an earlier task) —
 * images are their own domain, not a template concern.
 */

import { apiRequest } from './client';
import type {
  GoldenImage,
  GoldenImageCreate,
  GoldenImageUpdate,
  GoldenImageListResponse,
  CreateImageFromDiskRequest,
} from '../types/template';

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
