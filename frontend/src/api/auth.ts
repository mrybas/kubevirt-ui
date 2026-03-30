import { apiRequest } from './client';
import type { AuthConfig, TokenResponse, UserInfo, KubeconfigVariant, KubeconfigResponse } from '../types/auth';

export type { KubeconfigVariant, KubeconfigResponse };

export async function getAuthConfig(): Promise<AuthConfig> {
  return apiRequest<AuthConfig>('/auth/config');
}

export async function exchangeToken(code: string, redirectUri: string): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/auth/token', {
    method: 'POST',
    body: { code, redirect_uri: redirectUri },
  });
}

export async function refreshToken(refreshToken: string): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/auth/refresh', {
    method: 'POST',
    body: refreshToken,
  });
}

export async function getCurrentUser(accessToken: string): Promise<UserInfo> {
  return apiRequest<UserInfo>('/auth/me', {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
}

export async function getKubeconfig(tokens?: {
  id_token?: string;
  refresh_token?: string;
}): Promise<KubeconfigResponse> {
  return apiRequest<KubeconfigResponse>('/auth/kubeconfig', {
    method: 'POST',
    body: tokens || {},
  });
}

export async function logout(): Promise<void> {
  await apiRequest('/auth/logout', { method: 'POST' });
}

// Helper to build OIDC authorization URL
export function buildAuthorizationUrl(config: AuthConfig, redirectUri: string): string {
  if (!config.authorization_endpoint || !config.client_id) {
    throw new Error('OIDC not configured');
  }

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.client_id,
    redirect_uri: redirectUri,
    scope: 'openid profile email groups',
    state: generateState(),
  });

  return `${config.authorization_endpoint}?${params.toString()}`;
}

// Generate random state for CSRF protection
function generateState(): string {
  const array = new Uint8Array(16);
  crypto.getRandomValues(array);
  const state = Array.from(array, (byte) => byte.toString(16).padStart(2, '0')).join('');
  sessionStorage.setItem('oauth_state', state);
  return state;
}

// Validate state from callback
export function validateState(state: string): boolean {
  const savedState = sessionStorage.getItem('oauth_state');
  sessionStorage.removeItem('oauth_state');
  return state === savedState;
}
