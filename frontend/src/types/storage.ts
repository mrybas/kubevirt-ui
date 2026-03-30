export type DataVolumeSourceType = 'blank' | 'http' | 'registry' | 'pvc' | 's3';

export interface DataVolume {
  name: string;
  namespace: string;
  display_name?: string;
  phase: string | null;
  progress: string | null;
  size: string | null;
  storage_class: string | null;
  source_type: string | null;
  created: string | null;
  labels: Record<string, string>;
}

export interface DataVolumeListResponse {
  items: DataVolume[];
  total: number;
}

export interface DataVolumeCreateRequest {
  name: string;
  size: string;
  storage_class?: string;
  access_modes?: string[];
  source_type: DataVolumeSourceType;
  source_url?: string;
  registry_url?: string;
  source_pvc_name?: string;
  source_pvc_namespace?: string;
  s3_url?: string;
  s3_secret_ref?: string;
  labels?: Record<string, string>;
}

export interface PVC {
  name: string;
  namespace: string;
  phase: string | null;
  size: string | null;
  storage_class: string | null;
  access_modes: string[];
  volume_name: string | null;
  created: string | null;
  labels: Record<string, string>;
}

export interface PVCListResponse {
  items: PVC[];
  total: number;
}

export interface StorageClass {
  name: string;
  provisioner: string;
  reclaim_policy: string | null;
  volume_binding_mode: string | null;
  allow_volume_expansion: boolean;
  is_default: boolean;
}

export interface StorageClassDetail {
  name: string;
  provisioner: string;
  reclaim_policy: string | null;
  volume_binding_mode: string | null;
  allow_volume_expansion: boolean;
  is_default: boolean;
  parameters: Record<string, string>;
  pv_count: number;
  pvc_count: number;
  total_capacity_bytes: number;
  used_capacity_bytes: number;
  created: string | null;
}

export interface StorageClassListResponse {
  items: StorageClass[];
  total: number;
}
