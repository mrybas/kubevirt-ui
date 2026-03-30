// Addon catalog (from ConfigMap)

export interface AddonParameter {
  id: string;
  name: string;
  type: 'string' | 'select';
  default: string;
  options: string[];
  auto_discover: boolean;
  valuesPath: string;
}

export interface AddonComponent {
  id: string;
  name: string;
  category: string;
  description: string;
  required: boolean;
  default: boolean;
  chartPath: string;
  namespace: string;
  discovery_type: string;
  defaultValues: Record<string, unknown>;
  parameters: AddonParameter[];
}

export interface AddonCatalog {
  git_repository_ref: Record<string, string>;
  base_path: string;
  components: AddonComponent[];
}

// Tenant create / update

export interface TenantAddon {
  addon_id: string;
  parameters: Record<string, string>;
}

export interface TenantCreateRequest {
  name: string;
  display_name: string;
  kubernetes_version: string;
  control_plane_replicas: number;
  worker_type: 'vm' | 'bare_metal';
  worker_count: number;
  worker_vcpu: number;
  worker_memory: string;
  worker_disk: string;
  pod_cidr: string;
  service_cidr: string;
  admin_group: string;
  viewer_group: string;
  network_isolation?: boolean;
  egress_gateway?: string;
  addons: TenantAddon[];
}

export interface TenantScaleRequest {
  worker_count: number;
}

// Tenant response

export interface TenantAddonStatus {
  addon_id: string;
  name: string;
  ready: boolean;
  last_reconcile: string | null;
  message: string | null;
}

export interface TenantCondition {
  type: string;
  status: string;
  message: string;
  reason: string;
  last_transition_time: string | null;
}

export interface Tenant {
  name: string;
  display_name: string;
  namespace: string;
  kubernetes_version: string;
  status: string;
  phase: string | null;
  endpoint: string | null;
  control_plane_replicas: number;
  control_plane_ready: boolean;
  worker_type: string;
  worker_count: number;
  workers_ready: number;
  worker_vcpu: number;
  worker_memory: string;
  pod_cidr: string;
  service_cidr: string;
  created: string | null;
  conditions: TenantCondition[];
  addons: TenantAddonStatus[];
}

export interface TenantListResponse {
  items: Tenant[];
  total: number;
}

export interface TenantKubeconfigResponse {
  kubeconfig: string;
}

// Discovery

export interface StoragePoolInfo {
  name: string;
  driver: string;
  free_gb: number;
  total_gb: number;
  node_count: number;
}

export interface StorageDiscovery {
  type: string;
  api_url: string;
  pools: StoragePoolInfo[];
}

export interface MonitoringDiscovery {
  type: string;
  write_url: string;
  query_url: string;
}

export interface LoggingDiscovery {
  type: string;
  push_url: string;
}

export interface RegistryDiscovery {
  type: string;
  url: string;
}

export interface DiscoveryResponse {
  storage: StorageDiscovery[];
  monitoring: MonitoringDiscovery[];
  logging: LoggingDiscovery[];
  registry: RegistryDiscovery[];
}
