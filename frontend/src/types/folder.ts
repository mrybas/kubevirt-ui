/**
 * Folder Types
 *
 * Folder = hierarchical grouping (ConfigMap-based, replaces flat Projects)
 * Environment = K8s namespace belonging to a folder
 */

export interface FolderQuota {
  cpu?: string | null;
  memory?: string | null;
  storage?: string | null;
}

export interface FolderEnvironment {
  name: string;          // Full namespace name: {folder}-{environment}
  environment: string;   // Short name: dev, staging, prod
  folder: string;        // Parent folder name
  created: string | null;
  vm_count: number;
  storage_used: string | null;
  quota_cpu: string | null;
  quota_memory: string | null;
  quota_storage: string | null;
}

export interface Folder {
  name: string;
  display_name: string;
  description: string;
  parent_id: string | null;
  created_by: string | null;
  created_at: string | null;
  quota: FolderQuota | null;
  path: string[];           // Ancestor chain from root (not including self)
  children: Folder[];       // Nested children (populated in tree mode)
  environments: FolderEnvironment[];
  total_vms: number;
  total_storage: string | null;
  teams: string[];
  users: string[];
}

export interface FolderTreeResponse {
  items: Folder[];
  total: number;
}

export interface FolderListResponse {
  items: Folder[];
  total: number;
}

export interface CreateFolderRequest {
  name: string;
  display_name: string;
  description?: string;
  parent_id?: string | null;
  environments?: string[];
  quota?: FolderQuota;
}

export interface UpdateFolderRequest {
  display_name?: string;
  description?: string;
  quota?: FolderQuota;
}

export interface MoveFolderRequest {
  new_parent_id: string | null;
}

export interface AddFolderEnvironmentRequest {
  environment: string;
  quota_cpu?: string;
  quota_memory?: string;
  quota_storage?: string;
}

export interface FolderAccessEntry {
  id: string;
  type: 'team' | 'user';
  name: string;
  role: 'admin' | 'editor' | 'viewer';
  scope: 'folder' | 'environment';
  environment: string | null;
  folder: string | null;
  inherited: boolean;
  created: string | null;
}

export interface FolderAccessListResponse {
  items: FolderAccessEntry[];
  total: number;
}

export interface AddFolderAccessRequest {
  type: 'team' | 'user';
  name: string;
  role?: 'admin' | 'editor' | 'viewer';
  scope?: 'folder' | 'environment';
  environment?: string;
}

export type FolderRole = 'admin' | 'editor' | 'viewer';

export const FOLDER_ROLE_LABELS: Record<FolderRole, string> = {
  admin: 'Admin',
  editor: 'Editor',
  viewer: 'Viewer',
};

export const FOLDER_ROLE_DESCRIPTIONS: Record<FolderRole, string> = {
  admin: 'Full access + manage folder access',
  editor: 'Create, edit, delete VMs and storage',
  viewer: 'Read-only access',
};
