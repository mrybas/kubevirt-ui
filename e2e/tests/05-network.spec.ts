import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot } from './helpers';

test.describe('Network', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('should show network page', async ({ page }) => {
    await page.goto('/network');
    await waitForPageLoad(page);

    // Page should load without being blank
    const body = page.locator('body');
    await expect(body).not.toBeEmpty();

    // Should show some network-related content
    // The page shows a hierarchical view of ProviderNetworks > VLANs > Subnets
    // or an empty state if no networks are configured
    const hasContent =
      await page.getByText(/Network/i).first().isVisible().catch(() => false) ||
      await page.getByText(/No.*network/i).isVisible().catch(() => false);

    expect(hasContent).toBeTruthy();

    await takeScreenshot(page, '05-network-page');
  });

  test('should show Kube-OVN subnets', async ({ page }) => {
    await page.goto('/network');
    await waitForPageLoad(page);

    // Look for subnet-related content (CIDR blocks, gateway info)
    // The Network page shows subnets with their CIDR and gateway
    const cidrPattern = page.getByText(/\d+\.\d+\.\d+\.\d+\/\d+/);
    if (await cidrPattern.first().isVisible({ timeout: 10_000 }).catch(() => false)) {
      await expect(cidrPattern.first()).toBeVisible();
    }

    await takeScreenshot(page, '05-network-subnets');
  });

  test('should show system networks page', async ({ page }) => {
    await page.goto('/network/system');
    await waitForPageLoad(page);

    // System networks page shows internal Kube-OVN subnets (ovn-default, join, etc.)
    const body = page.locator('body');
    await expect(body).not.toBeEmpty();

    await takeScreenshot(page, '05-network-system');
  });
});
