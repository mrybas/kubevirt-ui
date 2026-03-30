export interface Namespace {
  name: string;
  status: string;
  labels: Record<string, string>;
  created?: string;
}

export interface NamespaceListResponse {
  items: Namespace[];
  total: number;
}

export interface NodeResourceUsage {
  total: number;
  used: number;
  free: number;
  percentage: number;
}

export interface Node {
  name: string;
  status: string;
  roles: string[];
  version?: string;
  os?: string;
  cpu?: string;
  memory?: string;
  internal_ip?: string;
  cpu_usage?: NodeResourceUsage;
  memory_usage?: NodeResourceUsage;
}

export interface NodeListResponse {
  items: Node[];
  total: number;
}

export interface ClusterStatus {
  kubevirt: {
    installed: boolean;
    phase?: string;
    version?: string;
    targetVersion?: string;
    error?: string;
  };
  cdi: {
    installed: boolean;
    phase?: string;
    error?: string;
  };
  nodes_count: number;
  nodes_ready: number;
}

export interface ResourceUsage {
  used: string;
  total: string;
  percentage: number;
}

export interface ClusterSettings {
  cpu_overcommit: number;
}

export interface SchedulableSlot {
  cpu_cores: number;
  memory_gi: number;
  node: string;
}

export interface UserResources {
  vms_total: number;
  vms_running: number;
  cpu: ResourceUsage;
  memory: ResourceUsage;
  storage: ResourceUsage;
  max_schedulable: SchedulableSlot | null;
}

export interface ActivityItem {
  id: string;
  type: string;
  message: string;
  resource_name: string;
  resource_namespace: string;
  timestamp: string;
  icon: string;
}

export interface RecentActivityResponse {
  items: ActivityItem[];
}
