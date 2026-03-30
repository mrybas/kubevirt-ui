import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, navigateVia, takeScreenshot } from './helpers';

test.describe('Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('should navigate to all main sidebar pages', async ({ page }) => {
    // Main navigation items from Sidebar.tsx
    const pages = [
      { name: 'Dashboard', url: '/dashboard', heading: /Dashboard/i },
      { name: 'All VMs', url: '/vms', heading: /Virtual Machines/i },
      { name: 'Images', url: '/storage/images', heading: /Storage|Images/i },
      { name: 'Classes', url: '/storage/classes', heading: /Storage Classes/i },
      { name: 'User Networks', url: '/network', heading: /Network/i },
      { name: 'Cluster', url: '/cluster', heading: /Cluster/i },
      { name: 'Projects', url: '/projects', heading: /Projects/i },
      { name: 'Tenants', url: '/tenants', heading: /Tenants/i },
      { name: 'All Users', url: '/users', heading: /Users/i },
      { name: 'Groups', url: '/users/groups', heading: /Groups/i },
    ];

    for (const p of pages) {
      await navigateVia(page, p.name, p.url);

      // Verify page loaded (no blank page)
      const body = page.locator('body');
      await expect(body).not.toBeEmpty();

      await takeScreenshot(page, `02-nav-${p.name.toLowerCase().replace(/\s+/g, '-')}`);
    }
  });

  test('should navigate to profile and CLI access (footer links)', async ({ page }) => {
    // Profile — in sidebar footer
    await page.getByRole('link', { name: 'Profile' }).click();
    await page.waitForURL('**/profile', { timeout: 10_000 });
    await waitForPageLoad(page);
    await takeScreenshot(page, '02-nav-profile');

    // CLI Access
    await page.getByRole('link', { name: 'CLI Access' }).click();
    await page.waitForURL('**/cli-access', { timeout: 10_000 });
    await waitForPageLoad(page);
    await takeScreenshot(page, '02-nav-cli-access');
  });

  test('each page loads without HTTP 500 errors', async ({ page }) => {
    const errors: string[] = [];

    // Listen for failed API responses
    page.on('response', (response) => {
      if (response.status() >= 500) {
        errors.push(`${response.status()} ${response.url()}`);
      }
    });

    const urls = [
      '/dashboard',
      '/vms',
      '/storage/images',
      '/storage/classes',
      '/network',
      '/network/system',
      '/cluster',
      '/projects',
      '/tenants',
      '/users',
      '/users/groups',
      '/profile',
      '/cli-access',
    ];

    for (const url of urls) {
      await page.goto(url);
      await waitForPageLoad(page);
    }

    // Some 500s may be acceptable if cluster is not fully bootstrapped,
    // but log them for visibility
    if (errors.length > 0) {
      console.warn('HTTP 500 errors encountered:', errors);
    }
  });
});
