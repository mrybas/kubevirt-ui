/**
 * Projects API
 */

import { apiRequest } from './client';
import type {
  Project,
  ProjectListResponse,
  CreateProjectRequest,
  UpdateProjectRequest,
  Environment,
  AddEnvironmentRequest,
  AccessEntry,
  AccessListResponse,
  AddAccessRequest,
  TeamListResponse,
} from '../types/project';

// Projects
export async function listProjects(): Promise<ProjectListResponse> {
  return apiRequest<ProjectListResponse>('/projects');
}

export async function getProject(name: string): Promise<Project> {
  return apiRequest<Project>(`/projects/${name}`);
}

export async function createProject(request: CreateProjectRequest): Promise<Project> {
  return apiRequest<Project>('/projects', {
    method: 'POST',
    body: request,
  });
}

export async function updateProject(name: string, request: UpdateProjectRequest): Promise<Project> {
  return apiRequest<Project>(`/projects/${name}`, {
    method: 'PATCH',
    body: request,
  });
}

export async function deleteProject(name: string): Promise<void> {
  await apiRequest<void>(`/projects/${name}`, {
    method: 'DELETE',
  });
}

// Environments
export async function addEnvironment(
  projectName: string,
  request: AddEnvironmentRequest
): Promise<Environment> {
  return apiRequest<Environment>(`/projects/${projectName}/environments`, {
    method: 'POST',
    body: request,
  });
}

export async function removeEnvironment(
  projectName: string,
  environment: string
): Promise<void> {
  await apiRequest<void>(`/projects/${projectName}/environments/${environment}`, {
    method: 'DELETE',
  });
}

// Project Access
export async function listProjectAccess(projectName: string): Promise<AccessListResponse> {
  return apiRequest<AccessListResponse>(`/projects/${projectName}/access`);
}

export async function addProjectAccess(
  projectName: string,
  request: AddAccessRequest
): Promise<AccessEntry> {
  return apiRequest<AccessEntry>(`/projects/${projectName}/access`, {
    method: 'POST',
    body: request,
  });
}

export async function removeProjectAccess(
  projectName: string,
  bindingId: string
): Promise<void> {
  await apiRequest<void>(`/projects/${projectName}/access/${bindingId}`, {
    method: 'DELETE',
  });
}

// Teams
export async function listTeams(): Promise<TeamListResponse> {
  return apiRequest<TeamListResponse>('/teams');
}
