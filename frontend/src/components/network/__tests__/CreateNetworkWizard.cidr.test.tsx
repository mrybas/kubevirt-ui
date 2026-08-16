/**
 * An untouched CIDR field means auto-allocation.
 *
 * The wizard's initial state was `vpcSubnetCidr: '10.100.0.0/24'` — the first
 * quick-select value — while the label read "optional — auto-allocated if
 * empty". So every VPC created without touching the field asked for the same
 * hard-coded /24: the first one took it, and the second was refused on the
 * cluster with
 *
 *   CIDR 10.100.0.0/24 overlaps existing subnet(s): acme-net-default
 *
 * and the review's own "(auto-allocated)" branch could never be reached.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CreateNetworkWizard } from '../CreateNetworkWizard';

const createVpc = vi.fn().mockResolvedValue({ name: 'acme-net' });

vi.mock('@/hooks/useVpcs', () => ({
  useCreateVpc: () => ({ mutateAsync: createVpc, isPending: false }),
}));
vi.mock('@/api/vpcs', () => ({ addVpcPeering: vi.fn() }));
vi.mock('@/hooks/useNetwork', () => ({
  useCreateProviderNetwork: () => ({ mutateAsync: vi.fn() }),
  useCreateVlan: () => ({ mutateAsync: vi.fn() }),
  useCreateSubnet: () => ({ mutateAsync: vi.fn() }),
  useSubnets: () => ({ data: [] }),
  useProviderNetworks: () => ({ data: [] }),
  useVlans: () => ({ data: [] }),
}));
vi.mock('@/api/cluster', () => ({ listNodes: () => Promise.resolve({ items: [], total: 0 }) }));
vi.mock('@/hooks/useFolders', () => ({
  useFoldersFlat: () => ({ data: { items: [] } }),
  useFolders: () => ({ data: { items: [] } }),
}));
vi.mock('@/hooks/useNamespaces', () => ({ useNamespaces: () => ({ data: { items: [] } }) }));

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CreateNetworkWizard onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

async function walkToVpcStep(name = 'acme-net') {
  fireEvent.click(await screen.findByText('VPC Network'));
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
  fireEvent.change(await screen.findByPlaceholderText('my-vpc'), { target: { value: name } });
}

beforeEach(() => createVpc.mockClear());

describe('subnet CIDR', () => {
  it('starts empty, as the label promises', async () => {
    renderWizard();
    await walkToVpcStep();
    expect((screen.getByPlaceholderText('10.100.0.0/24') as HTMLInputElement).value).toBe('');
  });

  it('the review says the CIDR will be allocated', async () => {
    renderWizard();
    await walkToVpcStep();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    expect(await screen.findByText('(auto-allocated)')).toBeInTheDocument();
  });

  it('sends no subnet_cidr when it was never typed', async () => {
    renderWizard();
    await walkToVpcStep();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(await screen.findByRole('button', { name: /create vpc/i }));

    await waitFor(() => expect(createVpc).toHaveBeenCalled());
    expect(createVpc.mock.calls[0][0].subnet_cidr).toBeUndefined();
  });

  it('still sends a CIDR the operator typed', async () => {
    // The fixed quick-select chips are gone: they were constants from another
    // network that overrode the allocator, and the first one collided with an
    // existing subnet on the lab. Typing still wins over auto-allocation.
    renderWizard();
    await walkToVpcStep();
    fireEvent.change(screen.getByPlaceholderText('10.100.0.0/24'), {
      target: { value: '10.200.0.0/24' },
    });
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(await screen.findByRole('button', { name: /create vpc/i }));

    await waitFor(() => expect(createVpc).toHaveBeenCalled());
    expect(createVpc.mock.calls[0][0].subnet_cidr).toBe('10.200.0.0/24');
  });
});
