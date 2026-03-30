import { apiRequest } from './client';
import type {
  NamespaceListResponse,
  NodeListResponse,
  ClusterStatus,
  UserResources,
  RecentActivityResponse,
  ClusterSettings,
} from '@/types/cluster';

export async function listNamespaces(): Promise<NamespaceListResponse> {
  return apiRequest<NamespaceListResponse>('/namespaces');
}

export async function listNodes(): Promise<NodeListResponse> {
  return apiRequest<NodeListResponse>('/cluster/nodes');
}

export async function getClusterStatus(): Promise<ClusterStatus> {
  return apiRequest<ClusterStatus>('/cluster/status');
}

export async function getUserResources(): Promise<UserResources> {
  return apiRequest<UserResources>('/cluster/resources');
}

export async function getRecentActivity(limit: number = 10): Promise<RecentActivityResponse> {
  return apiRequest<RecentActivityResponse>(`/cluster/activity?limit=${limit}`);
}

export async function getClusterSettings(): Promise<ClusterSettings> {
  return apiRequest<ClusterSettings>('/cluster/settings');
}

export async function updateClusterSettings(settings: ClusterSettings): Promise<ClusterSettings> {
  return apiRequest<ClusterSettings>('/cluster/settings', {
    method: 'PUT',
    body: settings,
  });
}
