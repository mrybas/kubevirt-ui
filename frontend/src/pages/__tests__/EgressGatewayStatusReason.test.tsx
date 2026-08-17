/**
 * `Not Ready` and `Degraded` have to say why, on the page.
 *
 * The backend already words both causes precisely — an announcement lag that
 * leaves a freshly attached VPC without return traffic, an `AcquireAddressFailed`
 * on an exhausted external subnet — and the list showed neither. The reason
 * lived in a `title` attribute, which is unreachable on touch and invisible to
 * anyone scanning the table, so the answer stayed behind `kubectl describe pod`
 * for people who have no cluster credentials.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import EgressGateways from '../EgressGateways';
import type { EgressGateway } from '@/types/egress';

const LAG =
  '10.200.16.0/22 not announced yet — the gateway pods still carry the previous ' +
  'route set. Traffic from these VPCs leaves but cannot come back until the pods roll.';
const NO_ADDRESS =
  'shared-egress-0: AcquireAddressFailed — no available IP in subnet external';

function gateway(over: Partial<EgressGateway> = {}): EgressGateway {
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
    attached_vpcs: [],
    assigned_ips: [],
    ready: true,
    status: null,
    ...over,
  } as EgressGateway;
}

let items: EgressGateway[] = [];

vi.mock('@/hooks/useEgressGateways', () => ({
  useEgressGateways: () => ({ data: { items, total: items.length }, isLoading: false, refetch: vi.fn() }),
  useCreateEgressGateway: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteEgressGateway: () => ({ mutateAsync: vi.fn() }),
  useAttachVpc: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDetachVpc: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useVpcs', () => ({ useVpcs: () => ({ data: { items: [] } }) }));

vi.mock('../../api/network', () => ({ listSubnets: vi.fn().mockResolvedValue([]) }));

function renderList(gateways: EgressGateway[]) {
  items = gateways;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <EgressGateways />
    </QueryClientProvider>,
  );
}

describe('egress gateway status reasons', () => {
  it('spells out an announcement lag next to the Degraded badge', async () => {
    renderList([gateway({ ready: true, degraded_reason: LAG })]);

    expect(await screen.findByText('Degraded')).toBeInTheDocument();
    expect(screen.getByText(new RegExp('not announced yet'))).toBeInTheDocument();
  });

  it('spells out why a gateway has not come up', async () => {
    renderList([gateway({ ready: false, not_ready_reason: NO_ADDRESS })]);

    expect(await screen.findByText('Not Ready')).toBeInTheDocument();
    expect(screen.getByText(/AcquireAddressFailed/)).toBeInTheDocument();
    expect(screen.getByText(/no available IP in subnet external/)).toBeInTheDocument();
  });

  it('says nothing beside a healthy gateway', async () => {
    renderList([gateway({ ready: true })]);

    expect(await screen.findByText('Ready')).toBeInTheDocument();
    expect(screen.queryByText(/not announced yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/AcquireAddressFailed/)).not.toBeInTheDocument();
  });

  it('does not leave a stale cause under a Ready badge', async () => {
    // The backend drops not_ready_reason once ready flips; the render must not
    // resurrect it from an older field either.
    renderList([gateway({ ready: true, not_ready_reason: NO_ADDRESS })]);

    expect(await screen.findByText('Ready')).toBeInTheDocument();
    expect(screen.queryByText(/AcquireAddressFailed/)).not.toBeInTheDocument();
  });
});
