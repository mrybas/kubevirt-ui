/**
 * Folders hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listFoldersTree,
  listFoldersFlat,
  getFolder,
  createFolder,
  updateFolder,
  deleteFolder,
  moveFolder,
  addFolderEnvironment,
  removeFolderEnvironment,
  listFolderAccess,
  addFolderAccess,
  removeFolderAccess,
} from '../api/folders';
import type {
  CreateFolderRequest,
  UpdateFolderRequest,
  MoveFolderRequest,
  AddFolderEnvironmentRequest,
  AddFolderAccessRequest,
} from '../types/folder';

export function useFoldersTree() {
  return useQuery({
    queryKey: ['folders', 'tree'],
    queryFn: listFoldersTree,
    refetchInterval: 30000,
  });
}

export function useFoldersFlat() {
  return useQuery({
    queryKey: ['folders', 'flat'],
    queryFn: listFoldersFlat,
  });
}

export function useFolder(name: string | undefined) {
  return useQuery({
    queryKey: ['folders', name],
    queryFn: () => getFolder(name!),
    enabled: !!name,
  });
}

export function useCreateFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateFolderRequest) => createFolder(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useUpdateFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, request }: { name: string; request: UpdateFolderRequest }) =>
      updateFolder(name, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useDeleteFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteFolder(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useMoveFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, request }: { name: string; request: MoveFolderRequest }) =>
      moveFolder(name, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useAddFolderEnvironment(folderName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AddFolderEnvironmentRequest) =>
      addFolderEnvironment(folderName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useRemoveFolderEnvironment(folderName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (environment: string) => removeFolderEnvironment(folderName, environment),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useFolderAccess(folderName: string | undefined) {
  return useQuery({
    queryKey: ['folders', folderName, 'access'],
    queryFn: () => listFolderAccess(folderName!),
    enabled: !!folderName,
  });
}

export function useAddFolderAccess(folderName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AddFolderAccessRequest) => addFolderAccess(folderName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', folderName, 'access'] });
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}

export function useRemoveFolderAccess(folderName: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (bindingId: string) => removeFolderAccess(folderName, bindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', folderName, 'access'] });
      queryClient.invalidateQueries({ queryKey: ['folders'] });
    },
  });
}
