/**
 * Creating an environment: quota fields, headroom, and taking room back.
 *
 * The backend has accepted `quota_cpu/memory/storage` on
 * `POST /folders/{name}/environments` all along and turns them into a real
 * ResourceQuota — the only enforcement that also binds kubectl — while the
 * form sent the name alone. And once a folder is fully allocated there was no
 * way to free room at all: environments could be created and deleted, never
 * re-sized.
 *
 * Memory and storage moved to a number plus a Mi/Gi toggle, and the donor
 * sliders move in that unit. They used to be CPU-only for exactly this
 * reason: a slider whose range is 0…17179869184 has no usable step and its
 * label reads as noise.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { AddEnvironmentModal } from '../FolderDetail';

const mutateAsync = vi.fn().mockResolvedValue({});
let headroom: any = {
  quota: { cpu: '16', memory: '32Gi', storage: '200Gi' },
  allocated: { cpu: 16, memory: 32 * 2 ** 30, storage: 200 * 2 ** 30 },
  free: { cpu: 0, memory: 0, storage: 0 },
};

vi.mock('../../hooks/useFolders', () => ({
  useFolder: vi.fn(),
  useFoldersFlat: () => ({ data: { items: [] } }),
  useCreateFolder: () => ({ mutateAsync: vi.fn() }),
  useDeleteFolder: () => ({ mutateAsync: vi.fn() }),
  useUpdateFolder: () => ({ mutateAsync: vi.fn() }),
  useMoveFolder: () => ({ mutateAsync: vi.fn() }),
  useAddFolderEnvironment: () => ({ mutateAsync, isPending: false }),
  useFolderQuotaHeadroom: () => ({ data: headroom }),
  useRemoveFolderEnvironment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useFolderAccess: () => ({ data: { items: [] } }),
  useAddFolderAccess: () => ({ mutateAsync: vi.fn() }),
  useRemoveFolderAccess: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock('../../hooks/useProjects', () => ({ useTeams: () => ({ data: { items: [] } }) }));

const folder: any = {
  name: 'lab',
  display_name: 'lab',
  children: [
    { name: 'kid', display_name: 'kid', quota: { cpu: '2', memory: '8Gi', storage: '40Gi' } },
  ],
  environments: [
    {
      environment: 'dev', name: 'lab-dev',
      quota_cpu: '5', quota_memory: '16Gi', quota_storage: '100Gi',
    },
  ],
};

function open(free: any = { cpu: 0, memory: 0, storage: 0 }) {
  headroom = { quota: { cpu: '16', memory: '32Gi', storage: '200Gi' }, allocated: {}, free };
  return render(<AddEnvironmentModal folder={folder} onClose={vi.fn()} />);
}

const name = () => screen.getByPlaceholderText('dev, staging, prod');
const mem = () => screen.getByLabelText('Environment memory quota');
const stor = () => screen.getByLabelText('Environment storage quota');
const cpu = () => screen.getByLabelText('Environment CPU quota');

beforeEach(() => mutateAsync.mockClear());

describe('quota fields', () => {
  it('sends memory and storage in the unit that is selected', async () => {
    open({ cpu: 16, memory: 32 * 2 ** 30, storage: 200 * 2 ** 30 });
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(cpu(), { target: { value: '4' } });
    fireEvent.change(mem(), { target: { value: '16' } });
    fireEvent.change(stor(), { target: { value: '50' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      environment: 'qa', quota_cpu: '4', quota_memory: '16Gi', quota_storage: '50Gi',
    });
  });

  it('the unit toggle changes what is sent, not only what is shown', async () => {
    open({ cpu: 16, memory: 32 * 2 ** 30, storage: 200 * 2 ** 30 });
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(mem(), { target: { value: '512' } });
    const memUnits = screen.getByRole('group', { name: 'Memory unit' });
    fireEvent.click(within(memUnits).getByText('Mi'));
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].quota_memory).toBe('512Mi');
  });
});

describe('taking room from a sibling', () => {
  it('offers a memory slider that moves in Gi, not in bytes', () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(mem(), { target: { value: '8' } });

    const slider = screen.getByLabelText('Take memory from dev') as HTMLInputElement;
    expect(Number(slider.max)).toBe(16 * 2 ** 30);
    // One notch is a unit of memory, not a byte — a byte step gives
    // 17 billion positions and a thumb that cannot be aimed.
    expect(Number(slider.step)).toBeGreaterThanOrEqual(2 ** 20);
    expect(16 * 2 ** 30 / Number(slider.step)).toBeLessThanOrEqual(512);
  });

  it('labels the amounts in units — no raw byte counts on screen', () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(mem(), { target: { value: '8' } });
    const slider = screen.getByLabelText('Take memory from dev');
    fireEvent.change(slider, { target: { value: String(4 * 2 ** 30) } });

    expect(screen.getByText(/−4Gi → leaves 12Gi/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\d{9,}/);
  });

  it('counts a sub-folder as a donor — its quota is reserved out of the parent too', () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(cpu(), { target: { value: '3' } });
    expect(screen.getByLabelText('Take CPU from kid (sub-folder)')).toBeInTheDocument();
  });

  it('sends only the dimension it took, so the donor keeps the rest', async () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(mem(), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('Take memory from dev'), {
      target: { value: String(8 * 2 ** 30) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    const [item] = mutateAsync.mock.calls[0][0].reallocate;
    expect(item).toEqual({ source: 'dev', kind: 'environment', memory: '8Gi' });
    expect(item).not.toHaveProperty('cpu');
  });

  it('names the sub-folder without its kind prefix in the request', async () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(cpu(), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Take CPU from kid (sub-folder)'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].reallocate).toEqual([
      { source: 'kid', kind: 'folder', cpu: '0' },
    ]);
  });

  it('refuses to submit while any dimension is short', () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(stor(), { target: { value: '10' } });
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
  });

  it('a dimension the folder does not cap is never short', () => {
    open({ cpu: 0, memory: null, storage: null });
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(mem(), { target: { value: '999' } });
    expect(screen.getByRole('button', { name: 'Create' })).toBeEnabled();
  });

  it('says so when nothing in the folder can help', () => {
    open();
    fireEvent.change(name(), { target: { value: 'qa' } });
    fireEvent.change(cpu(), { target: { value: '99' } });
    fireEvent.change(screen.getByLabelText('Take CPU from dev'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('Take CPU from kid (sub-folder)'), {
      target: { value: '2' },
    });
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
  });
});
