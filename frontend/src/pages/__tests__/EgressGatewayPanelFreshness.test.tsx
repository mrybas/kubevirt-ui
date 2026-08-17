/**
 * Detach then attach again, without reloading the page.
 *
 * Measured on the lab: detaching t3-vpc from `shared-egress` produced the
 * success toast and the table row correctly went to "1 VPC" — but the open
 * detail panel still listed t3-vpc, because the panel held the gateway
 * *object* captured when it opened. The Attach form builds its candidate list
 * by subtracting that object's attached VPCs from all VPCs, so the freshly
 * detached one still counted as attached and the combobox came up empty under
 * "All VPCs are already attached".
 *
 * The fix is to hold the gateway's *name* and resolve the object from the
 * query cache on every render, so the invalidation the detach already fires
 * reaches the panel and the form and not only the table. The API layer is
 * mocked here rather than the hooks, so the real invalidation runs.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import EgressGateways from '../EgressGateways';
import type { EgressGateway } from '@/types/egress';

function gateway(vpcs: [string, string][]): EgressGateway {
  return {
    name: 'shared-egress',
    gw_vpc_name: 'egw-shared-egress',
    gw_vpc_cidr: '10.199.128.0/24',
    transit_cidr: '10.199.129.0/24',
    macvlan_subnet: 'external',
    replicas: 2,
    bfd_enabled: false,
    node_selector: {},
    exclude_ips: [],
    attached_vpcs: vpcs.map(([vpc, cidr]) => ({
      vpc_name: vpc,
      subnet_name: `${vpc}-default`,
      cidr,
      transit_ip: '',
      peering_name: `${vpc}-peer`,
      peering_ok: true,
    })),
    assigned_ips: [],
    ready: true,
    status: null,
  } as EgressGateway;
}

const BOTH: [string, string][] = [['t1-vpc', '10.200.8.0/22'], ['t3-vpc', '10.200.16.0/22']];
const ONLY_T1: [string, string][] = [['t1-vpc', '10.200.8.0/22']];

let attachedNow: [string, string][] = BOTH;

vi.mock('../../api/egress', () => ({
  listEgressGateways: vi.fn(async () => ({ items: [gateway(attachedNow)], total: 1 })),
  detachVpc: vi.fn(async () => {
    attachedNow = ONLY_T1;
    return { detached: true };
  }),
  attachVpc: vi.fn(async () => ({})),
  createEgressGateway: vi.fn(),
  deleteEgressGateway: vi.fn(),
  getEgressGateway: vi.fn(),
  suggestGatewayCidrs: vi.fn().mockResolvedValue({
    gw_vpc_cidr: '10.199.128.0/24',
    transit_cidr: '10.199.129.0/24',
  }),
}));

vi.mock('../../api/network', () => ({ listSubnets: vi.fn().mockResolvedValue([]) }));

vi.mock('@/hooks/useVpcs', () => ({
  useVpcs: () => ({ data: { items: [{ name: 't1-vpc' }, { name: 't3-vpc' }] } }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <EgressGateways />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  attachedNow = BOTH;
});

describe('the detail panel after a detach', () => {
  it('stops listing the VPC it just detached', async () => {
    renderPage();

    fireEvent.click((await screen.findByText('shared-egress')).closest('tr')!);
    const panel = await screen.findByRole('dialog');
    expect(await within(panel).findByText('t3-vpc')).toBeInTheDocument();

    fireEvent.click(within(panel).getAllByTitle(/detach/i)[1]!);

    await waitFor(() =>
      expect(within(screen.getByRole('dialog')).queryByText('t3-vpc')).not.toBeInTheDocument(),
    );
    // …and t1-vpc is untouched, so this is a refresh and not an empty panel.
    expect(within(screen.getByRole('dialog')).getByText('t1-vpc')).toBeInTheDocument();
  });

  it('offers the detached VPC again in the attach form', async () => {
    // The whole point: detach → attach in one session. Before the fix this
    // combobox was empty and said every VPC was already attached.
    renderPage();

    fireEvent.click((await screen.findByText('shared-egress')).closest('tr')!);
    const panel = await screen.findByRole('dialog');
    fireEvent.click(within(panel).getAllByTitle(/detach/i)[1]!);
    await waitFor(() =>
      expect(within(screen.getByRole('dialog')).queryByText('t3-vpc')).not.toBeInTheDocument(),
    );

    fireEvent.click(within(screen.getByRole('dialog')).getByText(/attach vpc/i));

    const select = await screen.findByRole('combobox');
    await waitFor(() =>
      expect(within(select).getByRole('option', { name: 't3-vpc' })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/all vpcs are already attached/i)).not.toBeInTheDocument();
  });
});
