/**
 * LDAP API — group search for the Access tab group picker.
 * Backed by the FreeIPA readonly LDAP bind on the backend.
 */

import { apiRequest } from './client';

/**
 * Search LDAP groups by name prefix/substring.
 * Backend: GET /api/v1/ldap/groups?q=<query>&limit=<n>
 * Returns { items: string[] } — gracefully empty when LDAP isn't configured.
 */
export async function searchLdapGroups(query: string, limit = 20): Promise<string[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const resp = await apiRequest<{ items: string[] }>(`/ldap/groups?${params}`);
  return resp.items;
}
