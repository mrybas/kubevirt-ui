/**
 * TypeScript interfaces for BGP speaker management.
 * Matches backend app/models/bgp.py
 */

export interface SpeakerDeployRequest {
  neighbor_address: string;
  neighbor_as: number;
  cluster_as: number;
  announce_cluster_ip: boolean;
  node_names: string[];
}

export interface SpeakerPodInfo {
  name: string;
  node: string;
  status: string;
}

export interface SpeakerStatusResponse {
  deployed: boolean;
  config: Record<string, string>;
  pods: SpeakerPodInfo[];
  node_labels: string[];
}

export interface AnnouncementRequest {
  resource_type: string; // "subnet" | "service" | "eip"
  resource_name: string;
  resource_namespace: string;
  policy: string; // "cluster" | "local"
}

export interface AnnouncementResponse {
  resource_type: string;
  resource_name: string;
  resource_namespace: string;
  bgp_enabled: boolean;
  policy: string;
}

export interface BGPSessionResponse {
  peer_address: string;
  peer_asn: number;
  state: string; // "Established" | "Active" | "Connect" | ...
  uptime: string;
  prefixes_received: number;
  node: string;
}

export interface GatewayConfigExample {
  name: string;
  title: string;
  description: string;
  config: string;
}
