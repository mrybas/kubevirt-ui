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
  announced: number;
  node: string;
}

export interface GatewayConfigExample {
  name: string;
  title: string;
  description: string;
  config: string;
}

// ============================================================================
// BgpConf — the FRR config a VpcEgressGateway peers with
// ============================================================================
//
// Distinct from the speaker DaemonSet above: the speaker announces
// pod/service/EIP routes from the nodes, BgpConf configures the FRR inside an
// egress gateway, which announces the VPC's own subnets. One shared config
// serves every gateway — router-id is left unset so FRR derives a unique one
// per gateway from its internal address.

export interface BgpConfRequest {
  name?: string;
  local_asn: number;
  peer_asn: number;
  neighbours: string[];
  graceful_restart?: boolean;
  hold_time?: string;
  keepalive_time?: string;
}

export interface BgpConfResponse {
  name: string;
  local_asn: number;
  peer_asn: number;
  neighbours: string[];
  graceful_restart: boolean;
  hold_time: string;
  keepalive_time: string;
  router_id: string;
}

export interface BgpConfListResponse {
  items: BgpConfResponse[];
  total: number;
}
