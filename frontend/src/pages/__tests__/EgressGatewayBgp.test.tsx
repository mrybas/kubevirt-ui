/**
 * A BGP-peered egress gateway could not be created from the screen.
 *
 * `VpcEgressGateway.spec.bgpConf` exists in the CRD, `egress_gateway.create`
 * writes it from `bgp_conf`, and `CreateEgressGatewayRequest` declares the
 * field — but the Create dialog rendered no control for it, so every gateway
 * made through the UI came up with `bgp: None` and the VPC's prefix was never
 * announced. Proved on the lab router by creating one through the API
 * instead: session Established, and
 *
 *     10.100.0.0/24 via 10.198.190.220 dev eth2 proto bird
 *
 * in its kernel table, with pings to a VM inside the VPC answering.
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

vi.mock('@/hooks/useBgp', () => ({
  useBgpConfs: () => ({
    data: {
      items: [{
        name: 'lab-gateway', local_asn: 65001, peer_asn: 65000,
        neighbours: ['10.198.191.254'], graceful_restart: true,
        hold_time: '30s', keepalive_time: '10s', router_id: '',
      }],
      total: 1,
    },
  }),
}));

function renderModal() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CreateEgressGatewayModal
        subnets={[{ name: 'ext-sub', cidr: '10.198.176.0/20' }]}
        onClose={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

async function fillAndSubmit(pickBgp?: string) {
  fireEvent.change(await screen.findByPlaceholderText(/shared-egress/i), {
    target: { value: 'labgw' },
  });
  const selects = [...document.querySelectorAll('select')];
  const pick = (value: string) => selects.find((s) =>
    [...s.querySelectorAll('option')].some((o) => o.value === value),
  );
  fireEvent.change(pick('ext-sub')!, { target: { value: 'ext-sub' } });
  if (pickBgp !== undefined) {
    fireEvent.change(pick('lab-gateway')!, { target: { value: pickBgp } });
  }
  // The modal marks its wrapper aria-hidden, so role queries skip inside it.
  fireEvent.click(screen.getByText('Create Gateway').closest('button')!);
}

describe('creating an egress gateway with BGP', () => {
  beforeEach(() => createGateway.mockClear());

  it('offers the BgpConfs that exist', async () => {
    renderModal();

    expect(await screen.findByText(/lab-gateway — AS 65001 → 65000/)).toBeInTheDocument();
  });

  it('sends the chosen one, so the VPC actually peers', async () => {
    renderModal();
    await fillAndSubmit('lab-gateway');

    await waitFor(() => expect(createGateway).toHaveBeenCalled());
    expect(createGateway.mock.calls[0]![0]).toMatchObject({ bgp_conf: 'lab-gateway' });
  });

  it('stays optional — no BGP means NAT only, which is the old behaviour', async () => {
    renderModal();
    await fillAndSubmit();

    await waitFor(() => expect(createGateway).toHaveBeenCalled());
    expect(createGateway.mock.calls[0]![0].bgp_conf).toBeUndefined();
  });

  it('explains what the choice does', async () => {
    renderModal();

    expect(
      await screen.findByText(/announces the CIDRs of every attached VPC/i),
    ).toBeInTheDocument();
  });
});
