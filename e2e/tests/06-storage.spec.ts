import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot } from './helpers';

test.describe('Storage', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('should show storage images page', async ({ page }) => {
    await page.goto('/storage/images');
    await waitForPageLoad(page);

    // Page should load — might show images or empty state
    const body = page.locator('body');
    await expect(body).not.toBeEmpty();

    await takeScreenshot(page, '06-storage-images');
  });

  test('should show storage classes page', async ({ page }) => {
    await page.goto('/storage/classes');
    await waitForPageLoad(page);

    // Storage classes page should show available storage classes from the cluster
    const body = page.locator('body');
    await expect(body).not.toBeEmpty();

    // Look for storage class names or table content
    const hasContent =
      await page.getByText(/Storage Class/i).isVisible().catch(() => false) ||
      await page.locator('table').isVisible().catch(() => false) ||
      await page.getByText(/No storage/i).isVisible().catch(() => false);

    expect(hasContent).toBeTruthy();

    await takeScreenshot(page, '06-storage-classes');
  });
});
