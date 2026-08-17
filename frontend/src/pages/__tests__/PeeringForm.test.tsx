/**
 * Peering was a free-text box.
 *
 * It accepted a name that does not exist, a name already peered, and the VPC's
 * own name; and it said nothing about the far end, so an operator had every
 * reason to go and add the mirror by hand — which is how a list-typed field
 * gets merge-patched and a working leg gets wiped.
 *
 * Two refusals here are not cosmetic, because both produce objects that look
 * healthy and carry no packets:
 *
 *  * overlapping prefixes — each router already has a more specific route for
 *    the other's range, pointing at itself;
 *  * a remote VPC with no subnet — the backend refuses this one too, but only
 *    after the operator has committed; there is nothing to route to.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import VPCDetail from '../VPCDetail';

const subnet = (cidr: string) => ({
  name: `${cidr}-subnet`, cidr_block: cidr, gateway: '', available_ips: 10,
  using_ips: 1, protocol: 'IPv4', is_default: true,
});

let vpcList: any[] = [];
let ownPeerings: any[] = [];

vi.mock('@/hooks/useVpcs', () => ({
  useVpcs: () => ({ data: { items: vpcList, total: vpcList.length } }),
  useVpc: () => ({
    data: {
      name: 'b3v', tenant: null, enable_nat_gateway: false,
      default_subnet: 'b3v-default', subnets: [subnet('10.200.36.0/22')],
      peerings: ownPeerings, static_routes: [], namespaces: [], ready: true,
      conditions: [],
    },
    isLoading: false, refetch: vi.fn(),
  }),
  useDeleteVpc: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAddVpcPeering: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRemoveVpcPeering: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useVpcRoutes: () => ({ data: { items: [] }, isLoading: false }),
  useUpdateVpcRoutes: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useVpcDns: () => ({ data: null, isLoading: false }),
  useUpdateVpcDns: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRecreateVpcDns: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useVpcDnsPolicy: () => ({ data: null, isLoading: false }),
  useUpdateVpcDnsPolicy: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRecreateVpcDnsPolicy: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDisableVpcDnsPolicy: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useSetVpcScope: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useEnvironments', () => ({
  useEnvironments: () => ({ data: { items: [] } }),
}), { virtual: true } as any);

function renderPeerings() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/network/vpcs/b3v?tab=peerings']}>
        <VPCDetail />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('choosing what to peer with', () => {
  it('offers real VPCs and never the VPC itself', async () => {
    vpcList = [
      { name: 'b3v', subnets: [subnet('10.200.36.0/22')], peerings: [] },
      { name: 'b3w', subnets: [subnet('10.200.40.0/22')], peerings: [] },
    ];
    ownPeerings = [];
    renderPeerings();

    const { default: userEvent } = await import('@testing-library/user-event');
    await userEvent.click(screen.getByRole('button', { name: /peerings/i }));
    await userEvent.click(screen.getByRole('button', { name: /add peering/i }));

    const options = screen.getAllByRole('option').map((o) => o.textContent);
    expect(options.some((o) => o?.includes('b3w'))).toBe(true);
    expect(options.some((o) => o?.startsWith('b3v'))).toBe(false);
  });

  it('does not offer a VPC that is already peered', async () => {
    vpcList = [
      { name: 'b3w', subnets: [subnet('10.200.40.0/22')], peerings: [] },
      { name: 't8v', subnets: [subnet('10.200.24.0/22')], peerings: [] },
    ];
    ownPeerings = [{ remote_vpc: 'b3w', link_cidr: '10.199.200.0/30',
                     local_connect_ip: '10.199.200.1',
                     remote_connect_ip: '10.199.200.2' }];
    renderPeerings();

    const { default: userEvent } = await import('@testing-library/user-event');
    await userEvent.click(screen.getByRole('button', { name: /peerings/i }));
    await userEvent.click(screen.getByRole('button', { name: /add peering/i }));

    const options = screen.getAllByRole('option').map((o) => o.textContent);
    expect(options.some((o) => o?.includes('t8v'))).toBe(true);
    expect(options.filter((o) => o?.startsWith('b3w'))).toHaveLength(0);
  });

  it('shows each candidate with its prefix, since that is what decides', async () => {
    vpcList = [{ name: 'b3w', subnets: [subnet('10.200.40.0/22')], peerings: [] }];
    ownPeerings = [];
    renderPeerings();

    const { default: userEvent } = await import('@testing-library/user-event');
    await userEvent.click(screen.getByRole('button', { name: /peerings/i }));
    await userEvent.click(screen.getByRole('button', { name: /add peering/i }));

    expect(screen.getByRole('option', { name: /b3w — 10\.200\.40\.0\/22/ }))
      .toBeInTheDocument();
  });

  it('marks a candidate that has no subnet yet', async () => {
    vpcList = [{ name: 'empty', subnets: [], peerings: [] }];
    ownPeerings = [];
    renderPeerings();

    const { default: userEvent } = await import('@testing-library/user-event');
    await userEvent.click(screen.getByRole('button', { name: /peerings/i }));
    await userEvent.click(screen.getByRole('button', { name: /add peering/i }));

    expect(screen.getByRole('option', { name: /empty — no subnet yet/ }))
      .toBeInTheDocument();
  });
});

describe('the two peerings that would build cleanly and carry nothing', () => {
  async function openAndPick(name: string) {
    const { default: userEvent } = await import('@testing-library/user-event');
    renderPeerings();
    await userEvent.click(screen.getByRole('button', { name: /peerings/i }));
    await userEvent.click(screen.getByRole('button', { name: /add peering/i }));
    await userEvent.selectOptions(screen.getByRole('combobox'), name);
  }

  it('refuses overlapping prefixes and says what would happen', async () => {
    vpcList = [{ name: 'clash', subnets: [subnet('10.200.36.0/24')], peerings: [] }];
    ownPeerings = [];
    await openAndPick('clash');

    expect(screen.getByText(/Overlapping prefixes/i)).toBeInTheDocument();
    expect(screen.getByText(/more specific route/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Add$/ })).toBeDisabled();
  });

  it('allows a peering whose prefixes only look similar', async () => {
    // 10.200.36.0/22 vs 10.200.40.0/22 — adjacent, not overlapping. Refusing
    // this would block the pair the invariants are meant to be measured on.
    vpcList = [{ name: 'b3w', subnets: [subnet('10.200.40.0/22')], peerings: [] }];
    ownPeerings = [];
    await openAndPick('b3w');

    expect(screen.queryByText(/Overlapping prefixes/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Add$/ })).not.toBeDisabled();
  });

  it('refuses a remote with no subnet', async () => {
    vpcList = [{ name: 'empty', subnets: [], peerings: [] }];
    ownPeerings = [];
    await openAndPick('empty');

    expect(screen.getByText(/nothing to route to/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Add$/ })).toBeDisabled();
  });

  it('says the far end is written too', async () => {
    // The free-text box gave no such hint, and hand-editing the mirror is how
    // a list-typed field gets merge-patched and a live leg disappears.
    vpcList = [{ name: 'b3w', subnets: [subnet('10.200.40.0/22')], peerings: [] }];
    ownPeerings = [];
    await openAndPick('b3w');

    expect(screen.getByText(/Both ends are written/i)).toBeInTheDocument();
    expect(screen.getByText(/Nothing to add by hand on the far side/i))
      .toBeInTheDocument();
  });
});
