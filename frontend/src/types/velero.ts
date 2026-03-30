/** Velero and VM Snapshot types */

export interface VeleroBackup {
  name: string;
  namespace: string;
  phase: string;
  included_namespaces: string[];
  label_selector: Record<string, string>;
  snapshot_volumes: boolean;
  ttl: string;
  started_at: string;
  completed_at: string;
  expiration: string;
  items_backed_up: number;
  total_items: number;
  errors: number;
  warnings: number;
  creation_time: string;
}

export interface VeleroSchedule {
  name: string;
  namespace: string;
  schedule: string;
  paused: boolean;
  included_namespaces: string[];
  label_selector: Record<string, string>;
  snapshot_volumes: boolean;
  ttl: string;
  phase: string;
  last_backup: string;
  creation_time: string;
}

export interface VeleroStorageLocation {
  name: string;
  namespace: string;
  provider: string;
  bucket: string;
  prefix: string;
  phase: string;
  last_synced: string;
  last_validation: string;
  access_mode: string;
  default: boolean;
  creation_time: string;
}

export interface VeleroRestore {
  name: string;
  namespace: string;
  phase: string;
  backup_name: string;
  included_namespaces: string[];
  restore_pvs: boolean;
  started_at: string;
  completed_at: string;
  errors: number;
  warnings: number;
  creation_time: string;
}

export interface CreateVeleroBackupRequest {
  name: string;
  included_namespaces?: string[];
  label_selector?: string;
  snapshot_volumes?: boolean;
  ttl?: string;
}

export interface CreateVeleroScheduleRequest {
  name: string;
  schedule: string;
  included_namespaces?: string[];
  label_selector?: string;
  snapshot_volumes?: boolean;
  ttl?: string;
}

export interface CreateVeleroRestoreRequest {
  name?: string;
  included_namespaces?: string[];
  label_selector?: string;
  restore_pvs?: boolean;
}

export interface CreateStorageLocationRequest {
  name?: string;
  provider?: string;
  bucket: string;
  prefix?: string;
  region?: string;
  s3_url?: string;
  s3_force_path_style?: boolean;
  credential_secret?: string;
  credential_key?: string;
  access_mode?: string;
  default?: boolean;
}
