/**
 * "No CIDR overlaps detected" was a promise the form could not keep.
 *
 * Typing 10.199.0.0/24 on a cluster whose `cp-transit` is 10.199.0.0/22 left
 * the banner green and the button enabled. The backend refused it correctly
 * with a 422 — which then rendered *next to* the green banner. The preflight
 * compared the two typed CIDRs against each other and against nothing else,
 * while the backend compares against every Subnet on the cluster. That gap is
 * why U7 looked for two runs like "the backend lets overlaps through".
 *
 * So: same source of truth as `suggest-cidrs`, and no green tick before that
 * comparison has actually happened — silence is honester than a wrong yes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { CreateEgressGatewayModal } from '../EgressGateways';

const createGateway = vi.fn().mockResolvedValue({ name: 'labgw' });

vi.mock('@/hooks/useEgressGateways', () => ({
  useEgressGateways: () => ({ data: { items: [] } }),
  useCreateEgressGateway: () => ({ mutateAsync: createGateway, isPending: false }),
  useDeleteEgressGateway: () => ({ mutateAsync: vi.fn() }),
  useAttachVpc: () => ({ mutateAsync: vi.fn() }),
  useDetachVpc: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock('@/hooks/useVpcs', () => ({ useVpcs: () => ({ data: { items: [] } }) }));
vi.mock('@/hooks/useBgp', () => ({ useBgpConfs: () => ({ data: { items: [], total: 0 } }) }));

vi.mock('../../api/egress', () => ({
  suggestGatewayCidrs: vi.fn().mockResolvedValue({
    gw_vpc_cidr: '10.199.128.0/24',
    transit_cidr: '10.199.129.0/24',
  }),
}));

// The cluster this form is talking to. `cp-transit` is the exact range that
// made the old check lie.
const SUBNETS = [
  { name: 'cp-transit', cidr_block: '10.199.0.0/22', vlan: 'vlan300', used_as_transit: true },
  { name: 'external', cidr_block: '10.199.4.0/22', vlan: 'vlan310', used_as_transit: false },
];

vi.mock('../../api/network', () => ({
  listSubnets: vi.fn(async () => SUBNETS),
}));

function renderModal() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CreateEgressGatewayModal
        subnets={[{ name: 'external', cidr: '10.199.4.0/22' }]}
        onClose={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

async function typeCidrs(gw: string, transit: string) {
  const inputs = [...document.querySelectorAll('input')].filter(
    (i) => i.value.includes('/') || i.placeholder?.includes('/'),
  );
  fireEvent.change(inputs[0]!, { target: { value: gw } });
  fireEvent.change(inputs[1]!, { target: { value: transit } });
}

beforeEach(() => createGateway.mockClear());

describe('the CIDR preflight', () => {
  it('goes red on a range the cluster already uses, before any submit', async () => {
    renderModal();
    await screen.findByDisplayValue('10.199.128.0/24');

    await typeCidrs('10.199.0.0/24', '10.199.129.0/24');

    expect(await screen.findByText(/overlaps subnet cp-transit \(10\.199\.0\.0\/22\)/i))
      .toBeInTheDocument();
    expect(screen.queryByText(/no overlap with the transit range/i)).not.toBeInTheDocument();
    expect(createGateway).not.toHaveBeenCalled();
  });

  it('will not submit a collision the backend would 422', async () => {
    renderModal();
    await screen.findByDisplayValue('10.199.128.0/24');
    fireEvent.change(await screen.findByPlaceholderText(/shared-egress/i), {
      target: { value: 'labgw' },
    });
    // Fill everything else in, so a disabled button can only mean the
    // collision — otherwise this passes for the wrong reason.
    const externalSelect = [...document.querySelectorAll('select')].find((s) =>
      [...s.options].some((o) => o.value === 'external'),
    );
    fireEvent.change(externalSelect!, { target: { value: 'external' } });

    const submit = screen.getByRole('button', { name: /create gateway/i });
    await waitFor(() => expect(submit).toBeEnabled());

    await typeCidrs('10.199.0.0/24', '10.199.129.0/24');

    await waitFor(() => expect(submit).toBeDisabled());
  });

  it('says yes only about ranges it has actually compared', async () => {
    renderModal();
    await screen.findByDisplayValue('10.199.128.0/24');

    // The suggested pair, which by construction collides with nothing.
    expect(await screen.findByText(/no overlap with the transit range or any subnet/i))
      .toBeInTheDocument();
  });

  it('still catches the two typed ranges overlapping each other', async () => {
    renderModal();
    await screen.findByDisplayValue('10.199.128.0/24');

    await typeCidrs('10.198.0.0/24', '10.198.0.0/25');

    expect(await screen.findByText(/must use separate address ranges/i)).toBeInTheDocument();
  });
});
