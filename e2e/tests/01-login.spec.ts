import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot, AUTH_STATE_PATH } from './helpers';

test.describe('Login', () => {
  test('should display login page', async ({ page }) => {
    await page.goto('/login');
    await waitForPageLoad(page);

    // Verify login page elements
    await expect(page.getByText('Sign In')).toBeVisible();
    await expect(page.getByText('Access your virtual machines')).toBeVisible();
    await expect(page.getByRole('button', { name: /Sign in with SSO/i })).toBeVisible();

    // Verify dev hint is shown
    await expect(page.getByText('Development credentials: admin / admin_password')).toBeVisible();

    // Verify branding
    await expect(page.getByText('KubeVirt UI')).toBeVisible();

    await takeScreenshot(page, '01-login-page');
  });

  test('should login via OIDC (DEX + LLDAP)', async ({ page }) => {
    await login(page);

    // Verify we are on the dashboard
    expect(page.url()).toContain('/dashboard');

    await takeScreenshot(page, '01-login-success');
  });

  test('should show dashboard after login', async ({ page }) => {
    await login(page);

    // Dashboard should show resource gauges or activity feed
    await expect(page.getByRole('heading', { level: 2 }).first()).toBeVisible();

    // Sidebar should be visible with navigation
    await expect(page.locator('aside')).toBeVisible();
    await expect(page.getByText('Dashboard')).toBeVisible();

    await takeScreenshot(page, '01-dashboard-after-login');
  });

  test('should persist session across page reload', async ({ page }) => {
    await login(page);

    // Save storage state
    await page.context().storageState({ path: AUTH_STATE_PATH });

    // Reload the page
    await page.reload();
    await waitForPageLoad(page);

    // Should still be on dashboard (not redirected to login)
    await page.waitForURL('**/dashboard', { timeout: 15_000 });
    await expect(page.locator('aside')).toBeVisible();

    await takeScreenshot(page, '01-session-persisted');
  });
});
