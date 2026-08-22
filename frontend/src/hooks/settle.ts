/**
 * Refetching after a write that a controller has not finished making true.
 *
 * The product used to do the work in the request: by the time a POST answered,
 * the objects existed, and invalidating once was right. Now the request writes
 * a custom resource and an operator builds from it, so the answer arrives
 * before the fact does — and a single invalidation reads the world as it was
 * a moment before the write.
 *
 * UAT run 4 caught it three times in one session: "ProviderNetwork MISSING"
 * right after Build Underlay, "No peerings configured" right after Add with
 * the ManagedNetworkPeering already written and both legs in place, and an
 * image's status during import. Every one of them correct after a manual
 * reload. Each is small; together they teach people to press the button
 * twice, which on some of these pages is not harmless.
 *
 * So a write is followed by a few refetches over the seconds a controller
 * takes. This is a mitigation and not the cure: the cure is watching the
 * object's status, which needs a stream the API does not offer yet. What it
 * buys is that the page stops showing the previous world as if it were the
 * answer to what you just did.
 */

import type { QueryClient, QueryKey } from '@tanstack/react-query';

/** Roughly how long a controller takes to write what it was asked for. */
const SCHEDULE_MS = [500, 1500, 4000];

export function settle(
  queryClient: QueryClient,
  keys: QueryKey[],
  schedule: number[] = SCHEDULE_MS,
): void {
  const refetch = () => {
    for (const key of keys) queryClient.invalidateQueries({ queryKey: key });
  };
  refetch();
  for (const delay of schedule) setTimeout(refetch, delay);
}
