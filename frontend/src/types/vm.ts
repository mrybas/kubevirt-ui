export interface VMCondition {
  type: string;
  status: string;
  reason?: string;
  message?: string;
}

export interface VMConsoleConfig {
  vnc_enabled: boolean;
  serial_console_enabled: boolean;
}

export interface VM {
  name: string;
  namespace: string;
  status: string;
  ready: boolean;
  created?: string;
  cpu_cores?: number;
  memory?: string;
  run_strategy?: string;
  console?: VMConsoleConfig;
  phase?: string;
  ip_address?: string;
  node?: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  project?: string;
  environment?: string;
  owner?: string;
  conditions: VMCondition[];
  volumes: string[];
  disks: VMDiskInfo[];
}

export interface VMListResponse {
  items: VM[];
  total: number;
}

export interface VMStatusResponse {
  name: string;
  namespace: string;
  action: string;
  success: boolean;
  message?: string;
}

export interface VMCreateRequest {
  name: string;
  cpu_cores?: number;
  memory?: string;
  run_strategy?: 'Always' | 'Halted' | 'Manual' | 'RerunOnFailure' | 'Once';
  labels?: Record<string, string>;
  container_disk_image?: string;
  cloud_init?: {
    user_data?: string;
  };
}

export interface VMUpdateRequest {
  cpu_cores?: number;
  memory?: string;
  run_strategy?: 'Always' | 'Halted' | 'Manual' | 'RerunOnFailure' | 'Once';
  console?: Partial<VMConsoleConfig>;
  labels?: Record<string, string>;
  annotations?: Record<string, string>;
}

export interface VMDiskInfo {
  name: string;
  type: string;
  source_name?: string;
  size?: string;
  storage_class?: string;
  bus: string;
  boot_order?: number;
  is_cloudinit: boolean;
}

export interface DiskDetailResponse {
  name: string;
  type: string;
  source_name?: string;
  size?: string;
  storage_class?: string;
  bus: string;
  boot_order?: number;
  is_cloudinit: boolean;
  status?: string;
  can_resize: boolean;
}

export interface DiskResizeRequest {
  new_size: string;
}

export interface StopVMOptions {
  force?: boolean;
  gracePeriod?: number;
}

export interface VMEvent {
  type: string;
  reason: string;
  message: string;
  source: string;
  source_name: string;
  first_timestamp: string | null;
  last_timestamp: string | null;
  count: number;
}

export interface VMEventsResponse {
  items: VMEvent[];
}

export interface AttachDiskRequest {
  disk_name: string;
  pvc_name: string;
  bus?: string;
  is_cdrom?: boolean;
}

export interface HotplugCapabilities {
  hotplug_supported: boolean;
  declarative: boolean;
  supported_bus_types: string[];
}

export interface VolumeSnapshotInfo {
  name: string;
  namespace: string;
  pvc_name: string;
  storage_class: string;
  size: string;
  ready: boolean;
  creation_time: string;
  snapshot_class: string;
}

export interface VMSnapshotInfo {
  name: string;
  namespace: string;
  vm_name: string;
  phase: string;
  ready: boolean;
  creation_time: string;
  indications: string[];
  error: string | null;
}

export interface VMInterfaceInfo {
  name: string;
  binding: string;
  network_type: string;
  network_name: string;
  is_default: boolean;
  mac: string | null;
  ip_address: string | null;
  ip_addresses: string[];
  interface_name: string | null;
  state: string | null;
  hotplugged: boolean;
}

export interface AddNICRequest {
  name: string;
  network_name: string;
  binding?: string;
  mac_address?: string;
}

export interface CloneVMRequest {
  new_name: string;
  target_namespace?: string;
  start?: boolean;
}

export interface ResizeVMRequest {
  cpu_cores?: number;
  cpu_sockets?: number;
  memory?: string;
}

export type VMResponse = VM;
