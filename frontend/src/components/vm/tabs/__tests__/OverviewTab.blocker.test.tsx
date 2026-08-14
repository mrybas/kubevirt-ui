/**
 * A VM that cannot start has to say so where someone is looking.
 *
 * `nomac` sat on "Starting… / Not scheduled / Pending" for twenty minutes.
 * The reason was in the response all along —
 *
 *   Synchronized=False FailedCreate: failed to create virtual machine pod:
 *   ... is forbidden: exceeded quota: acme-dev-quota, requested cpu=2015m,
 *   used 2015m, limited 3
 *
 * — but only inside the Conditions panel, which is collapsed. Now that
 * quotas actually refuse things, a blocked VM is indistinguishable from a
 * slow one.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { OverviewTab } from '../OverviewTab';

vi.mock('@/components/common/CopyableValue', () => ({
  CopyableValue: ({ value }: { value: string }) => <span>{value}</span>,
}));

const QUOTA_REFUSAL =
  'failed to create virtual machine pod: pods "virt-launcher-nomac" is forbidden: ' +
  'exceeded quota: acme-dev-quota, requested: requests.cpu=2015m, limited: requests.cpu=3';

const base = {
  name: 'nomac-88jjm', display_name: 'nomac', namespace: 'acme-dev',
  cpu_cores: 2, memory: '4Gi', run_strategy: 'Always', volumes: [], disks: [],
  labels: {}, annotations: {},
};

const pending = {
  ...base, status: 'Starting', phase: 'Pending', ready: false,
  conditions: [
    { type: 'Ready', status: 'False', reason: 'PodNotExists', message: 'virt-launcher pod has not yet been scheduled' },
    { type: 'DataVolumesReady', status: 'True', reason: 'AllDVsReady', message: 'ready' },
    { type: 'Synchronized', status: 'False', reason: 'FailedCreate', message: QUOTA_REFUSAL },
  ],
};

describe('a blocked VM explains itself', () => {
  it('shows the refusal without expanding anything', () => {
    render(<OverviewTab vm={pending as any} />);
    expect(screen.getByText(/Not running: FailedCreate/)).toBeInTheDocument();
    // (the same text also lives in the collapsed Conditions panel)
    expect(screen.getAllByText(/exceeded quota: acme-dev-quota/).length).toBeGreaterThan(0);
  });

  it('prefers the API server refusal over the scheduling message', () => {
    render(<OverviewTab vm={pending as any} />);
    expect(screen.queryByText(/Not running: PodNotExists/)).not.toBeInTheDocument();
  });

  it('falls back to the Ready reason when there is no refusal', () => {
    const vm = { ...pending, conditions: [pending.conditions[0]] };
    render(<OverviewTab vm={vm as any} />);
    expect(screen.getByText(/Not running: PodNotExists/)).toBeInTheDocument();
  });

  it('says nothing about a running VM', () => {
    const vm = {
      ...base, status: 'Running', phase: 'Running', ready: true,
      conditions: [{ type: 'Ready', status: 'True', reason: '', message: '' }],
    };
    render(<OverviewTab vm={vm as any} />);
    expect(screen.queryByText(/Not running/)).not.toBeInTheDocument();
  });
});

describe('a disk that cannot be created outranks the VMI story', () => {
  const QUOTA_CLONE =
    'persistentvolumeclaims "tmp-pvc-591d600d" is forbidden: exceeded quota: ' +
    'acme-dev-quota, requested: requests.storage=10737418240';

  it('shows the DataVolume failure, not "VMI does not exist"', () => {
    const vm = {
      ...base, status: 'DataVolumeError', phase: '-', ready: false,
      conditions: [
        { type: 'Ready', status: 'False', reason: 'VMINotExists', message: 'VMI does not exist' },
        { type: 'DataVolume nomac-clone-disk0', status: 'False', reason: 'Error', message: QUOTA_CLONE },
      ],
    };
    render(<OverviewTab vm={vm as any} />);
    expect(screen.getByText(/Not running: Error/)).toBeInTheDocument();
    expect(screen.getAllByText(/exceeded quota/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Not running: VMINotExists/)).not.toBeInTheDocument();
  });
});
