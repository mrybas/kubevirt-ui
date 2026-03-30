/**
 * VM Template types
 */

export interface TemplateCompute {
  cpu_cores: number;
  vcpu?: number;
  cpu_sockets: number;
  cpu_threads: number;
  memory: string;
}

export interface TemplateDisk {
  size: string;
  storage_class?: string;
}

export interface TemplateNetwork {
  type: 'default' | 'multus' | 'bridge';
  multus_network?: string;
}

export interface TemplateCloudInit {
  user_data?: string;
  network_data?: string;
}

export interface TemplateConsole {
  vnc_enabled: boolean;
  serial_console_enabled: boolean;
}

export interface VMTemplate {
  name: string;
  display_name: string;
  description?: string;
  icon?: string;
  category: string;
  os_type: string;
  golden_image_name: string;
  golden_image_namespace: string;
  compute: TemplateCompute;
  disk: TemplateDisk;
  network: TemplateNetwork;
  cloud_init?: TemplateCloudInit;
  console?: TemplateConsole;
  created?: string;
}

export interface VMTemplateCreate {
  name: string;
  display_name: string;
  description?: string;
  icon?: string;
  category?: string;
  os_type?: string;
  golden_image_name: string;
  golden_image_namespace?: string;
  compute?: Partial<TemplateCompute>;
  disk?: Partial<TemplateDisk>;
  network?: Partial<TemplateNetwork>;
  cloud_init?: TemplateCloudInit;
  console?: Partial<TemplateConsole>;
}

export interface VMTemplateListResponse {
  items: VMTemplate[];
  total: number;
}

/**
 * Golden Image types
 */

export type DiskType = 'image' | 'data';

export type ImageScope = 'environment' | 'project';

export interface GoldenImage {
  name: string;
  namespace: string;
  display_name?: string;
  description?: string;
  os_type?: string;
  os_version?: string;
  size?: string;
  status: 'Ready' | 'InUse' | 'Pending' | 'Error' | string;
  error_message?: string;
  source_url?: string;
  created?: string;
  used_by?: string[];  // List of VMs using this image
  disk_type: DiskType;  // image or data
  persistent: boolean;  // If true, disk is not cloned
  scope: ImageScope;  // environment (single ns) or project (all envs)
  project?: string;  // Project name (for project-scoped images)
  environment?: string;  // Environment name (from namespace label)
}

export interface GoldenImageCreate {
  name?: string;
  display_name?: string;
  description?: string;
  os_type?: string;
  os_version?: string;
  source_url?: string;
  source_registry?: string;
  source_pvc?: string;  // For cloning existing disk
  source_pvc_namespace?: string;
  size?: string;
  storage_class?: string;
  disk_type?: DiskType;  // image or data
  persistent?: boolean;  // If true, disk is not cloned
  scope?: ImageScope;  // environment (default) or project
  project?: string;  // Project name (when scope=project)
}

export interface GoldenImageUpdate {
  scope?: ImageScope;
  display_name?: string;
  description?: string;
}

export interface GoldenImageListResponse {
  items: GoldenImage[];
  total: number;
}

export interface CreateImageFromDiskRequest {
  source_disk_name: string;
  source_namespace: string;
  name: string;
  display_name?: string;
  description?: string;
  os_type?: string;
  os_version?: string;
}

/**
 * Persistent Disk types
 */

export interface PersistentDisk {
  name: string;
  namespace: string;
  size: string;
  storage_class?: string;
  status: string;
  attached_to?: string;
  created?: string;
}

export interface PersistentDiskCreate {
  name: string;
  size?: string;
  storage_class?: string;
  source_golden_image?: string;
  source_golden_image_namespace?: string;
}

export interface PersistentDiskListResponse {
  items: PersistentDisk[];
  total: number;
}

export interface AttachDiskRequest {
  disk_name: string;
  vm_name: string;
  hotplug?: boolean;
}

/**
 * Network configuration for VM creation
 */

export interface VMNetworkRequest {
  subnet: string;
  static_ip?: string;
}

/**
 * VM from Template request
 */

export interface VMFromTemplateRequest {
  name: string;
  template_name: string;
  cpu_cores?: number;
  memory?: string;
  disk_size?: string;
  ssh_key?: string;
  password?: string;
  user_data?: string;
  start?: boolean;
  network?: VMNetworkRequest;
  networks?: VMNetworkRequest[];
}
