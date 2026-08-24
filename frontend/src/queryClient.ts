/**
 * The one QueryClient, and the safety net under every write.
 *
 * A mutation that fails and is not handled used to produce nothing: an
 * unhandled promise rejection in the console, a dialog sitting open, and no
 * sentence anywhere. UAT run 4, Q-3: the backend refused a disk with
 * "400Gi of storage and 110Gi already used, so 290Gi is free — asks for
 * 350Gi", which is exactly the message somebody needs, and the page dropped
 * it on the floor. The silence had moved up a layer from the API to the UI.
 *
 * Rather than 45 identical try/catch blocks, the cache reports what nobody
 * else claimed. A mutation that declares its own `onError` is handling it —
 * usually inline, next to the field that is wrong, which is better than a
 * toast — so this keeps quiet for those and speaks for the rest.
 */

import { MutationCache, QueryClient } from '@tanstack/react-query';

import { notify } from './store/notifications';

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10 * 1000,
        retry: 1,
      },
    },
    mutationCache: new MutationCache({
      onError: (error, _variables, _context, mutation) => {
        // Handled where it happened: either by the mutation's own onError, or
        // by a call site that says so. The second exists because a dialog that
        // renders the refusal next to the field that is wrong does not also
        // want it as a toast — one refusal, reported twice, is the noise this
        // net was meant to replace, not add to.
        if (mutation.options.onError) return;
        if (mutation.meta?.handledLocally) return;
        const message = error instanceof Error ? error.message : String(error);
        notify.error(message || 'The request was refused');
      },
    }),
  });
}
