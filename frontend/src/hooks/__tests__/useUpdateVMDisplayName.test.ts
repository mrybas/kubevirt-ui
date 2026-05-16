/**
 * useUpdateVMDisplayName hook — tests for mutation and cache invalidation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { useUpdateVMDisplayName } from '../useVMs';

// ─── mocks ────────────────────────────────────────────────────────────────────

const mockUpdateVMDisplayNameApi = vi.fn();

vi.mock('@/api/vms', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/vms')>();
  return {
    ...actual,
    updateVMDisplayName: (...args: any[]) => mockUpdateVMDisplayNameApi(...args),
  };
});

vi.mock('@/store/notifications', () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

// ─── helpers ──────────────────────────────────────────────────────────────────

function makeWrapper(queryClient: QueryClient) {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

// ─── tests ────────────────────────────────────────────────────────────────────

describe('useUpdateVMDisplayName', () => {
  let queryClient: QueryClient;
  const invalidateSpy = vi.fn();

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.invalidateQueries = invalidateSpy;
    mockUpdateVMDisplayNameApi.mockResolvedValue({
      name: 'web-server-q4r8z',
      display_name: 'Updated Name',
      namespace: 'default',
      status: 'Running',
      ready: true,
      labels: {},
      annotations: {},
      conditions: [],
      volumes: [],
      disks: [],
    });
    vi.clearAllMocks();
  });

  it('calls the API with correct arguments', async () => {
    const { result } = renderHook(() => useUpdateVMDisplayName(), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      namespace: 'default',
      name: 'web-server-q4r8z',
      data: { display_name: 'Updated Name' },
    });

    await waitFor(() => expect(mockUpdateVMDisplayNameApi).toHaveBeenCalledOnce());
    expect(mockUpdateVMDisplayNameApi).toHaveBeenCalledWith(
      'default',
      'web-server-q4r8z',
      { display_name: 'Updated Name' }
    );
  });

  it('invalidates [vms] query on success', async () => {
    const { result } = renderHook(() => useUpdateVMDisplayName(), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      namespace: 'default',
      name: 'web-server-q4r8z',
      data: { display_name: 'Updated Name' },
    });

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['vms'] })
    );
  });

  it('invalidates [vm, namespace, name] query on success', async () => {
    const { result } = renderHook(() => useUpdateVMDisplayName(), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      namespace: 'default',
      name: 'web-server-q4r8z',
      data: { display_name: 'Updated Name' },
    });

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ['vm', 'default', 'web-server-q4r8z'],
      })
    );
  });

  it('invalidates both queries on success (not just one)', async () => {
    const { result } = renderHook(() => useUpdateVMDisplayName(), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      namespace: 'default',
      name: 'web-server-q4r8z',
      data: { display_name: 'Updated Name' },
    });

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(2));
  });
});
