/**
 * A folder's quota must be visible against what it is spent on, and editable
 * where it is shown.
 *
 * Two gaps, one page. The folder's own ceiling could be set at creation and
 * then only through a dialog that showed nothing else — so "No quota
 * configured" read as "no quota is possible". And an environment's quota was
 * shown nowhere at all: `PUT /folders/{name}/environments/{env}/quota` had
 * existed since rebalancing was added, with no caller anywhere in the UI, so
 * the only way to re-size an environment was to delete it.
 *
 * This file measures the tab that answers both: the ceiling with its
 * allocation under it, every claimant listed, each editable in place.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FolderQuotaTab } from '../../components/folders/FolderQuotaTab';

const updateFolder = vi.fn().mockResolvedValue({});
const setEnvQuota = vi.fn().mockResolvedValue({});

let headroom: any = {
  quota: { cpu: '16', memory: '32Gi', storage: '200Gi' },
  allocated: { cpu: 9, memory: 24 * 2 ** 30, storage: 140 * 2 ** 30 },
  free: { cpu: 7, memory: 8 * 2 ** 30, storage: 60 * 2 ** 30 },
};

vi.mock('../../hooks/useFolders', () => ({
  useFolderQuotaHeadroom: () => ({ data: headroom }),
  useUpdateFolder: () => ({ mutateAsync: updateFolder, isPending: false }),
  useSetEnvironmentQuota: () => ({ mutateAsync: setEnvQuota, isPending: false }),
}));

const folder: any = {
  name: 'lab',
  display_name: 'Lab',
  quota: { cpu: '16', memory: '32Gi', storage: '200Gi' },
  environments: [
    {
      environment: 'dev', name: 'lab-dev',
      quota_cpu: '5', quota_memory: '16Gi', quota_storage: '100Gi',
      used_cpu: '2', used_memory: '8Gi', used_storage: '40Gi',
    },
  ],
  children: [
    { name: 'kid', display_name: 'Kid', quota: { cpu: '4', memory: '8Gi', storage: '40Gi' } },
  ],
};

function open(f: any = folder) {
  return render(
    <MemoryRouter>
      <FolderQuotaTab folder={f} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  updateFolder.mockClear();
  setEnvQuota.mockClear();
});

describe('the folder ceiling', () => {
  it('shows the current quota without anyone having to click', () => {
    open();
    expect(screen.getByText('16')).toBeInTheDocument();
    expect(screen.getByText('32Gi')).toBeInTheDocument();
    expect(screen.getByText('200Gi')).toBeInTheDocument();
  });

  it('shows how much of it is already spoken for', () => {
    open();
    // 9 of 16 cores, 24Gi of 32Gi, 140Gi of 200Gi — allocation, then what is left.
    expect(screen.getByText('9')).toBeInTheDocument();
    expect(screen.getByText('24Gi')).toBeInTheDocument();
    expect(screen.getByText(/7 free/)).toBeInTheDocument();
    expect(screen.getByText(/8Gi free/)).toBeInTheDocument();
  });

  it('is edited in place, seeded from what is set', async () => {
    open();
    fireEvent.click(screen.getByText('Edit quota'));
    expect((screen.getByLabelText('Quota CPU') as HTMLInputElement).value).toBe('16');
    expect((screen.getByLabelText('Quota memory') as HTMLInputElement).value).toBe('32Gi');
    expect((screen.getByLabelText('Quota storage') as HTMLInputElement).value).toBe('200Gi');

    fireEvent.change(screen.getByLabelText('Quota CPU'), { target: { value: '32' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(updateFolder).toHaveBeenCalled());
    expect(updateFolder.mock.calls[0][0]).toEqual({
      name: 'lab',
      request: { quota: { cpu: '32', memory: '32Gi', storage: '200Gi' } },
    });
  });

  it('clearing all three asks for no quota rather than sending blanks', async () => {
    open();
    fireEvent.click(screen.getByText('Edit quota'));
    for (const label of ['Quota CPU', 'Quota memory', 'Quota storage']) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: '' } });
    }
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(updateFolder).toHaveBeenCalled());
    expect(updateFolder.mock.calls[0][0].request.quota).toEqual({});
  });

  it('says a refusal out loud instead of closing as if it had saved', async () => {
    updateFolder.mockRejectedValueOnce(new Error('quota exceeds parent'));
    open();
    fireEvent.click(screen.getByText('Edit quota'));
    fireEvent.click(screen.getByText('Save'));
    expect(await screen.findByText('quota exceeds parent')).toBeInTheDocument();
    // Still editing: the values are not thrown away with the error.
    expect(screen.getByLabelText('Quota CPU')).toBeInTheDocument();
  });

  it('does not promise enforcement the cluster does not do', () => {
    open();
    fireEvent.click(screen.getByText('Edit quota'));
    expect(screen.getByText(/not a limit the\s+cluster enforces/)).toBeInTheDocument();
  });

  it('an uncapped folder says so instead of reading as broken', () => {
    headroom = { quota: null, allocated: { cpu: 2, memory: 0, storage: 0 }, free: { cpu: null, memory: null, storage: null } };
    open({ ...folder, quota: null });
    expect(screen.getByText(/No quota configured/)).toBeInTheDocument();
    expect(screen.getAllByText('not capped').length).toBe(3);
    headroom = {
      quota: { cpu: '16', memory: '32Gi', storage: '200Gi' },
      allocated: { cpu: 9, memory: 24 * 2 ** 30, storage: 140 * 2 ** 30 },
      free: { cpu: 7, memory: 8 * 2 ** 30, storage: 60 * 2 ** 30 },
    };
  });
});

describe('what holds the quota', () => {
  it('lists environments with their quota and their use', () => {
    open();
    expect(screen.getByText('dev')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('16Gi')).toBeInTheDocument();
    expect(screen.getByText('(8Gi used)')).toBeInTheDocument();
  });

  it('re-sizes an environment through the route that had no caller', async () => {
    open();
    fireEvent.click(screen.getByLabelText('Edit dev quota'));
    expect((screen.getByLabelText('dev memory quota') as HTMLInputElement).value).toBe('16Gi');
    fireEvent.change(screen.getByLabelText('dev memory quota'), { target: { value: '20Gi' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(setEnvQuota).toHaveBeenCalled());
    expect(setEnvQuota.mock.calls[0][0]).toEqual({
      environment: 'dev',
      quota: { cpu: '5', memory: '20Gi', storage: '100Gi' },
    });
  });

  it('shows the server refusing to shrink below what is in use', async () => {
    setEnvQuota.mockRejectedValueOnce(new Error('below current usage'));
    open();
    fireEvent.click(screen.getByLabelText('Edit dev quota'));
    fireEvent.click(screen.getByText('Save'));
    expect(await screen.findByText('below current usage')).toBeInTheDocument();
  });

  it('sends a sub-folder to its own page rather than editing it from here', () => {
    open();
    const link = screen.getByText(/Edit there/).closest('a');
    expect(link).toHaveAttribute('href', '/folders/kid');
  });
});
