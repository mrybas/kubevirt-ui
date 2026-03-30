import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:3333';
const SCREENSHOT_DIR = '/screenshots';

// OIDC login via DEX + LLDAP
async function login(page: import('@playwright/test').Page) {
  await page.goto(`${BASE}/login`);
  await page.waitForLoadState('networkidle');

  // Click "Sign in with SSO"
  const ssoBtn = page.getByRole('button', { name: /Sign in with SSO/i });
  await expect(ssoBtn).toBeVisible({ timeout: 10000 });
  await page.screenshot({ path: `${SCREENSHOT_DIR}/00-login-page.png`, fullPage: true });
  await ssoBtn.click();

  // DEX page — wait for redirect away from frontend
  await page.waitForURL(url => !url.href.includes('localhost:3333'), { timeout: 15000 });

  // DEX may show connector selection — click LDAP connector if visible
  const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in/i });
  if (await connectorLink.isVisible({ timeout: 3000 }).catch(() => false)) {
    await connectorLink.click();
  }

  await page.screenshot({ path: `${SCREENSHOT_DIR}/00-dex-login.png`, fullPage: true });

  // Fill DEX LDAP login form
  await page.getByRole('textbox', { name: /username/i }).fill('admin');
  await page.getByRole('textbox', { name: /password/i }).fill('admin_password');
  await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

  // Wait for redirect back to dashboard
  await page.waitForURL('**/dashboard', { timeout: 30000 });
  await page.waitForLoadState('networkidle');
}

test.describe('KubeVirt UI Walkthrough', () => {

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // Screenshot all main pages
  const pages = [
    { name: 'dashboard', path: '/dashboard' },
    { name: 'vms', path: '/vms' },
    { name: 'vm-templates', path: '/vms/templates' },
    { name: 'storage-images', path: '/storage/images' },
    { name: 'storage-classes', path: '/storage/classes' },
    { name: 'network', path: '/network' },
    { name: 'network-system', path: '/network/system' },
    { name: 'network-vpcs', path: '/network/vpcs' },
    { name: 'network-egress', path: '/network/egress-gateways' },
    { name: 'network-security-groups', path: '/network/security-groups' },
    { name: 'cluster', path: '/cluster' },
    { name: 'projects', path: '/projects' },
    { name: 'folders', path: '/folders' },
    { name: 'tenants', path: '/tenants' },
    { name: 'users', path: '/users' },
    { name: 'users-groups', path: '/users/groups' },
    { name: 'profile', path: '/profile' },
    { name: 'cli-access', path: '/cli-access' },
  ];

  for (const p of pages) {
    test(`Screenshot: ${p.name}`, async ({ page }) => {
      await page.goto(`${BASE}${p.path}`);
      await page.waitForLoadState('networkidle');
      // Wait a bit for React rendering
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/${p.name}.png`, fullPage: true });
    });
  }

  // Wizard tests
  test('Open Create VM wizard', async ({ page }) => {
    await page.goto(`${BASE}/vms`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-vm-wizard.png`, fullPage: true });
    }
  });

  test('Open Create Network', async ({ page }) => {
    await page.goto(`${BASE}/network`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-network-wizard.png`, fullPage: true });
    }
  });

  test('Open Create VPC', async ({ page }) => {
    await page.goto(`${BASE}/network/vpcs`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-vpc.png`, fullPage: true });
    }
  });

  test('Open Create Egress Gateway', async ({ page }) => {
    await page.goto(`${BASE}/network/egress-gateways`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-egress-gateway.png`, fullPage: true });
    }
  });

  test('Open Create Security Group', async ({ page }) => {
    await page.goto(`${BASE}/network/security-groups`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-security-group.png`, fullPage: true });
    }
  });

  test('Open Create Tenant wizard', async ({ page }) => {
    await page.goto(`${BASE}/tenants`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-tenant-step1.png`, fullPage: true });

      const nextBtn = page.getByRole('button', { name: /next/i });
      if (await nextBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
        await nextBtn.click();
        await page.waitForTimeout(500);
        await page.screenshot({ path: `${SCREENSHOT_DIR}/create-tenant-step2.png`, fullPage: true });
      }
    }
  });

  test('Open Create Folder', async ({ page }) => {
    await page.goto(`${BASE}/folders`);
    await page.waitForLoadState('networkidle');
    const createBtn = page.getByRole('button', { name: /create/i });
    if (await createBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/create-folder.png`, fullPage: true });
    }
  });

  test('Sidebar toggle', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/sidebar-expanded.png`, fullPage: true });

    const toggleBtn = page.locator('[aria-label*="collapse"], [aria-label*="sidebar"], [aria-label*="Collapse"]').first();
    if (await toggleBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await toggleBtn.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/sidebar-collapsed.png`, fullPage: true });
    }
  });

  test('404 page', async ({ page }) => {
    await page.goto(`${BASE}/nonexistent-page`);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/404-page.png`, fullPage: true });
  });
});
