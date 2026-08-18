/**
 * The tenant page showed everything except the address a worker joins through.
 *
 * `Endpoint` is the ingress URL for a human. A joining node dials the API,
 * konnectivity and — on Talos — trustd by address and port, and a join that
 * never completes is diagnosed against those. They were readable only with
 * kubectl, which made the per-tenant VIP work impossible to check from the
 * product that performs it.
 *
 * And the address alone is not the answer: this stand runs a shared VIP where
 * each tenant is distinguished by port (20000, 20002, …). Printing
 * `10.199.0.100` for both t1 and t3 would look like an answer and be one.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import TenantDetail from '../TenantDetail';

let workerOs: 'talos' | 'cloud-init' = 'talos';
let cp: any = {
  address: '10.199.0.101',
  api_port: 6443,
  konnectivity_port: 8132,
  trustd_port: 50001,
  shared_with: [],
  source: 'service',
};

const baseTenant = () => ({
  name: 't8', display_name: 't8', namespace: 'tenant-t8', folder: '',
  environment: '', kubernetes_version: 'v1.32.1', status: 'Ready',
  endpoint: 'https://t8.10.198.175.200.nip.io', worker_type: 'vm',
  worker_count: 1, workers_ready: 1, worker_vcpu: 2, worker_memory: '2Gi',
  control_plane_replicas: 1, control_plane_ready_replicas: 1,
  control_plane_ready: true, pod_cidr: '', service_cidr: '',
  enable_oidc: false, conditions: [], addons: [],
  control_plane_address: cp,
  worker_os: workerOs,
});

vi.mock('@/hooks/useTenants', () => ({
  useTenant: () => ({ data: { ...baseTenant(), control_plane_address: cp, worker_os: workerOs }, isLoading: false, refetch: vi.fn() }),
  useDeleteTenant: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useScaleTenant: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTenantKubeconfig: () => ({ data: null, isLoading: false }),
  // T5: the page offers a talosconfig for Talos tenants. Manual-fetch only,
  // so an unclicked mock is enough here.
  useTenantTalosconfig: () => ({ refetch: vi.fn(), isFetching: false }),
  useAddonCatalog: () => ({ data: { components: [] } }),
  useEnableAddon: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDisableAddon: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTenantImages: () => ({ data: { items: [] }, isLoading: false }),
  useDeleteTenantImage: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTenantStorageStatus: () => ({ data: null, isLoading: false }),
  useReconcileTenantStorage: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useVMs', () => ({
  useVMs: () => ({ data: { items: [] }, isLoading: false }),
  useStartVM: () => ({ mutateAsync: vi.fn() }),
  useStopVM: () => ({ mutateAsync: vi.fn() }),
  useRestartVM: () => ({ mutateAsync: vi.fn() }),
  useDeleteVM: () => ({ mutateAsync: vi.fn() }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/tenants/t8']}>
        <TenantDetail />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('the control-plane address on the tenant page', () => {
  it('shows the address with the port, not the address alone', () => {
    cp = { address: '10.199.0.101', api_port: 6443, konnectivity_port: 8132,
           trustd_port: 50001, shared_with: [], source: 'service' };
    renderPage();

    expect(screen.getByText('10.199.0.101:6443')).toBeInTheDocument();
  });

  it('shows the trustd port, which is the whole Talos join path', () => {
    renderPage();

    expect(screen.getByText(/trustd 50001/)).toBeInTheDocument();
  });

  it('warns when the address is shared, because then the port is the identity', () => {
    cp = { address: '10.199.0.100', api_port: 20000, konnectivity_port: 20001,
           trustd_port: null, shared_with: ['t3'], source: 'service' };
    renderPage();

    expect(screen.getByText('10.199.0.100:20000')).toBeInTheDocument();
    expect(screen.getByText(/shared with t3/)).toBeInTheDocument();
  });

  it('says plainly when a tenant has no address of its own', () => {
    cp = { address: 'tal1.10.198.175.200.nip.io', api_port: 443,
           konnectivity_port: null, trustd_port: null, shared_with: [],
           source: 'ingress' };
    renderPage();

    expect(screen.getByText(/no address of its own/i)).toBeInTheDocument();
  });
});

describe('the talosctl card', () => {
  it('is offered for a Talos tenant', () => {
    // Restore a normal control-plane address; the OS is what gates the card.
    cp = { address: '10.199.0.101', api_port: 6443, konnectivity_port: 8132,
           trustd_port: 50001, shared_with: [], source: 'service' };
    workerOs = 'talos';
    renderPage();

    expect(screen.getByText(/talosctl access/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download talosconfig/i }))
      .toBeInTheDocument();
  });

  it('is absent for a cloud-init tenant', () => {
    // Those nodes run no apid at all — a credential that connects to nothing
    // is worse than no button.
    workerOs = 'cloud-init';
    renderPage();

    expect(screen.queryByText(/talosctl access/i)).not.toBeInTheDocument();
  });

  it('names the first command worth running', () => {
    // "check the clock before the network" is the first troubleshooting entry,
    // and this is where someone stands when they need it.
    workerOs = 'talos';
    renderPage();

    expect(screen.getByText(/talosctl --talosconfig .* time/)).toBeInTheDocument();
  });

  it('says the credential expires', () => {
    workerOs = 'talos';
    renderPage();

    expect(screen.getByText(/24 hours/i)).toBeInTheDocument();
  });
});
