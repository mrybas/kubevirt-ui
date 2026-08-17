/**
 * A VPC's folder/environment used to be write-once.
 *
 * It is picked in the create wizard — which did not show it on the Review step
 * — and after that the only correction was deleting the VPC and everything in
 * it. The tenant-create wizard meanwhile advises scoping a VPC to the folder
 * you are working in, advice whose action did not exist.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import VPCDetail from '../VPCDetail';

const setScope = vi.fn().mockResolvedValue({});

let vpc: any = {
  name: 't1-vpc',
  ready: true,
  subnets: [{ name: 't1-vpc-default', cidr_block: '10.200.8.0/22' }],
  peerings: [],
  namespaces: [],
  folder: 'old-folder',
  environment: 'dev',
};

vi.mock('@/hooks/useVpcs', () => ({
  useVpc: () => ({ data: vpc, isLoading: false, refetch: vi.fn() }),
  useDeleteVpc: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAddVpcPeering: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRemoveVpcPeering: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useVpcRoutes: () => ({ data: { routes: [] }, isLoading: false }),
  useUpdateVpcRoutes: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useVpcDns: () => ({ data: null, isLoading: false }),
  useUpdateVpcDns: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRecreateVpcDns: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useVpcDnsPolicy: () => ({ data: null, isLoading: false }),
  useUpdateVpcDnsPolicy: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRecreateVpcDnsPolicy: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDisableVpcDnsPolicy: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useSetVpcScope: () => ({ mutateAsync: setScope, isPending: false }),
}));

vi.mock('@/hooks/useEgressGateways', () => ({
  useEgressGateways: () => ({ data: { items: [] } }),
  useDetachVpc: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useFolders', () => ({
  useFoldersFlat: () => ({
    data: {
      items: [
        { name: 'old-folder', environments: [{ name: 'old-folder-dev', environment: 'dev' }] },
        {
          name: 'platform',
          environments: [
            { name: 'platform-prod', environment: 'prod' },
            { name: 'platform-staging', environment: 'staging' },
          ],
        },
      ],
    },
  }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/network/vpcs/t1-vpc']}>
        <Routes>
          <Route path="/network/vpcs/:name" element={<VPCDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => setScope.mockClear());

describe('what the page says about egress', () => {
  it('does not call a routed VPC "no internet"', async () => {
    // Measured: b3v had working internet through its own leg while the page
    // read "None (no internet)" — next to a Configure button that would have
    // attached a hub and rewritten the route carrying its traffic.
    vpc = { ...vpc, routed_egress: true, egress_next_hop: '10.199.4.11',
            announced_cidrs: ['10.200.36.0/22'] };
    renderPage();

    expect(await screen.findByText(/routed — via its own leg/i)).toBeInTheDocument();
    expect(screen.getByText('10.199.4.11')).toBeInTheDocument();
    expect(screen.queryByText(/no internet/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/configure/i)).not.toBeInTheDocument();
  });

  it('shows what the upstream router was told', async () => {
    vpc = { ...vpc, routed_egress: true, egress_next_hop: '10.199.4.11',
            announced_cidrs: ['10.200.36.0/22'] };
    renderPage();

    expect(await screen.findByText(/10\.200\.36\.0\/22/)).toBeInTheDocument();
  });

  it('still offers a gateway to a VPC that has neither', async () => {
    vpc = { ...vpc, routed_egress: false };
    renderPage();

    expect(await screen.findByText(/none \(no internet\)/i)).toBeInTheDocument();
  });
});

describe('the VPC scope editor', () => {
  it('offers the short environment name, not the namespace', async () => {
    // VPCs are labelled `kubevirt-ui.io/environment=dev` and the tenant wizard
    // filters on that; writing the namespace `platform-prod` would scope the
    // VPC to a value nothing matches. The folders API returns both fields and
    // it is easy to pick the wrong one — this caught it in review.
    renderPage();

    fireEvent.click(await screen.findByText('Change'));
    fireEvent.change([...document.querySelectorAll('select')][0]!, {
      target: { value: 'platform' },
    });

    const envSelect = [...document.querySelectorAll('select')][1]!;
    const values = [...envSelect.options].map((o) => o.value);
    expect(values).toContain('prod');
    expect(values).not.toContain('platform-prod');
  });

  it('shows the current scope', async () => {
    renderPage();

    expect(await screen.findByText('Scope')).toBeInTheDocument();
    expect(screen.getByText(/old-folder \/ dev/)).toBeInTheDocument();
  });

  it('moves the VPC to another folder and environment', async () => {
    renderPage();

    fireEvent.click(await screen.findByText('Change'));
    const selects = [...document.querySelectorAll('select')];
    fireEvent.change(selects[0]!, { target: { value: 'platform' } });
    fireEvent.change(selects[1]!, { target: { value: 'prod' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(setScope).toHaveBeenCalledWith({
      folder: 'platform',
      environment: 'prod',
    }));
  });

  it('clears the environment when the folder changes', async () => {
    // A folder's environments are its own; carrying `dev` into a folder that
    // has no `dev` would scope the VPC to nothing.
    renderPage();

    fireEvent.click(await screen.findByText('Change'));
    const selects = [...document.querySelectorAll('select')];
    fireEvent.change(selects[0]!, { target: { value: 'platform' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(setScope).toHaveBeenCalledWith({
      folder: 'platform',
      environment: null,
    }));
  });

  it('unscopes the VPC entirely', async () => {
    renderPage();

    fireEvent.click(await screen.findByText('Change'));
    fireEvent.change([...document.querySelectorAll('select')][0]!, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(setScope).toHaveBeenCalledWith({
      folder: null,
      environment: null,
    }));
  });
});
