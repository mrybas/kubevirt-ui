/**
 * A write is followed by refetches over the seconds the controller takes.
 *
 * The product used to do the work inside the request, so one invalidation
 * after a POST was right. It writes a custom resource now and an operator
 * builds from it — the answer arrives before the fact — and a single
 * invalidation reads the world from just before the write.
 *
 * UAT run 4 hit it three times in one session: "ProviderNetwork MISSING"
 * after a Build Underlay that worked, "No peerings configured" with the
 * peering written and both legs in place, and an image's status during
 * import. All three correct after a manual reload. The cost is not the wrong
 * pixel; it is that people press the button again, and two of those buttons
 * should not be pressed twice.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { settle } from '../settle';

function client() {
  return { invalidateQueries: vi.fn() } as any;
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('settle', () => {
  it('refetches at once, for the case the controller was quick', () => {
    const qc = client();
    settle(qc, [['vpcs']]);
    expect(qc.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['vpcs'] });
    expect(qc.invalidateQueries).toHaveBeenCalledTimes(1);
  });

  it('and again while it catches up', () => {
    const qc = client();
    settle(qc, [['vpcs']], [500, 1500]);
    vi.advanceTimersByTime(2000);
    expect(qc.invalidateQueries).toHaveBeenCalledTimes(3);
  });

  it('covers several seconds, not milliseconds', () => {
    // A controller writing a VPC, its subnet and its ACLs takes seconds. A
    // schedule that gives up inside one is the same bug with extra steps.
    const qc = client();
    settle(qc, [['vpcs']]);
    vi.advanceTimersByTime(999);
    const early = qc.invalidateQueries.mock.calls.length;
    vi.advanceTimersByTime(4000);
    expect(qc.invalidateQueries.mock.calls.length).toBeGreaterThan(early);
  });

  it('refetches every key it was given, on every pass', () => {
    const qc = client();
    settle(qc, [['underlay'], ['subnets']], [500]);
    vi.advanceTimersByTime(600);
    const keys = qc.invalidateQueries.mock.calls.map((c: any[]) => c[0].queryKey[0]);
    expect(keys).toEqual(['underlay', 'subnets', 'underlay', 'subnets']);
  });
});

describe('the writes that go through a controller', () => {
  it('use it — peering, underlay, VPC and image', async () => {
    const { readFileSync } = await import('fs');
    const { join } = await import('path');
    const hooks = join(__dirname, '..');

    const vpcs = readFileSync(join(hooks, 'useVpcs.ts'), 'utf8');
    // Add and remove both: the page lied in both directions.
    expect(vpcs.match(/settle\(queryClient/g)?.length).toBeGreaterThanOrEqual(3);
    expect(vpcs).not.toMatch(
      /addVpcPeering\(vpcName, request\),\s*onSuccess: \(\) => \{\s*queryClient\.invalidateQueries/,
    );

    expect(readFileSync(join(hooks, 'useUnderlay.ts'), 'utf8')).toContain('settle(queryClient');
    expect(readFileSync(join(hooks, 'useTemplates.ts'), 'utf8')).toContain('settle(queryClient');
  });
});
