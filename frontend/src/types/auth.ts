export interface AuthConfig {
  type: 'none' | 'oidc' | 'token';
  issuer?: string;
  client_id?: string;
  authorization_endpoint?: string;
  token_endpoint?: string;
  userinfo_endpoint?: string;
  user_management?: 'lldap' | 'external' | 'none';
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in?: number;
  refresh_token?: string;
  id_token?: string;
}

export interface UserInfo {
  id: string;
  email: string;
  username: string;
  groups: string[];
}

export interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: UserInfo | null;
  accessToken: string | null;
  refreshToken: string | null;
  config: AuthConfig | null;
}

export interface KubeconfigVariant {
  id: string;
  label: string;
  description: string;
  kubeconfig: string;
  instructions: string;
}

export interface KubeconfigResponse {
  variants: KubeconfigVariant[];
  cluster_name: string;
  server: string;
  username: string;
  auth_type: string;
}
