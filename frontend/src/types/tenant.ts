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
  // Folder / environment binding (T8 — confirmed field names from backend T1)
  folder?: string;      // required by backend when using folder model; optional here for compat
  environment?: string; // required by backend when folder is set
  // Network isolation (T9 — backend T3 contract: network_isolation boolean removed)
  // Backend always creates vpc-<name> for isolated tenants; no vpc_name / egress_gateway choice
  network_isolation_mode?: 'shared' | 'isolated_shared_egress' | 'isolated_dedicated_egress';
  infra_subnet?: string | null; // required (non-null) iff mode == isolated_dedicated_egress
  // Worker image (T10)
  worker_image_url?: string;
  worker_image_source_type?: 'http' | 'registry';
  worker_image_pull_secrets?: string[];
  // Network binding (T11 — field name matches backend T6)
  worker_network_binding?: 'bridge' | 'masquerade';
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
