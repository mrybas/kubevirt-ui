/**
 * The underlay fabric must be buildable from the UI.
 *
 * `POST /network/underlay` existed in the backend for a long time while no
 * screen ever called it, so every fresh cluster had its fabric applied by hand
 * from lab manifests — and a VPC created in the wizard came up attached to
 * nothing. These tests fail against a build where the page or its tab is gone,
 * which is exactly the state that shipped.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Networks } from '../Networks';

const apiRequest = vi.fn();

vi.mock('@/api/client', () => ({
  apiRequest: (...args: any[]) => apiRequest(...args),
}));

vi.mock('@/store/notifications', () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

// The other tabs are not under test and drag in the whole network stack.
vi.mock('../VPCs', () => ({ default: () => null }));
vi.mock('../Network', () => ({ Network: () => null }));
vi.mock('../SystemNetworks', () => ({ SystemNetworks: () => null }));

const MISSING = {
  ready: false,
  detail: 'Missing: ProviderNetwork/external, Vlan/vlan-external, Subnet/ext-sub.',
  objects: [
    { kind: 'ProviderNetwork', name: 'external', namespace: '', state: 'missing', detail: '', workaround: false },
    { kind: 'Vlan', name: 'vlan-external', namespace: '', state: 'missing', detail: '', workaround: false },
    { kind: 'Subnet', name: 'ext-sub', namespace: '', state: 'missing', detail: '', workaround: false },
  ],
};

function route(endpoint: string) {
  if (endpoint.startsWith('/network/underlay')) return Promise.resolve(MISSING);
  if (endpoint.startsWith('/cluster/nodes') || endpoint.startsWith('/nodes')) {
    return Promise.resolve({
      items: [
        { name: 'cp-1', status: 'Ready', roles: ['control-plane'] },
        { name: 'worker-1', status: 'Ready', roles: [] },
      ],
      total: 2,
    });
  }
  return Promise.resolve({});
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Networks />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function fillAndSubmit() {
  fireEvent.change(await screen.findByPlaceholderText('eth1'), {
    target: { value: 'eth1' },
  });
  fireEvent.change(screen.getByPlaceholderText('10.198.176.0/20'), {
    target: { value: '10.198.176.0/20' },
  });
  fireEvent.change(screen.getByPlaceholderText('10.198.191.254'), {
    target: { value: '10.198.191.254' },
  });
  fireEvent.click(screen.getByRole('button', { name: /build underlay/i }));
}

describe('Underlay tab', () => {
  beforeEach(() => {
    apiRequest.mockReset();
    apiRequest.mockImplementation((endpoint: string) => route(endpoint));
  });

  it('is reachable from the Networks page', async () => {
    renderAt('/network?tab=underlay');
    expect(await screen.findByText('Underlay Fabric')).toBeInTheDocument();
  });

  it('reads the current fabric state instead of assuming it', async () => {
    renderAt('/network?tab=underlay');
    await waitFor(() =>
      expect(apiRequest).toHaveBeenCalledWith(expect.stringContaining('/network/underlay')),
    );
    expect(await screen.findByText(/Missing: ProviderNetwork\/external/)).toBeInTheDocument();
  });

  it('POSTs the fabric the operator described', async () => {
    renderAt('/network?tab=underlay');
    await screen.findByText('Underlay Fabric');
    await fillAndSubmit();

    await waitFor(() => {
      expect(apiRequest).toHaveBeenCalledWith(
        '/network/underlay',
        expect.objectContaining({ method: 'POST' }),
      );
    });
    const call = apiRequest.mock.calls.find(
      (c) => c[0] === '/network/underlay' && c[1]?.method === 'POST',
    );
    expect(call![1].body).toMatchObject({
      interface: 'eth1',
      external_cidr: '10.198.176.0/20',
      external_gateway: '10.198.191.254',
      vlan_id: 0,
      link_watcher: true,
    });
  });

  it('will not submit a fabric with no interface — that builds a subnet with no way out', async () => {
    renderAt('/network?tab=underlay');
    await screen.findByText('Underlay Fabric');
    fireEvent.change(screen.getByPlaceholderText('10.198.176.0/20'), {
      target: { value: '10.198.176.0/20' },
    });
    fireEvent.click(screen.getByRole('button', { name: /build underlay/i }));
    await waitFor(() => {
      expect(
        apiRequest.mock.calls.filter((c) => c[1]?.method === 'POST'),
      ).toHaveLength(0);
    });
  });

  it('excludes control planes by default — they have a single NIC', async () => {
    renderAt('/network?tab=underlay');
    await screen.findByText('Underlay Fabric');
    await waitFor(() => expect(screen.getByText('cp-1')).toBeInTheDocument());
    await fillAndSubmit();
    await waitFor(() => {
      const call = apiRequest.mock.calls.find(
        (c) => c[0] === '/network/underlay' && c[1]?.method === 'POST',
      );
      expect(call).toBeTruthy();
      expect(call![1].body.exclude_nodes).toEqual(['cp-1']);
    });
  });
});
