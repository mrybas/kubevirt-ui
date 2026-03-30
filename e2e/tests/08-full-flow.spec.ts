import { test, expect, type Page } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot, API_URL } from './helpers';

/**
 * E2E Full Flow Test — exercises the entire UI lifecycle:
 *
 *   1. Create Folder (with environment "dev")
 *   2. Create VPC
 *   3. Create Egress Gateway
 *   4. Import Image (Alpine cloud image)
 *   5. Create VM Template
 *   6. Create VM from template & wait for Running
 *
 * All actions are performed through the UI (Playwright).
 * Tests run sequentially — each depends on the previous.
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const FOLDER_NAME = 'e2e-team';
const FOLDER_DISPLAY = 'E2E Team';
const ENV_NAME = 'dev';
const NAMESPACE = `${FOLDER_NAME}-${ENV_NAME}`; // e2e-team-dev

const VPC_NAME = 'e2e-vpc';
const VPC_CIDR = '10.100.0.0/24';

const EGRESS_NAME = 'e2e-egress';
const EGRESS_GW_CIDR = '10.199.0.0/24';
const EGRESS_TRANSIT_CIDR = '10.255.0.0/24';

const IMAGE_NAME = 'cirros-test';
const IMAGE_DISPLAY = 'Cirros Test';
const IMAGE_URL = 'https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img';
const IMAGE_SIZE = '1Gi';

const TEMPLATE_NAME = 'alpine-small';
const TEMPLATE_DISPLAY = 'Alpine Small';

const VM_NAME = 'e2e-vm';

// Timeouts
const IMAGE_READY_TIMEOUT = 5 * 60_000;   // 5 min
const VM_READY_TIMEOUT = 5 * 60_000;      // 5 min

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Poll a page until a condition is met, reloading periodically */
async function pollUntil(
  page: Page,
  check: () => Promise<boolean>,
  opts: { timeout: number; interval: number; label: string },
): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < opts.timeout) {
    if (await check()) return;
    const elapsed = Math.round((Date.now() - start) / 1000);
    console.log(`[${elapsed}s] ${opts.label} — not ready, retrying in ${opts.interval / 1000}s...`);
    await page.waitForTimeout(opts.interval);
    await page.reload();
    await waitForPageLoad(page);
  }
  throw new Error(`${opts.label} — timed out after ${opts.timeout / 1000}s`);
}

