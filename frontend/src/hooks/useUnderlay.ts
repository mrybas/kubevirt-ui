/**
 * VPC underlay fabric hooks
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getUnderlay, ensureUnderlay, type EnsureUnderlayRequest } from '../api/underlay';
import { notify } from '../store/notifications';
import { settle } from './settle';

export function useUnderlay(names?: {
  provider_network_name?: string;
  vlan_name?: string;
  subnet_name?: string;
}) {
  return useQuery({
    queryKey: ['underlay', names?.provider_network_name, names?.vlan_name, names?.subnet_name],
    queryFn: () => getUnderlay(names),
    refetchInterval: 30000,
  });
}

export function useEnsureUnderlay() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: EnsureUnderlayRequest) => ensureUnderlay(request),
    onSuccess: (result) => {
      // Over the next few seconds, not once: the request writes a
      // ManagedUnderlay and the operator makes the provider network, the
      // VLAN and the subnet from it. Reading straight away is what put
      // "ProviderNetwork MISSING" on the screen of a build that worked.
      settle(queryClient, [['underlay'], ['subnets']]);
      // A partial build reports ready=false with the objects that failed; that
      // is a warning, not a success — surfacing it as one is how a half-built
      // fabric gets mistaken for a working one.
      if (result.ready) {
        notify.success('Underlay ready — VPC egress gateways can attach');
      } else {
        notify.error(result.detail || 'Underlay incomplete');
      }
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to build the underlay');
    },
  });
}
