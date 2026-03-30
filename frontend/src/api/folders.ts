/**
 * Folders API
 */

import { apiRequest } from './client';
import type {
  Folder,
  FolderTreeResponse,
  FolderListResponse,
  CreateFolderRequest,
  UpdateFolderRequest,
  MoveFolderRequest,
  FolderEnvironment,
  AddFolderEnvironmentRequest,
  FolderAccessEntry,
  FolderAccessListResponse,
  AddFolderAccessRequest,
} from '../types/folder';

// Folders
export async function listFoldersTree(): Promise<FolderTreeResponse> {
  return apiRequest<FolderTreeResponse>('/folders');
}

export async function listFoldersFlat(): Promise<FolderListResponse> {
  return apiRequest<FolderListResponse>('/folders?flat=true');
}

export async function getFolder(name: string): Promise<Folder> {
  return apiRequest<Folder>(`/folders/${name}`);
}

export async function createFolder(request: CreateFolderRequest): Promise<Folder> {
  return apiRequest<Folder>('/folders', {
    method: 'POST',
    body: request,
  });
}

export async function updateFolder(name: string, request: UpdateFolderRequest): Promise<Folder> {
  return apiRequest<Folder>(`/folders/${name}`, {
    method: 'PATCH',
    body: request,
  });
}

export async function deleteFolder(name: string): Promise<void> {
  await apiRequest<void>(`/folders/${name}?cascade=true`, {
    method: 'DELETE',
  });
}

export async function moveFolder(name: string, request: MoveFolderRequest): Promise<Folder> {
  return apiRequest<Folder>(`/folders/${name}/move`, {
    method: 'POST',
    body: request,
  });
}

// Environments
export async function addFolderEnvironment(
  folderName: string,
  request: AddFolderEnvironmentRequest
): Promise<FolderEnvironment> {
  return apiRequest<FolderEnvironment>(`/folders/${folderName}/environments`, {
    method: 'POST',
    body: request,
  });
}

export async function removeFolderEnvironment(
  folderName: string,
  environment: string
): Promise<void> {
  await apiRequest<void>(`/folders/${folderName}/environments/${environment}`, {
    method: 'DELETE',
  });
}

// Access
export async function listFolderAccess(folderName: string): Promise<FolderAccessListResponse> {
  return apiRequest<FolderAccessListResponse>(`/folders/${folderName}/access`);
}

export async function addFolderAccess(
  folderName: string,
  request: AddFolderAccessRequest
): Promise<FolderAccessEntry> {
  return apiRequest<FolderAccessEntry>(`/folders/${folderName}/access`, {
    method: 'POST',
    body: request,
  });
}

export async function removeFolderAccess(
  folderName: string,
  bindingId: string
): Promise<void> {
  await apiRequest<void>(`/folders/${folderName}/access/${bindingId}`, {
    method: 'DELETE',
  });
}
