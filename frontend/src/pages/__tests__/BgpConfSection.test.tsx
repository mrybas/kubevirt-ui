/**
 * The create-gateway form told people to make a BgpConf on a page that had no
 * such control.
 *
 * The backend has had GET/PUT/DELETE /bgp/confs all along, and the hooks
 * `useUpsertBgpConf` / `useDeleteBgpConf` sat in useBgp.ts unused — so the only
 * way to get one was to call the API by hand. A gateway created without a
 * BgpConf comes up with `bgp: None` and none of its tenants' prefixes are ever
 * announced, which is invisible until someone checks the upstream router.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import BgpPeering from '../BgpPeering';

const upsert = vi.fn().mockResolvedValue({});
const remove = vi.fn().mockResolvedValue({});

let confs = [
  {
    name: 'lab-gateway-common',
    local_asn: 65010,
    peer_asn: 65000,
    neighbours: ['10.199.4.254'],
    graceful_restart: true,
    hold_time: '30s',
    keepalive_time: '10s',
    router_id: '',
  },
];

let gateways: any[] = [];

vi.mock('@/hooks/useBgp', () => ({
  useSpeakerStatus: () => ({ data: { deployed: false, config: {}, node_labels: [] }, isLoading: false, refetch: vi.fn() }),
  useDeploySpeaker: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateSpeaker: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteSpeaker: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAnnouncements: () => ({ data: [], isLoading: false }),
  useCreateAnnouncement: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteAnnouncement: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useBgpSessions: () => ({ data: [], isLoading: false, refetch: vi.fn() }),
  useBgpConfs: () => ({ data: { items: confs, total: confs.length }, isLoading: false }),
  useUpsertBgpConf: () => ({ mutateAsync: upsert, isPending: false }),
  useDeleteBgpConf: () => ({ mutateAsync: remove, isPending: false }),
}));

vi.mock('@/hooks/useEgressGateways', () => ({
  useEgressGateways: () => ({ data: { items: gateways } }),
}));

vi.mock('@/hooks/useNetwork', () => ({ useSubnets: () => ({ data: { items: [] } }) }));
vi.mock('../../api/bgp', () => ({ getGatewayConfigExamples: vi.fn().mockResolvedValue([]) }));
vi.mock('../../api/cluster', () => ({ listNodes: vi.fn().mockResolvedValue({ items: [] }) }));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BgpPeering />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  upsert.mockClear();
  remove.mockClear();
  gateways = [];
});

describe('the Gateway BGP Config section', () => {
  it('lists the configs a gateway can peer through', async () => {
    renderPage();

    expect(await screen.findByText('Gateway BGP Config')).toBeInTheDocument();
    expect(screen.getByText('lab-gateway-common')).toBeInTheDocument();
    expect(screen.getByText('10.199.4.254')).toBeInTheDocument();
  });

  it('creates one from the page instead of from the API', async () => {
    renderPage();

    fireEvent.click(await screen.findByText(/new config/i));
    const dialog = await screen.findByRole('dialog');

    fireEvent.change(within(dialog).getByPlaceholderText('lab-gateway-common'), {
      target: { value: 'second-gw' },
    });
    fireEvent.change(within(dialog).getByPlaceholderText('10.199.4.254'), {
      target: { value: '10.199.4.253' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }));
    fireEvent.click(within(dialog).getByRole('button', { name: /save/i }));

    await waitFor(() => expect(upsert).toHaveBeenCalled());
    expect(upsert.mock.calls[0]![0]).toMatchObject({
      name: 'second-gw',
      neighbours: ['10.199.4.253'],
    });
  });

  it('will not save a config with no neighbour', async () => {
    // FRR with no neighbour peers with nothing — the gateway would look
    // configured and announce to no one.
    renderPage();

    fireEvent.click(await screen.findByText(/new config/i));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByPlaceholderText('lab-gateway-common'), {
      target: { value: 'second-gw' },
    });

    expect(within(dialog).getByRole('button', { name: /save/i })).toBeDisabled();
  });

  it('refuses to delete a config a gateway is announcing through', async () => {
    gateways = [{ name: 'shared-egress', bgp_conf: 'lab-gateway-common', ready: true, external_ips: [] }];
    renderPage();

    await screen.findByText('Gateway BGP Config');
    const del = screen.getByTitle(/in use by shared-egress/i);

    expect(del).toBeDisabled();
    expect(screen.getByText(/used by shared-egress/i)).toBeInTheDocument();
  });
});
