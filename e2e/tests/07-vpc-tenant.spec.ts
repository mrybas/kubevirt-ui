import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot, navigateVia } from './helpers';

/**
 * E2E test: VPC network creation + tenant cluster provisioning
 *
 * Flow:
 * 1. Login via OIDC
 * 2. Navigate to /network → create VPC "test-vpc" with NAT gateway
 * 3. Navigate to /tenants → create tenant "team-alpha" with network isolation
 * 4. Poll tenant detail page until status = Ready (up to 20 min)
 * 5. Verify workers ready, Calico and Namespaces addons reconciled
 */

const VPC_NAME = 'test-vpc';
const TENANT_NAME = 'team-alpha';
const TENANT_DISPLAY_NAME = 'Team Alpha';
const READY_TIMEOUT_MS = 20 * 60 * 1000; // 20 minutes
const POLL_INTERVAL_MS = 20 * 1000;       // 20 seconds

test.describe('VPC and Tenant Creation', () => {
  test.setTimeout(READY_TIMEOUT_MS + 60_000); // extra buffer

  test('should create VPC network', async ({ page }) => {
    await login(page);

    // Navigate to the Network page
    await page.goto('/network');
    await waitForPageLoad(page);
    await takeScreenshot(page, '07-01-network-page');

    // Click "Create Network" button (header button, first match)
    await page.getByRole('button', { name: /Create Network/i }).first().click();

    // The wizard opens — step 1: Type
    await expect(page.getByText('Select Network Type')).toBeVisible({ timeout: 10_000 });
    await takeScreenshot(page, '07-02-wizard-type-step');

    // Select "VPC Network" type
    await page.getByRole('button', { name: /VPC Network/i }).click();

    // Verify VPC Network is selected (CheckCircle appears next to it)
    await takeScreenshot(page, '07-03-vpc-type-selected');

    // Click Next → VPC Config step
    await page.getByRole('button', { name: /Next/i }).click();

    // Step: VPC Configuration
    await expect(page.getByText('VPC Configuration')).toBeVisible({ timeout: 10_000 });
    await takeScreenshot(page, '07-04-vpc-config-step');

    // Fill VPC Name (label has no htmlFor/id, use placeholder)
    const vpcNameInput = page.getByPlaceholder('my-vpc');
    await vpcNameInput.clear();
    await vpcNameInput.fill(VPC_NAME);

    // NAT Gateway is enabled by default (vpcEnableNat: true in initialState)
    // Verify the toggle is in the "on" position (bg-primary-500 class)
    // The toggle button area shows "NAT Gateway" label
    const natSection = page.locator('div').filter({ hasText: /NAT Gateway/ }).last();
    await expect(natSection).toBeVisible();
    await takeScreenshot(page, '07-05-vpc-config-filled');

    // Click Next → VPC Peering step
    await page.getByRole('button', { name: /Next/i }).click();

    // Step: VPC Peering
    await expect(page.getByText('VPC Peering')).toBeVisible({ timeout: 10_000 });
    await takeScreenshot(page, '07-06-vpc-peering-step');

    // Leave peering defaults and go to Review
    await page.getByRole('button', { name: /Next/i }).click();

    // Step: vpc-review
    await expect(page.getByText('Review VPC Configuration')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(VPC_NAME).first()).toBeVisible();
    await takeScreenshot(page, '07-07-vpc-review');

    // Submit — "Create VPC" button
    await page.getByRole('button', { name: /Create VPC/i }).click();

    // Wizard closes; wait for VPC to appear in the list
    await waitForPageLoad(page);
    await takeScreenshot(page, '07-08-after-vpc-create');

    // VPC Networks section should appear with our VPC
    await expect(page.getByText('VPC Networks')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('heading', { name: VPC_NAME })).toBeVisible({ timeout: 30_000 });
    await takeScreenshot(page, '07-09-vpc-in-list');
  });

  test('should create tenant with network isolation', async ({ page }) => {
    await login(page);

    // Navigate to Tenants page
    await page.goto('/tenants');
    await waitForPageLoad(page);
    await takeScreenshot(page, '07-10-tenants-page');

    // Click "New Tenant" button
    await page.getByRole('button', { name: /New Tenant/i }).click();

    // Wizard opens — step 0: Basics
    await expect(page.getByText('Create Tenant')).toBeVisible({ timeout: 10_000 });
    await takeScreenshot(page, '07-11-tenant-basics-step');

    // Fill Tenant Name
    await page.getByPlaceholder('my-tenant').fill(TENANT_NAME);

    // Fill Display Name
    await page.getByPlaceholder('My Tenant Cluster').fill(TENANT_DISPLAY_NAME);

    // Leave Kubernetes version and Control Plane Replicas at defaults
    await takeScreenshot(page, '07-12-tenant-basics-filled');

    // Click Next → Workers step
    await page.getByRole('button', { name: /Next/i }).click();

    // Step 1: Workers
    await expect(page.getByText('Worker Count')).toBeVisible({ timeout: 10_000 });
    await takeScreenshot(page, '07-13-workers-step');

    // Worker type should default to "Virtual Machine" (already selected)
    await expect(page.getByText('Virtual Machine', { exact: true })).toBeVisible();

    // Reduce worker count to 1
    const workerCountInput = page.locator('div:has(label:text-is("Worker Count")) input[type="number"]').first();
    await workerCountInput.clear();
    await workerCountInput.fill('1');

    // Fill worker OS image URL (viewport enlarged to 1024px height so input is visible)
    const imageUrlInput = page.locator('input[placeholder*="cloud-images.ubuntu.com"]');
    await imageUrlInput.fill('https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img');

    await takeScreenshot(page, '07-14-workers-filled');

    // Click Next → Addons step
    await page.getByRole('button', { name: /Next/i }).click();

    // Step 2: Addons — just proceed
    await takeScreenshot(page, '07-15-addons-step');
    await page.getByRole('button', { name: /Next/i }).click();

    // Step 3: Network
    await expect(page.getByText('Network Isolation (VPC)')).toBeVisible({ timeout: 10_000 });
    await takeScreenshot(page, '07-16-network-step');

    // Enable network isolation (VPC) toggle
    // The toggle button is a sibling of the h3 — find h3 then navigate to the adjacent button
    const isolationToggle = page.locator('h3:has-text("Network Isolation (VPC)")').locator('xpath=../../button');
    await isolationToggle.click();
    await takeScreenshot(page, '07-17-network-isolation-enabled');

    // Verify summary shows "VPC (isolated)"
    await expect(page.getByText('VPC (isolated)')).toBeVisible({ timeout: 5_000 });

    // Submit — "Create Tenant" button
    await page.getByRole('button', { name: /Create Tenant/i }).click();

    // Wait for wizard to close and tenant to appear in list
    await waitForPageLoad(page);
    await takeScreenshot(page, '07-18-after-tenant-create');

    // Tenant should appear in the list
    await expect(page.getByText(TENANT_DISPLAY_NAME)).toBeVisible({ timeout: 30_000 });
    await takeScreenshot(page, '07-19-tenant-in-list');
  });

  test('should poll tenant until Ready status', async ({ page }) => {
    await login(page);

    // Navigate directly to tenant detail page
    await page.goto(`/tenants/${TENANT_NAME}`);
    await waitForPageLoad(page);

    const startTime = Date.now();
    let isReady = false;

    while (!isReady && Date.now() - startTime < READY_TIMEOUT_MS) {
      // Reload to get fresh status
      await page.reload();
      await waitForPageLoad(page);

      // Check for "Ready" status badge
      const readyBadge = page.locator('span').filter({ hasText: /^Ready$/ }).first();
      const failedBadge = page.locator('span').filter({ hasText: /^Failed$/ }).first();

      if (await failedBadge.isVisible({ timeout: 2_000 }).catch(() => false)) {
        await takeScreenshot(page, '07-20-tenant-failed');
        throw new Error(`Tenant ${TENANT_NAME} reached Failed status`);
      }

      if (await readyBadge.isVisible({ timeout: 2_000 }).catch(() => false)) {
        isReady = true;
        await takeScreenshot(page, '07-21-tenant-ready');
        break;
      }

      const elapsed = Math.round((Date.now() - startTime) / 1000);
      console.log(`[${elapsed}s] Tenant not ready yet, polling again in ${POLL_INTERVAL_MS / 1000}s...`);
      await takeScreenshot(page, `07-poll-${elapsed}s`);

      // Wait before next poll
      await page.waitForTimeout(POLL_INTERVAL_MS);
    }

    expect(isReady, `Tenant ${TENANT_NAME} did not become Ready within 20 minutes`).toBe(true);

    // --- Stronger acceptance criteria after Ready status ---

    // 1. Verify at least 1 worker node is visible
    // The Workers info card shows "{workers_ready} / {worker_count}" pattern
    // The Workers section text shows "{N} of {M} workers ready"
    const workersText = page.locator('text=/\\d+ of \\d+ workers ready/');
    await expect(workersText).toBeVisible({ timeout: 10_000 });
    const workersContent = await workersText.textContent();
    const workersMatch = workersContent?.match(/(\d+) of (\d+) workers ready/);
    expect(workersMatch, 'Could not parse workers ready count').toBeTruthy();
    const workersReady = parseInt(workersMatch![1], 10);
    expect(workersReady, 'Expected at least 1 worker node to be ready').toBeGreaterThanOrEqual(1);
    console.log(`Workers ready: ${workersMatch![1]} / ${workersMatch![2]}`);
    await takeScreenshot(page, '07-22-workers-verified');

    // 2. Verify Calico addon shows as reconciled/ready
    const addonsSection = page.locator('.card', { has: page.locator('h2:has-text("Addons")') });
    await expect(addonsSection).toBeVisible({ timeout: 10_000 });

    // Each addon row contains the addon name and a "Reconciled" or "Reconciling..." status text
    const calicoRow = addonsSection.locator('div.flex', { hasText: /calico/i }).first();
    await expect(calicoRow).toBeVisible({ timeout: 10_000 });
    await expect(calicoRow.locator('text=Reconciled')).toBeVisible({ timeout: 10_000 });
    console.log('Calico addon: Reconciled');
    await takeScreenshot(page, '07-23-calico-reconciled');

    // 3. Verify Namespaces addon shows as reconciled/ready
    const namespacesRow = addonsSection.locator('div.flex', { hasText: /namespaces/i }).first();
    await expect(namespacesRow).toBeVisible({ timeout: 10_000 });
    await expect(namespacesRow.locator('text=Reconciled')).toBeVisible({ timeout: 10_000 });
    console.log('Namespaces addon: Reconciled');
    await takeScreenshot(page, '07-24-namespaces-reconciled');
  });
});
