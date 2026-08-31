/**
 * The tenants page was unreachable by the one role allowed to use it.
 *
 * The backend has always let a folder-admin create a tenant — `create_tenant`
 * demands folder-admin, not platform admin — and the list endpoint scopes
 * itself to folders where the caller has at least viewer access. The UI did
 * not know any of that: the menu item lived in the "Admin" block and both
 * routes were wrapped in `RequireAdmin`, so anyone who was not a platform
 * admin got bounced to the dashboard. The right existed and nobody who held
 * it could use it.
 *
 * These render the real page: what a caller may do comes from the backend's
 * own per-folder answer, so a viewer is not offered a button that returns 403,
 * and someone who may create is not left staring at a page with no way in.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { readFileSync } from 'fs';
import { join } from 'path';

let folders: { items: Array<{ name: string; can_create_tenant?: boolean; users: string[] }> } = {
  items: [],
};

vi.mock('../../hooks/useFolders', () => ({
  useFoldersFlat: () => ({ data: folders }),
  useFolders: () => ({ data: folders }),
}));
vi.mock('../../hooks/useTenants', () => ({
  useTenants: () => ({ data: { items: [] }, isLoading: false, refetch: vi.fn() }),
  useDeleteTenant: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCreateTenant: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock('../../store/app', () => ({ useAppStore: () => ({ selectedNamespace: null }) }));

async function renderTenants() {
  const { default: Tenants } = await import('../Tenants');
  return render(
    <MemoryRouter>
      <Tenants />
    </MemoryRouter>,
  );
}

describe('the tenants page', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('offers creation when the backend says this caller may create one', async () => {
    folders = { items: [{ name: 'poc', can_create_tenant: true, users: [] }] };
    await renderTenants();
    expect(screen.getAllByText(/Create Tenant/i).length).toBeGreaterThan(0);
  });

  it('offers no creation to someone who may only look', async () => {
    // `users` is deliberately empty: access granted by group never put anyone
    // in that list, which is how the old filter hid the page from people who
    // had the right. The answer must not come from it.
    folders = { items: [{ name: 'poc', can_create_tenant: false, users: [] }] };
    await renderTenants();
    expect(screen.queryByText(/Create Tenant/i)).toBeNull();
  });

  it('says what it would take, instead of an empty page with no explanation', async () => {
    folders = { items: [{ name: 'poc', can_create_tenant: false, users: [] }] };
    await renderTenants();
    expect(screen.getByText(/folder-admin/i)).toBeTruthy();
  });
});

describe('reaching the page at all', () => {
  const read = (p: string) => readFileSync(join(__dirname, p), 'utf-8');

  it('does not wrap the tenant routes in RequireAdmin', () => {
    const app = read('../../App.tsx');
    const tenantRoutes = app
      .split('\n')
      .filter(line => line.includes('path="/tenants'));
    expect(tenantRoutes.length).toBeGreaterThan(0);
    for (const line of tenantRoutes) {
      expect(line).not.toMatch(/RequireAdmin/);
    }
  });

  it('does not hide the menu item in the admin-only block', () => {
    const sidebar = read('../../components/layout/Sidebar.tsx');
    const adminBlock = sidebar.slice(
      sidebar.indexOf('const adminNavigation'),
      sidebar.indexOf('const adminNavigation') + 400,
    );
    expect(adminBlock).not.toMatch(/'\/tenants'/);
  });
});
