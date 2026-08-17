/**
 * The tenant wizard's three dead ends (U12, U14, U15).
 *
 * U12 — Next was greyed out with nothing beside it. `display_name` is the
 *       easiest field to miss: a tenant name is the obvious thing to type and
 *       the display name reads like a nicety.
 * U15 — "No folders available. Ask an admin to create one." is a dead end for
 *       the admin standing there, and on a fresh cluster this is the first
 *       screen anyone opens.
 * U14 — "Ask an admin to label a VPC with this folder/environment" did not say
 *       which labels, so the admin being asked had to go and find out.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import { CreateTenantWizard } from '../Tenants';

let folders: any[] = [];
let vpcs: any[] = [];

vi.mock('@/hooks/useTenants', () => ({
  useTenants: () => ({ data: { items: [] }, isLoading: false }),
  useCreateTenant: () => ({ mutateAsync: vi.fn(), isPending: false, isError: false }),
  useDeleteTenant: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAddonCatalog: () => ({ data: { addons: [] } }),
  useDiscovery: () => ({ data: null }),
}));

vi.mock('@/hooks/useStorage', () => ({ useStorageClasses: () => ({ data: { items: [] } }) }));
vi.mock('@/hooks/useFolders', () => ({ useFoldersFlat: () => ({ data: { items: folders } }) }));
vi.mock('@/hooks/useVpcs', () => ({ useVpcs: () => ({ data: { items: vpcs } }) }));
// The store is read with a selector: useAuthStore(s => s.user).
vi.mock('@/store/auth', () => ({
  useAuthStore: (selector: (s: any) => unknown) =>
    selector({ user: { username: 'admin', is_admin: true } }),
}));

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CreateTenantWizard onClose={vi.fn()} onCreated={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  folders = [
    {
      name: 'poc',
      display_name: 'POC',
      path: [],
      users: [],
      environments: [{ name: 'poc-dev', environment: 'dev' }],
    },
  ];
  vpcs = [];
});

describe('a disabled Next says what is missing', () => {
  it('names every empty required field', async () => {
    renderWizard();

    expect(await screen.findByText(/still needed:/i)).toHaveTextContent(
      'a tenant name, a display name, a folder, an environment',
    );
  });

  it('drops each one as it is filled in', async () => {
    renderWizard();

    fireEvent.change(await screen.findByPlaceholderText('my-tenant'), {
      target: { value: 't9' },
    });
    fireEvent.change(screen.getByPlaceholderText('My Tenant Cluster'), {
      target: { value: 'Tenant Nine' },
    });

    const still = screen.getByText(/still needed:/i);
    expect(still).toHaveTextContent('a folder, an environment');
    expect(still).not.toHaveTextContent('display name');
  });
});

describe('with no folders at all', () => {
  it('offers to create one instead of naming an absent admin', async () => {
    folders = [];
    renderWizard();

    const link = await screen.findByRole('link', { name: /create a folder/i });

    expect(link).toHaveAttribute('href', '/folders/new');
    expect(screen.queryByText(/ask an admin to create one/i)).not.toBeInTheDocument();
  });
});

describe('with no VPC scoped to the chosen folder', () => {
  it('names the labels and the screen that writes them', async () => {
    renderWizard();

    fireEvent.change(await screen.findByPlaceholderText('my-tenant'), { target: { value: 't9' } });
    fireEvent.change(screen.getByPlaceholderText('My Tenant Cluster'), {
      target: { value: 'Tenant Nine' },
    });
    const selects = [...document.querySelectorAll('select')];
    fireEvent.change(selects.find((s) => [...s.options].some((o) => o.value === 'poc'))!, {
      target: { value: 'poc' },
    });
    fireEvent.change(
      [...document.querySelectorAll('select')].find((s) =>
        [...s.options].some((o) => o.value === 'dev'),
      )!,
      { target: { value: 'dev' } },
    );

    // Basics → Workers → Addons → Network
    for (let i = 0; i < 3; i++) {
      fireEvent.click(screen.getByRole('button', { name: /next/i }));
    }

    expect(await screen.findByText(/kubevirt-ui\.io\/folder=poc/)).toBeInTheDocument();
    expect(screen.getByText(/kubevirt-ui\.io\/environment=dev/)).toBeInTheDocument();
    expect(screen.queryByText(/ask an admin to label a vpc/i)).not.toBeInTheDocument();
  });
});
