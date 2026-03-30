/**
 * Projects hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listProjects,
  getProject,
  createProject,
  updateProject,
  deleteProject,
  addEnvironment,
  removeEnvironment,
  listProjectAccess,
  addProjectAccess,
  removeProjectAccess,
  listTeams,
} from '../api/projects';
import type { CreateProjectRequest, UpdateProjectRequest, AddEnvironmentRequest, AddAccessRequest } from '../types/project';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: listProjects,
    refetchInterval: 30000,
  });
}

export function useProject(name: string | undefined) {
  return useQuery({
    queryKey: ['projects', name],
    queryFn: () => getProject(name!),
    enabled: !!name,
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: CreateProjectRequest) => createProject(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useUpdateProject() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ name, request }: { name: string; request: UpdateProjectRequest }) =>
      updateProject(name, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteProject(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

// Environment hooks
export function useAddEnvironment(projectName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: AddEnvironmentRequest) => addEnvironment(projectName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useRemoveEnvironment(projectName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (environment: string) => removeEnvironment(projectName, environment),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

// Access hooks
export function useProjectAccess(projectName: string | undefined) {
  return useQuery({
    queryKey: ['projects', projectName, 'access'],
    queryFn: () => listProjectAccess(projectName!),
    enabled: !!projectName,
  });
}

export function useAddProjectAccess(projectName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: AddAccessRequest) => addProjectAccess(projectName, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects', projectName, 'access'] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useRemoveProjectAccess(projectName: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (bindingId: string) => removeProjectAccess(projectName, bindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects', projectName, 'access'] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useTeams() {
  return useQuery({
    queryKey: ['teams'],
    queryFn: listTeams,
  });
}
