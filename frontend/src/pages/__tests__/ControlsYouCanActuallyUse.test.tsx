/**
 * Three controls from UAT run 4 that looked usable and were not.
 *
 * V-1: "Create Snapshot" was reported as sending no request at all — two GETs
 * over the life of the page and no POST. True, and not the reason: the button
 * is disabled until the name field has a value, and the field showed
 * `<pvc>-snap` as a *placeholder*, which reads exactly like a value. There was
 * nothing to send and nothing saying so. Worse, the name was cleared after
 * every successful create, so the second snapshot hit the same wall.
 *
 * B5: a viewer was shown two "Create VM" buttons, both of which answer 403.
 * The page decided from what it could see rather than from what the caller may
 * do; the backend answers that per folder now and the page reads it.
 *
 * E3-1: Live Migrate offered `kubevirt-lab-cp-1` first, so the easiest click
 * in the dialog moved a workload onto a control plane.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { readFileSync } from 'fs';
import { join } from 'path';
import { MigrateVMModal } from '../../components/vm/MigrateVMModal';

const nodes = [
  { name: 'kubevirt-lab-cp-1', status: 'Ready', roles: ['control-plane'], cpu: '8', memory: '32Gi' },
  { name: 'kubevirt-lab-worker-2', status: 'Ready', roles: ['worker'], cpu: '16', memory: '64Gi' },
  { name: 'kubevirt-lab-worker-1', status: 'Ready', roles: ['worker'], cpu: '16', memory: '64Gi' },
  { name: 'kubevirt-lab-worker-3', status: 'NotReady', roles: ['worker'], cpu: '16', memory: '64Gi' },
];

vi.mock('@/hooks/useNamespaces', () => ({ useNodes: () => ({ data: { items: nodes } }) }));

describe('Live Migrate', () => {
  it('offers a worker before a control plane', () => {
    render(
      <MigrateVMModal
        vmName="web-01" currentNode="kubevirt-lab-worker-1"
        onClose={vi.fn()} onMigrate={vi.fn()} isMigrating={false}
      />,
    );
    const offered = screen.getAllByRole('button')
      .map(b => b.textContent ?? '')
      .filter(t => t.includes('kubevirt-lab-'));
    expect(offered[0]).toContain('kubevirt-lab-worker-2');
    expect(offered[offered.length - 1]).toContain('kubevirt-lab-cp-1');
  });

  it('says which one is a control plane', () => {
    render(
      <MigrateVMModal
        vmName="web-01" currentNode="kubevirt-lab-worker-1"
        onClose={vi.fn()} onMigrate={vi.fn()} isMigrating={false}
      />,
    );
    expect(screen.getByText('control plane')).toBeInTheDocument();
  });

  it('does not hide it — an untainted master is a legitimate target', () => {
    render(
      <MigrateVMModal
        vmName="web-01" currentNode="kubevirt-lab-worker-1"
        onClose={vi.fn()} onMigrate={vi.fn()} isMigrating={false}
      />,
    );
    expect(screen.getByText('kubevirt-lab-cp-1')).toBeInTheDocument();
  });
});

describe('Create Snapshot', () => {
  const disks = readFileSync(
    join(__dirname, '..', '..', 'components', 'vm', 'tabs', 'DisksTab.tsx'), 'utf8');
  const vmTab = readFileSync(
    join(__dirname, '..', '..', 'components', 'vm', 'tabs', 'SnapshotsTab.tsx'), 'utf8');

  it('starts with a name, so the button is live when it looks live', () => {
    expect(disks).toMatch(/const suggested = `\$\{pvcName\}-snap`/);
    expect(disks).toMatch(/setSnapshotName\(suggested\)/);
    expect(vmTab).toMatch(/useState\(suggested\)/);
  });

  it('puts the name back after a create instead of clearing it', () => {
    // Clearing it disabled the button again, so the second snapshot met the
    // same dead control as the first.
    expect(disks).not.toMatch(/onSuccess: \(\) => setSnapshotName\(''\)/);
    expect(vmTab).not.toMatch(/onSuccess: \(\) => setSnapshotName\(''\)/);
  });
});

describe('Create VM', () => {
  const page = readFileSync(join(__dirname, '..', 'VirtualMachines.tsx'), 'utf8');

  it('is offered only to someone who may create one', () => {
    expect(page).toMatch(/const mayCreate = .*can_create/);
    // Both of them — the header button and the empty-state one.
    expect(page.match(/mayCreate/g)?.length).toBeGreaterThanOrEqual(3);
  });

  it('reads the answer rather than re-deriving the access rules', () => {
    expect(page).not.toMatch(/access\?\.(admins|members)/);
    expect(page).not.toMatch(/groups\.includes/);
  });
});
