/**
 * An environment created with the folder has to get a quota too.
 *
 * The default path enforced nothing: create a folder with a quota and two
 * initial environments, and both namespaces came up with no ResourceQuota at
 * all. The folder number caps the sum of *declared* quotas, and nothing was
 * declared — verified on the cluster with folder `acme` (16 CPU / 32Gi /
 * 200Gi) whose `acme-dev` and `acme-prod` had none.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { CreateFolderModal } from '../Folders';

const mutateAsync = vi.fn().mockResolvedValue({});

vi.mock('../../hooks/useFolders', () => ({
  useFolders: () => ({ data: { items: [] } }),
  useFoldersFlat: () => ({ data: { items: [] } }),
  useCreateFolder: () => ({ mutateAsync, isPending: false }),
  useDeleteFolder: () => ({ mutateAsync: vi.fn() }),
  useMoveFolder: () => ({ mutateAsync: vi.fn() }),
}));

const open = () =>
  render(<CreateFolderModal parentOptions={[]} onClose={vi.fn()} />);

const setValue = (el: HTMLElement, value: string) =>
  fireEvent.change(el, { target: { value } });

beforeEach(() => mutateAsync.mockClear());

async function fillName() {
  setValue(screen.getByPlaceholderText('My Team'), 'Acme');
}

describe('initial environments carry a quota', () => {
  it('offers quota fields per environment', () => {
    open();
    expect(screen.getByLabelText('CPU quota for dev')).toBeInTheDocument();
    expect(screen.getByLabelText('Memory quota for dev')).toBeInTheDocument();
    expect(screen.getByLabelText('Storage quota for dev')).toBeInTheDocument();
  });

  it('a new environment gets its own fields', () => {
    open();
    setValue(screen.getByPlaceholderText('dev, staging, prod...'), 'prod');
    fireEvent.keyDown(screen.getByPlaceholderText('dev, staging, prod...'), { key: 'Enter' });
    expect(screen.getByLabelText('CPU quota for prod')).toBeInTheDocument();
  });

  it('sends them, in the unit that is selected', async () => {
    open();
    await fillName();
    setValue(screen.getByLabelText('CPU quota for dev'), '4');
    setValue(screen.getByLabelText('Memory quota for dev'), '8');
    setValue(screen.getByLabelText('Storage quota for dev'), '512');
    const storUnits = screen.getByRole('group', { name: 'Storage unit for dev' });
    fireEvent.click(within(storUnits).getByText('Mi'));

    fireEvent.click(screen.getByRole('button', { name: /create folder/i }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].environment_quotas).toEqual({
      dev: { cpu: '4', memory: '8Gi', storage: '512Mi' },
    });
  });

  it('an environment left blank sends no quota rather than an empty one', async () => {
    open();
    await fillName();
    fireEvent.click(screen.getByRole('button', { name: /create folder/i }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty('environment_quotas');
  });

  it('refuses a set of environments that overruns the folder ceiling', async () => {
    open();
    await fillName();
    fireEvent.click(screen.getByRole('checkbox'));
    setValue(screen.getByPlaceholderText('e.g. 16'), '8');
    setValue(screen.getByLabelText('CPU quota for dev'), '12');

    expect(screen.getByText(/more cpu than the folder ceiling/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create folder/i })).toBeDisabled();
  });

  it('no longer calls the folder quota a soft limit — it is a real ResourceQuota', () => {
    open();
    expect(screen.queryByText(/soft limit/i)).not.toBeInTheDocument();
  });
});
