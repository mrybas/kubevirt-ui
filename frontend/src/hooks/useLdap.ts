/**
 * LDAP hooks — group search for the Access tab group picker.
 */

import { useQuery } from '@tanstack/react-query';
import { searchLdapGroups } from '../api/ldap';

/**
 * Search LDAP groups by query string.
 * Only fires when query has at least 2 characters.
 * Does not retry on failure (LDAP may be unavailable).
 */
export function useLdapGroupSearch(query: string) {
  return useQuery({
    queryKey: ['ldap', 'groups', query],
    queryFn: () => searchLdapGroups(query),
    enabled: query.trim().length >= 2,
    staleTime: 30_000,
    retry: false,
  });
}
