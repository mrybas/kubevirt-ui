/**
 * Project and Access Types
 *
 * Project = logical grouping (ConfigMap-based, no own namespace)
 * Environment = K8s namespace belonging to a project
 */

export interface Environment {
  name: string;           // Full namespace name: {project}-{environment}
  environment: string;    // Short name: dev, staging, prod
  project: string;        // Parent project name
  created: string | null;
  vm_count: number;
  storage_used: string | null;
  quota_cpu: string | null;
  quota_memory: string | null;
  quota_storage: string | null;
}

export interface ProjectQuota {
  cpu?: string | null;
  memory?: string | null;
  storage?: string | null;
}

export interface Project {
  name: string;
  display_name: string;
  description: string;
  created_by: string | null;
  quota: ProjectQuota | null;
  environments: Environment[];
  total_vms: number;
  total_storage: string | null;
  teams: string[];
  users: string[];
}

export interface ProjectListResponse {
  items: Project[];
  total: number;
}

export interface CreateProjectRequest {
  name: string;
  display_name: string;
  description?: string;
  environments?: string[];  // e.g. ["dev", "staging", "prod"]
  quota?: ProjectQuota;
}

export interface UpdateProjectRequest {
  display_name?: string;
  description?: string;
  quota?: ProjectQuota;
}

export interface AddEnvironmentRequest {
  environment: string;
  quota_cpu?: string;
  quota_memory?: string;
  quota_storage?: string;
}

export interface AccessEntry {
  id: string;
  type: 'team' | 'user';
  name: string;
  role: 'admin' | 'editor' | 'viewer';
  scope: 'project' | 'environment';
  environment: string | null;
  created: string | null;
}

export interface AccessListResponse {
  items: AccessEntry[];
  total: number;
}

export interface AddAccessRequest {
  type: 'team' | 'user';
  name: string;
  role: 'admin' | 'editor' | 'viewer';
  scope?: 'project' | 'environment';
  environment?: string;
}

export interface Team {
  name: string;
  display_name: string;
  description: string;
}

export interface TeamListResponse {
  items: Team[];
  total: number;
}

export type Role = 'admin' | 'editor' | 'viewer';

export const ROLE_LABELS: Record<Role, string> = {
  admin: 'Admin',
  editor: 'Editor',
  viewer: 'Viewer',
};

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  admin: 'Full access + manage project access',
  editor: 'Create, edit, delete VMs and storage',
  viewer: 'Read-only access',
};
