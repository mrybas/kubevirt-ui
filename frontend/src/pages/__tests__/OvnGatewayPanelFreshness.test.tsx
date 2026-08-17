/**
 * The same freshness defect as the egress-gateway panel, found by looking for
 * its shape rather than by hitting it.
 *
 * A detail panel that performs mutations while staying open must not hold the
 * entity it renders in `useState`. This one deletes DNAT rules and FIPs from
 * inside itself and listed them from a snapshot taken when it opened, so a
 * deleted rule stayed on screen — with the row behind it already correct.
 *
 * On the egress gateway that went further: the attach form fed from the same
 * stale object then declared every VPC already attached, and detach-then-attach
 * was impossible without a page reload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import OvnGateways from '../OvnGateways';

const DNAT = { name: 'dnat-a', eip: '10.199.4.9', external_port: '8080', internal_ip: '10.200.8.5', internal_port: '80', protocol: 'tcp' };

let rules: any[] = [DNAT];

const deleteDnat = vi.fn().mockImplementation(async () => {
  rules = [];
});

// The API layer, not the hooks, so the real invalidation path runs.
vi.mock('../../api/ovn_gateway', () => ({
  listOvnGateways: async () => ({
    items: [{
      name: 'gw-vpc',
      vpc: 'gw-vpc',
      external_subnet: 'external',
      eips: [],
      dnat_rules: rules,
      fips: [],
      snat_rules: [],
      ready: true,
    }],
    total: 1,
  }),
  getOvnGateway: vi.fn(),
  createOvnGateway: vi.fn(),
  deleteOvnGateway: vi.fn(),
  createDnatRule: vi.fn(),
  // Lazy: the factory is hoisted above the consts above it.
  deleteDnatRule: (...args: unknown[]) => deleteDnat(...args),
  createFip: vi.fn(),
  deleteFip: vi.fn(),
}));

vi.mock('../../api/network', () => ({ listSubnets: vi.fn().mockResolvedValue([]) }));
vi.mock('@/hooks/useVpcs', () => ({ useVpcs: () => ({ data: { items: [] } }) }));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OvnGateways />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  rules = [DNAT];
  deleteDnat.mockClear();
});

describe('the OVN gateway detail panel', () => {
  it('stops listing a DNAT rule it just deleted', async () => {
    renderPage();

    fireEvent.click((await screen.findByText('gw-vpc')).closest('tr')!);
    const panel = await screen.findByRole('dialog');
    expect(await within(panel).findByText(/8080/)).toBeInTheDocument();

    fireEvent.click(within(panel).getByTitle(/delete rule/i));

    await waitFor(() => expect(deleteDnat).toHaveBeenCalled());
    await waitFor(() =>
      expect(within(screen.getByRole('dialog')).queryByText(/8080/)).not.toBeInTheDocument(),
    );
  });
});
