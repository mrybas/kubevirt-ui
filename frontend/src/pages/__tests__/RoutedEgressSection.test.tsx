/**
 * The BGP page said there was no BGP.
 *
 * Measured on the live stand: one `BGPSessionState` Established against
 * 10.198.175.254 and five prefixes generated into the frr-k8s configuration —
 * while the page rendered "No BGP sessions. Deploy the speaker first." and
 * "No BGP announcements configured." Both statements came from
 * kube-ovn-speaker, which announces the default VPC and nothing else, so on a
 * stand where every tenant has its own VPC the speaker's view is empty *by
 * construction* and reads as a cluster-wide verdict.
 *
 * The section replacing it reports the routed plane, and reports it as intent:
 * FRR was handed these prefixes. Whether the upstream router took them is a
 * fact only the router holds.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import BgpPeering from '../BgpPeering';

let routed: any = {
  enabled: true,
  peer: '10.198.175.254',
  local_asn: 65030,
  nodes: ['kubevirt-lab-worker-2'],
  intended: [
    { vpc: 'b3v', cidr: '10.200.36.0/22', next_hop: '10.199.4.11' },
    { vpc: 'team-a', cidr: '10.200.0.0/22', next_hop: '10.199.4.1' },
  ],
  sessions: [
    {
      node: 'kubevirt-lab-worker-2',
      peer: '10.198.175.254',
      status: 'Established',
      bfd: 'N/A',
    },
  ],
  config_errors: {},
};

vi.mock('@/hooks/useBgp', () => ({
  useRoutedEgress: () => ({ data: routed, refetch: vi.fn() }),
  useBgpConfs: () => ({ data: { items: [], total: 0 }, isLoading: false }),
  useUpsertBgpConf: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteBgpConf: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useEgressGateways', () => ({
  useEgressGateways: () => ({ data: { items: [] } }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BgpPeering />
    </QueryClientProvider>,
  );
}

describe('the routed egress section', () => {
  it('shows the session that the page used to deny existed', () => {
    renderPage();

    expect(screen.getByText('Established')).toBeInTheDocument();
    // Once in the header ("announced from"), once in the session row.
    expect(screen.getAllByText('kubevirt-lab-worker-2').length).toBeGreaterThan(0);
    expect(screen.queryByText(/Deploy the speaker first/i)).not.toBeInTheDocument();
  });

  it('lists every announced prefix with the leg that carries it', () => {
    renderPage();

    expect(screen.getByText('10.200.36.0/22')).toBeInTheDocument();
    expect(screen.getByText('10.199.4.11')).toBeInTheDocument();
    expect(screen.getByText('10.200.0.0/22')).toBeInTheDocument();
    expect(screen.getByText(/Prefixes \(2\)/)).toBeInTheDocument();
  });

  it('does not claim the router accepted them', () => {
    renderPage();

    expect(
      screen.getByText(/only visible on the router itself/i),
    ).toBeInTheDocument();
  });

  it('names the node FRR refused, and says what stops working', () => {
    // FRR keeps the last good configuration when it rejects a new one. The
    // session stays Established and the old prefixes keep flowing, so the only
    // symptom is that a VPC created after the break is never announced —
    // invisible unless the page reads lastReloadResult.
    routed = {
      ...routed,
      config_errors: { 'kubevirt-lab-worker-2': 'failed to reload: line 7 malformed' },
    };
    renderPage();

    expect(screen.getByText(/line 7 malformed/)).toBeInTheDocument();
    expect(screen.getByText(/new VPCs are not being announced/i)).toBeInTheDocument();
  });

  it('says so plainly where the routed plane is not configured', () => {
    routed = { enabled: false, peer: '', local_asn: 0, nodes: [], intended: [], sessions: [], config_errors: {} };
    renderPage();

    expect(screen.getByText(/Not configured on this deployment/i)).toBeInTheDocument();
  });
});
