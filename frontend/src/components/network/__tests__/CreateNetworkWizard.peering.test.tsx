/**
 * The host-cluster-access toggle has to do something.
 *
 * The peering step rendered, the toggle defaulted to on, and the review said
 * "Host Cluster Peering: Enabled" — while the create handler ended at:
 *
 *     // TODO: if peering enabled, create peering with ovn-cluster (default VPC)
 *
 * So every VPC created through the wizard reported connectivity it did not
 * have. Verified on the cluster: `Vpc/t1` came up with no `spec.vpcPeerings`
 * at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CreateNetworkWizard } from '../CreateNetworkWizard';

const createVpc = vi.fn().mockResolvedValue({ name: 't1' });
const addVpcPeering = vi.fn().mockResolvedValue({ name: 't1' });

vi.mock('@/hooks/useVpcs', () => ({
  useCreateVpc: () => ({ mutateAsync: createVpc, isPending: false }),
}));

vi.mock('@/api/vpcs', () => ({
  addVpcPeering: (...args: any[]) => addVpcPeering(...args),
}));

vi.mock('@/hooks/useNetwork', () => ({
  useCreateProviderNetwork: () => ({ mutateAsync: vi.fn() }),
  useCreateVlan: () => ({ mutateAsync: vi.fn() }),
  useCreateSubnet: () => ({ mutateAsync: vi.fn() }),
  useSubnets: () => ({ data: [] }),
  useProviderNetworks: () => ({ data: [] }),
  useVlans: () => ({ data: [] }),
}));

vi.mock('@/api/cluster', () => ({
  listNodes: () => Promise.resolve({ items: [], total: 0 }),
}));

vi.mock('@/hooks/useFolders', () => ({
  useFoldersFlat: () => ({ data: { items: [] } }),
  useFolders: () => ({ data: { items: [] } }),
}));

vi.mock('@/hooks/useNamespaces', () => ({
  useNamespaces: () => ({ data: { items: [] } }),
}));

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CreateNetworkWizard onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

/** Type → VPC → name/CIDR → Peering step. */
async function walkToPeering(name = 't1') {
  fireEvent.click(await screen.findByText('VPC Network'));
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
  fireEvent.change(await screen.findByPlaceholderText('my-vpc'), {
    target: { value: name },
  });
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
  await screen.findByText('Enable host cluster access');
}

describe('host cluster access', () => {
  beforeEach(() => {
    createVpc.mockClear();
    addVpcPeering.mockClear();
  });

  it('is off by default — the wizard never peered anything, so on would be a new behaviour', async () => {
    renderWizard();
    await walkToPeering();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    fireEvent.click(await screen.findByRole('button', { name: /create vpc/i }));

    await waitFor(() => expect(createVpc).toHaveBeenCalled());
    expect(addVpcPeering).not.toHaveBeenCalled();
  });

  it('peers with the host cluster VPC when switched on', async () => {
    renderWizard();
    await walkToPeering();
    // The toggle sits next to its label in the peering step.
    const toggles = screen.getAllByRole('button').filter(
      (b) => b.className.includes('rounded-full'),
    );
    fireEvent.click(toggles[toggles.length - 1]);
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    fireEvent.click(await screen.findByRole('button', { name: /create vpc/i }));

    await waitFor(() =>
      expect(addVpcPeering).toHaveBeenCalledWith('t1', { remote_vpc: 'ovn-cluster' }),
    );
  });

  it('says so when the VPC was created but the peering failed', async () => {
    addVpcPeering.mockRejectedValueOnce(new Error('no room for a link'));
    renderWizard();
    await walkToPeering();
    const toggles = screen.getAllByRole('button').filter(
      (b) => b.className.includes('rounded-full'),
    );
    fireEvent.click(toggles[toggles.length - 1]);
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    fireEvent.click(await screen.findByRole('button', { name: /create vpc/i }));

    expect(await screen.findByText(/peering it with ovn-cluster failed/i)).toBeInTheDocument();
    expect(screen.getByText(/no room for a link/i)).toBeInTheDocument();
  });

  it('reviews the isolation setting, not only the peering one', async () => {
    // Isolation is the security-relevant switch in this wizard and the review
    // did not mention it at all.
    renderWizard();
    await walkToPeering();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    expect(await screen.findByText(/Isolated:/)).toBeInTheDocument();
    expect(
      screen.getByText(/traffic to and from other tenant VPCs is blocked/i),
    ).toBeInTheDocument();
  });
});
