/**
 * What the browser actually PUTS IN THE REQUEST when a catalogue row is
 * materialised — not merely that it fired one.
 *
 * The defect this replaces: both call sites sent `catalog_ref` as
 * `source_registry`. `catalog_ref` is host-less by design
 * ("project/repo:tag"), so CDI got a registry source with no registry —
 * resolving against Docker Hub — and the disk's stored `source_url` then had
 * no `docker://` prefix, so `catalog_ref_from_source_url` returned null and
 * the finished disk never merged back with its catalogue row. The unified
 * list, the whole point of the feature, became two permanent rows for one
 * image.
 *
 * It shipped green because the existing tests asserted the mutation was
 * CALLED, never with what. Every assertion here is on the request body.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const mockCreate = vi.fn();
const mockDelete = vi.fn();
const mockRefetch = vi.fn();

const CATALOG_ROW = {
  name: 'rocky-9:20260901',
  namespace: '',
  display_name: 'Rocky 9',
  status: 'Catalog',
  disk_type: 'image',
  persistent: false,
  scope: 'environment',
  origin: 'catalog',
  catalog_ref: 'vm-images-tenant-a/rocky-9:20260901',
};

vi.mock('../../hooks/useTemplates', () => ({
  useGoldenImages: () => ({
    data: { items: [CATALOG_ROW] },
    catalogAvailable: true,
    isLoading: false,
    refetch: mockRefetch,
  }),
  useCreateGoldenImage: () => ({
    isPending: false,
    error: null,
    mutateAsync: mockCreate,
  }),
  useDeleteGoldenImage: () => ({ isPending: false, error: null, mutateAsync: mockDelete }),
}));
vi.mock('../../hooks/useNamespaces', () => ({
  useNamespaces: () => ({ data: { items: [{ name: 'tenant-a-dev' }] } }),
}));
vi.mock('../../hooks/useStorage', () => ({
  useStorageClasses: () => ({ data: { items: [] } }),
}));
vi.mock('../../hooks/useFolders', () => ({
  useFoldersFlat: () => ({ data: { items: [] } }),
}));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
vi.mock('../../store', () => ({
  useAppStore: () => ({ selectedNamespace: 'tenant-a-dev' }),
}));

import { Storage } from '../Storage';

beforeEach(() => {
  mockCreate.mockReset();
  mockCreate.mockResolvedValue({ name: 'rocky-9-abc12' });
});

describe('materialising a catalogue row from the Storage page', () => {
  it('sends catalog_ref, never the host-less ref as source_registry', async () => {
    const user = userEvent.setup();
    render(<Storage />);

    // Scoped to the catalogue ROW — the page header carries its own
    // "Create Disk" button for a blank disk, which is a different action.
    const row = await screen.findByTestId('origin-catalog');
    await user.click(within(row).getByRole('button', { name: /create disk/i }));

    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
    const { data, namespace } = mockCreate.mock.calls[0][0];

    expect(data.catalog_ref).toBe('vm-images-tenant-a/rocky-9:20260901');
    // The bug, stated directly: this field must not carry a host-less ref.
    expect(data.source_registry).toBeUndefined();
    expect(namespace).toBe('tenant-a-dev');
  });

  it('names no credential — the backend attaches the tenant robot itself', async () => {
    const user = userEvent.setup();
    render(<Storage />);

    // Scoped to the catalogue ROW — the page header carries its own
    // "Create Disk" button for a blank disk, which is a different action.
    const row = await screen.findByTestId('origin-catalog');
    await user.click(within(row).getByRole('button', { name: /create disk/i }));

    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
    const { data } = mockCreate.mock.calls[0][0];

    // Not "the browser forgot to send these" — the browser must never know
    // them. One source of truth for the registry host, and a credential name
    // that is never in a page cannot leak from one.
    expect(data.source_registry_secret).toBeUndefined();
    expect(data.source_registry_ca_configmap).toBeUndefined();
  });
});