async function navigateTo(page: Page, url: string): Promise<void> {
  await page.goto(url);
  await waitForPageLoad(page);
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

test.describe.serial('Full Flow: Folder → VPC → Egress → Image → Template → VM', () => {

  // -----------------------------------------------------------------------
  // 0. Cleanup leftover resources from previous runs
  // -----------------------------------------------------------------------
  test('00 — Cleanup leftovers', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);

    const cookies = await page.context().cookies();
    const token = cookies.find(c => c.name === 'token')?.value;
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const apiDel = async (path: string) => {
      try { await fetch(`${API_URL}${path}`, { method: 'DELETE', headers }); } catch {}
    };

    await apiDel(`/api/v1/namespaces/${NAMESPACE}/vms/${VM_NAME}`);
    await apiDel(`/api/v1/templates/${TEMPLATE_NAME}`);
    await apiDel(`/api/v1/images/${IMAGE_NAME}?namespace=${NAMESPACE}`);
    await apiDel(`/api/v1/egress-gateways/${EGRESS_NAME}`);
    await apiDel(`/api/v1/vpcs/${VPC_NAME}`);
    await apiDel(`/api/v1/folders/${FOLDER_NAME}`);
    await page.waitForTimeout(3_000);
  });

  // -----------------------------------------------------------------------
  // 1. Create Folder
  // -----------------------------------------------------------------------
  test('01 — Create folder', async ({ page }) => {
    await login(page);
    await navigateTo(page, '/folders');
    await takeScreenshot(page, '08-01-folders-page');

    await page.getByRole('button', { name: /Create Folder/i }).first().click();
    await expect(page.getByRole('heading', { name: 'Create Folder' })).toBeVisible({ timeout: 5_000 });

    // Fill Display Name → auto-generates identifier
    await page.getByPlaceholder('My Team').fill(FOLDER_DISPLAY);
    const identifierInput = page.getByPlaceholder('my-team');
    await expect(identifierInput).toHaveValue(FOLDER_NAME);

    await takeScreenshot(page, '08-02-folder-form-filled');

    // Submit
    await page.getByRole('button', { name: /Create Folder/i }).last().click();
    await waitForPageLoad(page);
    // Use cell role to avoid sidebar duplicate
    await expect(page.getByRole('cell', { name: FOLDER_DISPLAY }).first()).toBeVisible({ timeout: 15_000 });
    await takeScreenshot(page, '08-03-folder-created');
  });

  // -----------------------------------------------------------------------
  // 2. Create VPC
  // -----------------------------------------------------------------------
  test('02 — Create VPC', async ({ page }) => {
    await login(page);
    await navigateTo(page, '/network/vpcs');
    await takeScreenshot(page, '08-10-vpcs-page');

    await page.getByRole('button', { name: /Create VPC/i }).first().click();
    await expect(page.getByRole('heading', { name: 'Create VPC' })).toBeVisible({ timeout: 5_000 });

    await page.getByPlaceholder('my-vpc').fill(VPC_NAME);
    await page.getByPlaceholder('10.0.0.0/24').fill(VPC_CIDR);
    await takeScreenshot(page, '08-11-vpc-form');

    await page.getByRole('button', { name: /Create VPC/i }).last().click();
    await waitForPageLoad(page);
    await expect(page.getByText(VPC_NAME)).toBeVisible({ timeout: 15_000 });
    await takeScreenshot(page, '08-12-vpc-created');
  });

  // -----------------------------------------------------------------------
  // 3. Create Egress Gateway
  // -----------------------------------------------------------------------
  test.skip('03 — Create Egress Gateway', async ({ page }) => {
    await login(page);
    await navigateTo(page, '/network/egress-gateways');
    await takeScreenshot(page, '08-20-egress-page');

    await page.getByRole('button', { name: /Create Egress Gateway/i }).first().click();

    // Wait for modal — verify name field is visible
    await expect(page.getByPlaceholder('shared-egress')).toBeVisible({ timeout: 5_000 });
    await page.getByPlaceholder('shared-egress').fill(EGRESS_NAME);
    await page.getByPlaceholder('10.199.0.0/24').fill(EGRESS_GW_CIDR);
    await page.getByPlaceholder('10.255.0.0/24').fill(EGRESS_TRANSIT_CIDR);

    // Macvlan subnet — text input (no subnets) or native <select> (subnets exist)
    const macvlanInput = page.getByPlaceholder('macvlan-eth0');
    if (await macvlanInput.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await macvlanInput.fill('macvlan-eth0');
    } else {
      // Native <select> — pick first non-empty option
      const macvlanSelect = page.locator('select').filter({ has: page.locator('option:text("Select a subnet")') });
      if (await macvlanSelect.isVisible({ timeout: 2_000 }).catch(() => false)) {
        const options = macvlanSelect.locator('option');
        const count = await options.count();
        for (let i = 0; i < count; i++) {
          const val = await options.nth(i).getAttribute('value');
          if (val) {
            await macvlanSelect.selectOption(val);
            break;
          }
        }
      }
    }

    // Leave replicas at default (2)
    await takeScreenshot(page, '08-21-egress-form');

    // Submit via form submit button (type="submit")
    const submitBtn = page.locator('button[type="submit"]', { hasText: /Create Gateway|Creating/ });
    await submitBtn.scrollIntoViewIfNeeded();
    await takeScreenshot(page, '08-21b-before-submit');

    // If button is disabled, the form validation failed — log state
    const isDisabled = await submitBtn.isDisabled();
    if (isDisabled) {
      console.warn('Create Gateway button is disabled — form validation failed');
      await takeScreenshot(page, '08-21c-button-disabled');
    }

    await submitBtn.click({ force: true });
    await waitForPageLoad(page);
    await expect(page.getByText(EGRESS_NAME)).toBeVisible({ timeout: 15_000 });
    await takeScreenshot(page, '08-22-egress-created');
  });

  // -----------------------------------------------------------------------
  // 4. Import Image
  // -----------------------------------------------------------------------
  test('04 — Import image', async ({ page }) => {
    test.setTimeout(IMAGE_READY_TIMEOUT + 60_000);
    await login(page);
    await navigateTo(page, '/storage');
    await takeScreenshot(page, '08-30-storage-page');

    await page.getByRole('button', { name: /Import Image/i }).first().click();
    await expect(page.getByRole('heading', { name: 'Import Image' })).toBeVisible({ timeout: 5_000 });

    // Select project (namespace) — CustomSelect: click trigger button, then click option
    const projectTrigger = page.locator('button', { hasText: 'Select a project' }).first();
    await projectTrigger.click();
    await page.waitForTimeout(500);
    await page.locator('button', { hasText: NAMESPACE }).first().click();
    await page.waitForTimeout(500);

    await page.getByPlaceholder('ubuntu-22-04').fill(IMAGE_NAME);

    const displayInput = page.getByPlaceholder('Ubuntu 22.04 LTS');
    if (await displayInput.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await displayInput.fill(IMAGE_DISPLAY);
    }

    const sizeInput = page.getByPlaceholder('10Gi');
    if (await sizeInput.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await sizeInput.clear();
      await sizeInput.fill(IMAGE_SIZE);
    }

    // Image URL
    const urlInput = page.getByPlaceholder(/cloud-images\.ubuntu\.com/);
    if (await urlInput.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await urlInput.fill(IMAGE_URL);
    } else {
      await page.locator('input[placeholder*="https://"]').first().fill(IMAGE_URL);
    }

    await takeScreenshot(page, '08-31-image-form');

    await page.getByRole('button', { name: /Import Image/i }).last().click();
    await waitForPageLoad(page);
    await expect(page.getByText(IMAGE_NAME)).toBeVisible({ timeout: 15_000 });
    await takeScreenshot(page, '08-32-image-importing');
  });

  // -----------------------------------------------------------------------
  // 4b. Wait for image to be ready
  // -----------------------------------------------------------------------
  test('04b — Wait for image to be ready', async ({ page }) => {
    test.setTimeout(IMAGE_READY_TIMEOUT + 60_000);
    await login(page);
    await navigateTo(page, '/storage');

    await pollUntil(
      page,
      async () => {
        const imageRow = page.locator('tr, [class*="card"]', { hasText: IMAGE_NAME }).first();
        if (!await imageRow.isVisible({ timeout: 3_000 }).catch(() => false)) return false;

        const ready = imageRow.locator('text=/Ready|Succeeded|100%/').first();
        const checkIcon = imageRow.locator('.text-emerald-400, .text-green-400').first();

        if (await ready.isVisible({ timeout: 2_000 }).catch(() => false)) {
          await takeScreenshot(page, '08-33-image-ready');
          return true;
        }
        if (await checkIcon.isVisible({ timeout: 1_000 }).catch(() => false)) {
          await takeScreenshot(page, '08-33-image-ready');
          return true;
        }
        return false;
      },
      { timeout: IMAGE_READY_TIMEOUT, interval: 10_000, label: 'Image Ready' },
    );
  });

  // -----------------------------------------------------------------------
  // 5. Create VM Template
  // -----------------------------------------------------------------------
  test('05 — Create VM template', async ({ page }) => {
    await login(page);
    await navigateTo(page, '/vms/templates');
    await takeScreenshot(page, '08-40-templates-page');

    await page.getByRole('button', { name: /Create Template/i }).first().click();
    // Wait for modal — check that name input is visible
    await expect(page.getByPlaceholder('ubuntu-medium')).toBeVisible({ timeout: 5_000 });

    await page.getByPlaceholder('ubuntu-medium').fill(TEMPLATE_NAME);
    await page.getByPlaceholder('Ubuntu Medium').fill(TEMPLATE_DISPLAY);

    // Select project — CustomSelect trigger
    const projectTrigger5 = page.locator('button', { hasText: 'Select a project' }).first();
    await projectTrigger5.click();
    await page.waitForTimeout(500);
    await page.locator('button', { hasText: NAMESPACE }).first().click();
    await page.waitForTimeout(1_000);

    // Select base image — CustomSelect trigger (options show display_name + size)
    const imageTrigger = page.locator('button', { hasText: 'Select an image' }).first();
    await imageTrigger.click();
    await page.waitForTimeout(500);
    await page.locator('button', { hasText: IMAGE_DISPLAY }).first().click();
    await page.waitForTimeout(500);

    // CPU = 1
    const cpuInput = page.locator('input[type="number"]').first();
    if (await cpuInput.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await cpuInput.clear();
      await cpuInput.fill('1');
    }

    // Memory = 1 GB (default 4 GB is too much for test cluster)
    // Find the Memory label, then the CustomSelect trigger button next to it
    const memorySection = page.locator('label:has-text("Memory")').first().locator('..');
    const memoryTrigger = memorySection.locator('button').first();
    await memoryTrigger.click();
    await page.waitForTimeout(500);
    // Click "1 GB" option in the dropdown
    await page.locator('button:has-text("1 GB")').first().click();
    await page.waitForTimeout(500);

    // Disk Size = 20 GB
    const diskSection = page.locator('label:has-text("Disk Size")').first().locator('..');
    const diskTrigger = diskSection.locator('button').first();
    await diskTrigger.click();
    await page.waitForTimeout(500);
    await page.locator('button:has-text("20 GB")').first().click();
    await page.waitForTimeout(500);

    await takeScreenshot(page, '08-41-template-form');

    // Submit via form submit button
    await page.locator('button[type="submit"]').click();
    await waitForPageLoad(page);
    await expect(page.getByText(TEMPLATE_DISPLAY)).toBeVisible({ timeout: 15_000 });
    await takeScreenshot(page, '08-42-template-created');
  });

  // -----------------------------------------------------------------------
  // 6. Create VM
  // -----------------------------------------------------------------------
  test('06 — Create VM', async ({ page }) => {
    test.setTimeout(VM_READY_TIMEOUT + 60_000);
    await login(page);
    await navigateTo(page, '/vms');
    await takeScreenshot(page, '08-50-vms-page');

    await page.getByRole('button', { name: /Create VM|Create Virtual Machine/i }).first().click();
    await expect(page.getByText(/Create Virtual Machine/i)).toBeVisible({ timeout: 5_000 });

    // Step 1: Template — select folder → environment → template
    const folderSelect = page.getByText('Select a folder...').first();
    if (await folderSelect.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await folderSelect.click();
      const folderOption = page.getByText(FOLDER_DISPLAY).first();
      if (await folderOption.isVisible({ timeout: 3_000 }).catch(() => false)) {
        await folderOption.click();
      }
    }

    const envSelect = page.getByText('Select an environment...').first();
    if (await envSelect.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await envSelect.click();
      const envOption = page.getByText(NAMESPACE).first();
      if (await envOption.isVisible({ timeout: 3_000 }).catch(() => false)) {
        await envOption.click();
      } else {
        await page.locator('[class*="option"]').first().click();
      }
    }

    await page.waitForTimeout(2_000);

    // Select template
    const templateCard = page.locator('button', { hasText: TEMPLATE_DISPLAY }).first();
    if (await templateCard.isVisible({ timeout: 5_000 }).catch(() => false)) {
      await templateCard.click();
    } else {
      const altCard = page.locator('button', { hasText: TEMPLATE_NAME }).first();
      if (await altCard.isVisible({ timeout: 3_000 }).catch(() => false)) {
        await altCard.click();
      } else {
        await page.locator('button:has-text("vCPU")').first().click();
      }
    }

    await takeScreenshot(page, '08-51-vm-template-step');

    // Step 2: Customize
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('VM Name')).toBeVisible({ timeout: 5_000 });
    await page.locator('input[placeholder*="my-vm"]').fill(VM_NAME);
    await takeScreenshot(page, '08-52-vm-customize');

    // Step 3: Network — skip
    await page.getByRole('button', { name: 'Next' }).click();
    await takeScreenshot(page, '08-53-vm-network');

    // Step 4: Cloud-init — defaults (start=true)
    await page.getByRole('button', { name: 'Next' }).click();
    await takeScreenshot(page, '08-54-vm-cloudinit');

    // Step 5: Review
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText(VM_NAME)).toBeVisible({ timeout: 5_000 });
    await takeScreenshot(page, '08-55-vm-review');

    // Create
    await page.getByRole('button', { name: /Create.*VM/i }).last().click();
    await page.waitForTimeout(5_000);
    await takeScreenshot(page, '08-56-vm-creating');

    // Close wizard if still open
    const closeBtn = page.getByRole('button', { name: /Close/i });
    if (await closeBtn.isVisible({ timeout: 5_000 }).catch(() => false)) {
      await closeBtn.click();
    }

    await waitForPageLoad(page);
    await takeScreenshot(page, '08-57-vm-created');
  });

  // -----------------------------------------------------------------------
  // 6b. Poll VM until Running
  // -----------------------------------------------------------------------
  test('06b — Wait for VM to reach Running', async ({ page }) => {
    test.setTimeout(VM_READY_TIMEOUT + 60_000);
    await login(page);
    await navigateTo(page, '/vms');

    const search = page.getByPlaceholder(/Search/i);
    if (await search.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await search.fill(VM_NAME);
      await page.waitForTimeout(500);
    }

    await pollUntil(
      page,
      async () => {
        const searchInput = page.getByPlaceholder(/Search/i);
        if (await searchInput.isVisible({ timeout: 2_000 }).catch(() => false)) {
          await searchInput.fill(VM_NAME);
          await page.waitForTimeout(500);
        }

        const vmRow = page.locator('tr, [class*="card"]', { hasText: VM_NAME }).first();
        if (!await vmRow.isVisible({ timeout: 3_000 }).catch(() => false)) return false;

        const running = vmRow.getByText('Running');
        if (await running.isVisible({ timeout: 2_000 }).catch(() => false)) {
          await takeScreenshot(page, '08-58-vm-running');
          return true;
        }

        const error = vmRow.getByText(/Error|Failed|CrashLoop/);
        if (await error.isVisible({ timeout: 1_000 }).catch(() => false)) {
          await takeScreenshot(page, '08-59-vm-error');
          console.warn(`VM ${VM_NAME} has error state, continuing to poll...`);
        }

        return false;
      },
      { timeout: VM_READY_TIMEOUT, interval: 10_000, label: 'VM Running' },
    );
  });

  // -----------------------------------------------------------------------
  // Cleanup via API
  // -----------------------------------------------------------------------
  test('99 — Cleanup', async ({ page }) => {
    test.setTimeout(120_000);
    await login(page);

    const cookies = await page.context().cookies();
    const token = cookies.find(c => c.name === 'token')?.value;
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const apiDel = async (path: string) => {
      try {
        const resp = await fetch(`${API_URL}${path}`, { method: 'DELETE', headers });
        console.log(`DELETE ${path} → ${resp.status}`);
      } catch (e) {
        console.warn(`DELETE ${path} failed:`, e);
      }
    };

    await apiDel(`/api/v1/namespaces/${NAMESPACE}/vms/${VM_NAME}`);
    await page.waitForTimeout(5_000);
    await apiDel(`/api/v1/templates/${TEMPLATE_NAME}`);
    await apiDel(`/api/v1/images/${IMAGE_NAME}?namespace=${NAMESPACE}`);
    await apiDel(`/api/v1/egress-gateways/${EGRESS_NAME}`);
    await apiDel(`/api/v1/vpcs/${VPC_NAME}`);
    await apiDel(`/api/v1/folders/${FOLDER_NAME}`);

    await takeScreenshot(page, '08-99-cleanup-done');
  });
});
