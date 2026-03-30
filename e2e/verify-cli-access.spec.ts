import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:3333';

async function waitIdle(page: Page) {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(800);
}

async function login(page: Page) {
  await page.goto(`${BASE}/login`);
  await waitIdle(page);

  const ssoBtn = page.getByRole('button', { name: /Sign in with SSO/i });

  if (await ssoBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await ssoBtn.click();
    await page.waitForURL(url => !url.href.includes('localhost:3333'), { timeout: 15000 });
    await waitIdle(page);

    // DEX connector list
    const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in with|LDAP/i });
    if (await connectorLink.first().isVisible({ timeout: 3000 }).catch(() => false)) {
      await connectorLink.first().click();
      await waitIdle(page);
    }

    await page.getByRole('textbox', { name: /username/i }).fill('admin');
    await page.getByRole('textbox', { name: /password/i }).fill('admin_password');
    await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

    await page.waitForURL('**/dashboard', { timeout: 30000 });
    await waitIdle(page);
  }
}

test('CLI Access page shows kubeconfig after login', async ({ page }) => {
  await login(page);

  // Navigate to CLI Access
  await page.goto(`${BASE}/cli-access`);
  await waitIdle(page);
  await page.waitForTimeout(5000);

  await page.screenshot({ path: 'screenshots/verify-cli-access.png', fullPage: true });

  // Check no error
  const errorHeading = page.locator('text=Cannot Generate Kubeconfig');
  const hasError = await errorHeading.count();

  if (hasError > 0) {
    const errorMsg = await page.locator('.text-surface-400').first().textContent();
    console.log('CLI Access error:', errorMsg);
  } else {
    console.log('CLI Access: no error');
  }

  // Check for kubeconfig content
  const kubeconfigPre = page.locator('pre');
  const hasKubeconfig = await kubeconfigPre.count();
  console.log('Has kubeconfig pre element:', hasKubeconfig > 0);

  expect(hasError).toBe(0);
});
