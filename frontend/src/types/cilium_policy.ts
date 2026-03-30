export interface CiliumPolicyCreateRequest {
  name: string;
  namespace: string;
  template?: string;
  allowed_fqdns?: string[];
  allowed_http_methods?: string[];
  allowed_http_paths?: string[];
  custom_spec?: Record<string, unknown>;
}

export interface CiliumPolicyResponse {
  name: string;
  namespace: string;
  spec: Record<string, unknown>;
  status?: Record<string, unknown> | null;
  ready: boolean;
  yaml_repr: string;
}

export interface CiliumPolicyListResponse {
  items: CiliumPolicyResponse[];
  total: number;
}

// Security Baseline
export interface SecurityBaselineCreateRequest {
  preset: string;
  name?: string;
  description?: string;
  custom_spec?: Record<string, unknown>;
}

export interface SecurityBaselineResponse {
  name: string;
  preset: string;
  description: string;
  spec: Record<string, unknown>;
  status?: Record<string, unknown> | null;
  enabled: boolean;
  yaml_repr: string;
}

export interface SecurityBaselineListResponse {
  items: SecurityBaselineResponse[];
  total: number;
  available_presets: { name: string; description: string }[];
}
