/**
 * A VPC brings its default subnet with it, so both lists have to refresh.
 *
 * The Subnets tab kept showing the list from before: after creating two VPCs
 * through the wizard it still read "Total Subnets 2" with four on the cluster,
 * and only a page reload corrected it. Creating a VPC invalidated `['vpcs']`
 * alone, while the subnet list reads `['network', 'subnets']`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useCreateVpc, useDeleteVpc } from '../useVpcs';

vi.mock('@/api/vpcs', () => ({
  createVpc: vi.fn().mockResolvedValue({ name: 't1' }),
  deleteVpc: vi.fn().mockResolvedValue(undefined),
  listVpcs: vi.fn(),
  getVpc: vi.fn(),
  addVpcPeering: vi.fn(),
  removeVpcPeering: vi.fn(),
  getVpcRoutes: vi.fn(),
}));

function wrapper(qc: QueryClient) {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe('VPC mutations invalidate the subnet list too', () => {
  let qc: QueryClient;
  let invalidated: unknown[][];

  beforeEach(() => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    invalidated = [];
    const original = qc.invalidateQueries.bind(qc);
    vi.spyOn(qc, 'invalidateQueries').mockImplementation((filters: any) => {
      invalidated.push(filters?.queryKey);
      return original(filters);
    });
  });

  it('on create', async () => {
    const { result } = renderHook(() => useCreateVpc(), { wrapper: wrapper(qc) });
    result.current.mutate({ name: 't1' } as any);

    await waitFor(() => expect(invalidated.length).toBeGreaterThan(0));
    expect(invalidated).toContainEqual(['vpcs']);
    expect(invalidated).toContainEqual(['network']);
  });

  it('on delete — the subnet goes away with the VPC', async () => {
    const { result } = renderHook(() => useDeleteVpc(), { wrapper: wrapper(qc) });
    result.current.mutate('t1');

    await waitFor(() => expect(invalidated.length).toBeGreaterThan(0));
    expect(invalidated).toContainEqual(['vpcs']);
    expect(invalidated).toContainEqual(['network']);
  });
});
