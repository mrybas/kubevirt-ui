/**
 * The VPC page stopped offering the attach; this page kept offering it.
 *
 * A VPC on the routed plane leaves through its own router leg. Attaching it to
 * a hub rewrites its default route to the gateway's transit address and takes
 * it off that leg, so traffic that works today stops. That is why the "Configure
 * egress gateway" button disappeared from the VPC page — and the same action,
 * from the other direction, was still one click away here with no check at all.
 *
 * The backend refuses it now (422). This is the half that means nobody has to
 * discover the refusal by trying it.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AttachVpcModal } from '../EgressGateways';

const subnet = (cidr: string) => ({
  name: `${cidr}-subnet`, cidr_block: cidr, gateway: '', available_ips: 10,
  using_ips: 1, protocol: 'IPv4', is_default: true,
});

let vpcs: any[] = [];

vi.mock('@/hooks/useVpcs', () => ({
  useVpcs: () => ({ data: { items: vpcs, total: vpcs.length }, isLoading: false }),
}));

vi.mock('@/hooks/useEgressGateways', () => ({
  useAttachVpc: () => ({ mutateAsync: vi.fn(), isPending: false, isError: false }),
}));

function renderModal(existing: string[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AttachVpcModal
        gatewayName="shared-egress"
        existingVpcNames={existing}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
}

describe('what the attach dialog offers', () => {
  it('does not offer a VPC that already egresses through its own leg', () => {
    vpcs = [
      { name: 'b3v', subnets: [subnet('10.200.36.0/22')], routed_egress: true },
      { name: 't1-vpc', subnets: [subnet('10.200.8.0/22')], routed_egress: false },
    ];
    renderModal();

    const options = screen.getAllByRole('option').map((o) => o.textContent);
    expect(options.some((o) => o?.includes('t1-vpc'))).toBe(true);
    expect(options.some((o) => o?.includes('b3v'))).toBe(false);
  });

  it('says why they are missing instead of just hiding them', () => {
    // A VPC absent with no explanation reads as a bug, and invites someone to
    // go and write the attach by hand.
    vpcs = [
      { name: 'b3v', subnets: [subnet('10.200.36.0/22')], routed_egress: true },
      { name: 't1-vpc', subnets: [subnet('10.200.8.0/22')], routed_egress: false },
    ];
    renderModal();

    expect(screen.getByText(/1 VPC not offered/)).toBeInTheDocument();
    expect(screen.getByText(/own router leg/i)).toBeInTheDocument();
  });

  it('distinguishes "all attached" from "all routed"', () => {
    vpcs = [{ name: 'b3v', subnets: [subnet('10.200.36.0/22')], routed_egress: true }];
    renderModal();

    expect(screen.getByText(/Nothing to attach/i)).toBeInTheDocument();
    expect(screen.queryByText(/All VPCs are already attached/i)).not.toBeInTheDocument();
  });

  it('still says "already attached" when that is the real reason', () => {
    vpcs = [{ name: 't1-vpc', subnets: [subnet('10.200.8.0/22')], routed_egress: false }];
    renderModal(['t1-vpc']);

    expect(screen.getByText(/All VPCs are already attached/i)).toBeInTheDocument();
  });

  it('offers an ordinary VPC that has no egress yet', () => {
    vpcs = [{ name: 'fresh', subnets: [subnet('10.200.60.0/22')] }];
    renderModal();

    expect(screen.getAllByRole('option').some((o) => o.textContent?.includes('fresh')))
      .toBe(true);
  });
});
