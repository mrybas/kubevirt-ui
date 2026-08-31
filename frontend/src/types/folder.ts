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
  /** What the quota already has spoken for — the floor under shrinking it. */
  used_cpu?: string | null;
  used_memory?: string | null;
  used_storage?: string | null;
}

export interface Folder {
  name: string;
  display_name: string;
  description: string;
  parent_id: string | null;
  created_by: string | null;
  created_at: string | null;
  quota: FolderQuota | null;
  /** What this folder's subtree already holds; the floor under its quota. */
  allocated?: { cpu?: string; memory?: string; storage?: string } | null;
  path: string[];           // Ancestor chain from root (not including self)
  children: Folder[];       // Nested children (populated in tree mode)
  environments: FolderEnvironment[];
  total_vms: number;
  total_storage: string | null;
  teams: string[];
  users: string[];
  /**
   * Phase 2: LDAP-group-based access block.
   * null  → legacy folder (no access block) — only global admins can act on it.
   * non-null → access configured; use for Access tab.
   */
  access: AccessBlock | null;
  /**
   * Whether *this* user may create something here. Decided by the backend
   * with the predicates that enforce it, so a create button is rendered from
   * a fact rather than from the access rules re-derived in TypeScript.
   */
  can_create?: boolean;
  /** May this caller create a TENANT here — a narrower right than can_create,
   *  answered by the backend predicate that enforces it. */
  can_create_tenant?: boolean;
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
  /** Per-environment quota, keyed by the short environment name. */
  environment_quotas?: Record<string, FolderQuota>;
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
  /** Sibling quotas to shrink first, applied in the same request. */
  reallocate?: import('../api/folders').QuotaReallocation[];
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

// ---------------------------------------------------------------------------
// Phase 2: LDAP-group-based access block (stored in folder ConfigMap)
// ---------------------------------------------------------------------------

/** Role lists for one scope (folder-level or one specific env). */
export interface EnvAccess {
  admins: string[];
  members: string[];
  viewers: string[];
}

/**
 * Full access block returned by GET /folders / GET /folders/{name}.
 * Folder-level lists apply to ALL envs; env_access[env] is additive.
 * null on legacy folders that predate Phase 2 (global admins only).
 */
export interface AccessBlock extends EnvAccess {
  /** Env-specific overrides; always present (may be empty object) when access != null */
  env_access: Record<string, EnvAccess>;
}

/**
 * Partial request body for PATCH /folders/{name}/access.
 * Omitting a field keeps the current value. Send [] to clear a list.
 */
export interface PatchFolderAccessRequest {
  admins?: string[];
  members?: string[];
  viewers?: string[];
  /** Full replace of env_access when provided; omit to keep current */
  env_access?: Record<string, EnvAccess>;
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
