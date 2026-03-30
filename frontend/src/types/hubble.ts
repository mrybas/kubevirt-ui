export interface HubbleFlow {
  time: string;
  source_namespace: string;
  source_pod: string;
  source_ip: string;
  destination_namespace: string;
  destination_pod: string;
  destination_ip: string;
  destination_port: number;
  protocol: string;
  verdict: string;
  drop_reason: string;
  policy_match: string;
  summary: string;
}

export interface HubbleFlowsResponse {
  flows: HubbleFlow[];
  total: number;
  hubble_namespace: string;
}

export interface HubbleStatusResponse {
  available: boolean;
  namespace: string;
  pod_name: string;
  num_connected_nodes: number;
  max_flows: number;
  message: string;
}

export interface HubbleFlowsParams {
  namespace?: string;
  pod?: string;
  verdict?: string;
  protocol?: string;
  limit?: number;
  since?: string;
}
