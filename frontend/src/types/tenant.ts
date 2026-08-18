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
  /** What the worker nodes run. Talos swaps the CAPI bootstrap provider. */
  worker_os?: 'cloud-init' | 'talos';
  worker_count: number;
  worker_vcpu: number;
  worker_memory: string;
  worker_disk: string;
  pod_cidr: string;
  service_cidr: string;
  enable_oidc?: boolean;
  admin_group: string;
  viewer_group: string;
  // Worker DNS injection (written into /etc/resolv.conf via cloud-init)
  dns_servers?: string[];
  dns_mode?: 'append' | 'override';
  dns_include_public_fallback?: boolean;
  // Folder / environment binding (T8 — confirmed field names from backend T1)
  folder?: string;      // required by backend when using folder model; optional here for compat
  environment?: string; // required by backend when folder is set
  // Network: VPC chosen explicitly by user; empty = default cluster network
  vpc_name?: string;
  // Worker image (T10)
  worker_image_url?: string;
  worker_image_source_type?: 'http' | 'registry';
  worker_image_pull_secrets?: string[];
  // Network binding (T11 — field name matches backend T6)
  worker_network_binding?: 'bridge' | 'masquerade';
  // Storage (T5 — kubevirt-csi persistent storage for workers)
  enable_storage?: boolean;
  storage_class?: string;       // empty string = cluster default
  storage_quota_gi?: number;
  storage_pvc_count?: number;
  addons: TenantAddon[];
}

export interface TenantScaleRequest {
  worker_count: number;
  /** Optional resize: when set (and changed) the worker template is rotated. */
  worker_vcpu?: number;
  worker_memory?: string;
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
  folder?: string;
  environment?: string;
  kubernetes_version: string;
  status: string;
  /** Why the status is not Ready (no workers joined, an addon wedged). */
  status_detail?: string | null;
  /** Address this tenant's subnet SNATs to on the transit network — the one
   *  the control-plane guard ACLs are keyed on. Detail view only. */
  transit_snat_ip?: string | null;
  /** Where the control plane answers for a *joining node* — not the human
   *  endpoint. Three schemes coexist, and the ports carry the identity when
   *  the address is shared. */
  control_plane_address?: {
    address: string;
    api_port: number;
    konnectivity_port?: number | null;
    trustd_port?: number | null;
    shared_with: string[];
    source: 'service' | 'advertised' | 'ingress';
  } | null;
  phase: string | null;
  endpoint: string | null;
  control_plane_replicas: number;
  control_plane_ready_replicas?: number;
  control_plane_ready: boolean;
  worker_type: string;
  /** cloud-init or talos — read from the root volume the workers boot;
   *  only a Talos tenant has an API for talosctl to reach. */
  worker_os?: 'cloud-init' | 'talos';
  worker_count: number;
  workers_ready: number;
  worker_vcpu: number;
  worker_memory: string;
  pod_cidr: string;
  service_cidr: string;
  enable_oidc?: boolean;
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

export interface TenantStorageStatus {
  tenant: string;
  phase: 'disabled' | 'pending' | 'ready';
  host_credentials_ready: boolean;
  capk_credentials_ready: boolean;
  tenant_reachable: boolean;
  credentials_replicated: boolean;
  detail: string;
}

export interface TenantStorageReconcileResponse {
  tenant: string;
  credentials_replicated: boolean;
  detail: string;
}

export interface StorageClassInfo {
  name: string;
  provisioner: string;
  volume_binding_mode: string;
  is_default: boolean;
  mode: string;         // 'block' (RBD) | 'filesystem' (CephFS) | ''
  suggested: boolean;   // preselect this one for INFRA_STORAGE_CLASS_NAME
}

export interface StorageDiscovery {
  type: string;         // 'linstor' | 'ceph'
  api_url: string;      // empty for StorageClass-only backends (Ceph)
  pools: StoragePoolInfo[];
  storage_classes: StorageClassInfo[];
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

export interface TalosRelease {
  talos: string;
  image_url: string;
  sha: string;
  k8s_min: string;
  k8s_max: string;
  default: boolean;
}

export interface TalosVersionsResponse {
  /** Releases that support the Kubernetes version asked for. Already filtered
   *  by the server; the client does not apply the rule again. */
  items: TalosRelease[];
  /** How many the filter removed — said out loud rather than silently absent. */
  hidden: number;
  default: string;
}

export interface TenantTalosconfigResponse {
  tenant: string;
  /** Node addresses talosctl will speak to — apid on :50000, not the tenant's
   *  Kubernetes control plane, which is Kamaji pods and has no Talos API. */
  nodes: string[];
  expires_hours: number;
  talosconfig: string;
}
