import { type Page, expect } from '@playwright/test';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const BASE_URL = process.env.BASE_URL || 'http://frontend:3000';
export const API_URL = process.env.API_URL || 'http://backend:8000';
export const DEX_ISSUER = process.env.DEX_ISSUER || 'http://dex:5556';
export const LLDAP_URL = process.env.LLDAP_URL || 'http://lldap:17170';

export const CREDENTIALS = {
  username: process.env.LLDAP_ADMIN_USER || 'admin',
  password: process.env.LLDAP_ADMIN_PASSWORD || 'admin_password',
};

export const AUTH_STATE_PATH = './results/.auth/state.json';

// ---------------------------------------------------------------------------
// Login via OIDC (DEX + LLDAP)
// ---------------------------------------------------------------------------

export async function login(page: Page): Promise<void> {
  await page.goto('/', { timeout: 60_000 });
  await page.waitForURL('**/login', { timeout: 30_000 });

  await page.getByRole('button', { name: /Sign in with SSO/i }).click();

  await page.waitForURL(url => !url.href.includes('frontend') && !url.href.includes('localhost:3333'), { timeout: 15_000 });

  const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in/i });
  if (await connectorLink.isVisible({ timeout: 3_000 }).catch(() => false)) {
    await connectorLink.click();
  }

  await page.getByRole('textbox', { name: /username/i }).fill(CREDENTIALS.username);
  await page.getByRole('textbox', { name: /password/i }).fill(CREDENTIALS.password);
  await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

  await page.waitForURL('**/dashboard', { timeout: 30_000 });
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
}

// ---------------------------------------------------------------------------
// Wait for React app to finish loading
// ---------------------------------------------------------------------------

export async function waitForPageLoad(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const spinners = document.querySelectorAll('.animate-spin');
    return spinners.length === 0;
  }, { timeout: 30_000 }).catch(() => {});

  await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
}

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------

export async function apiRequest(
  endpoint: string,
  options: { method?: string; body?: unknown; token?: string } = {}
): Promise<Response> {
  const { method = 'GET', body, token } = options;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  return fetch(`${API_URL}${endpoint}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
}

// ---------------------------------------------------------------------------
// Navigation helpers
// ---------------------------------------------------------------------------

export async function navigateVia(page: Page, linkText: string, expectedUrlPart: string): Promise<void> {
  const link = page.getByRole('link', { name: linkText, exact: true });

  if (!await link.isVisible({ timeout: 2_000 }).catch(() => false)) {
    const parentButton = page.locator('aside button', { hasText: linkText });
    if (await parentButton.isVisible({ timeout: 1_000 }).catch(() => false)) {
      await parentButton.click();
    }
  }

  await link.click();
  await page.waitForURL(`**${expectedUrlPart}`, { timeout: 15_000 });
  await waitForPageLoad(page);
}

// ---------------------------------------------------------------------------
// Screenshot helper
// ---------------------------------------------------------------------------

export async function takeScreenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `./results/screenshots/${name}.png`, fullPage: true });
}
