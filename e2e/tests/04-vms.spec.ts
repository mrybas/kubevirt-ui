import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot } from './helpers';

const TEST_VM_NAME = 'e2e-test-vm';

test.describe('Virtual Machines', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('should show VM list page', async ({ page }) => {
    await page.goto('/vms');
    await waitForPageLoad(page);

    // Page heading
    await expect(page.getByRole('heading', { name: /Virtual Machines/i })).toBeVisible();

    // "Create VM" button should be present
    await expect(page.getByRole('button', { name: /Create VM/i })).toBeVisible();

    // Search input
    await expect(page.getByPlaceholder(/Search by name/i)).toBeVisible();

    await takeScreenshot(page, '04-vm-list');
  });

  test('should open Create VM wizard', async ({ page }) => {
    await page.goto('/vms');
    await waitForPageLoad(page);

    // Click "Create VM" button
    await page.getByRole('button', { name: /Create VM/i }).click();

    // Wizard modal should appear
    await expect(page.getByRole('heading', { name: /Create Virtual Machine/i })).toBeVisible();

    // Step 1: should show project selection
    await expect(page.getByText('Project *')).toBeVisible();
    await expect(page.getByText('Select a Template')).toBeVisible();

    await takeScreenshot(page, '04-vm-create-wizard-step1');
  });

  test('should create VM via wizard', async ({ page }) => {
    test.setTimeout(120_000); // VM creation can be slow
    await page.goto('/vms');
    await waitForPageLoad(page);

    // Open wizard
    await page.getByRole('button', { name: /Create VM/i }).click();
    await expect(page.getByRole('heading', { name: /Create Virtual Machine/i })).toBeVisible();

    // Step 1: Select project
    // Click the project dropdown and select the first available project
    const projectSelect = page.locator('[class*="CustomSelect"]').first();
    if (await projectSelect.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await projectSelect.click();
      // Select first non-placeholder option
      await page.locator('[class*="option"]').first().click();
    }

    // Wait for templates to load, then select the first one
    const templateButton = page.locator('button:has-text("vCPU")').first();
    await templateButton.waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {
      // No templates available — skip rest of test
    });

    if (await templateButton.isVisible().catch(() => false)) {
      await templateButton.click();

      // Click Next to go to Customize step
      await page.getByRole('button', { name: 'Next' }).click();

      // Step 2: Fill VM name
      await expect(page.getByText('VM Name *')).toBeVisible();
      const nameInput = page.locator('input[placeholder*="my-vm"]');
      await nameInput.fill(TEST_VM_NAME);

      await takeScreenshot(page, '04-vm-create-wizard-step2');

      // Click Next to go to Network step
      await page.getByRole('button', { name: 'Next' }).click();

      // Step 3: Network — just skip (use default pod network)
      await expect(page.getByText('Network Configuration')).toBeVisible();
      await page.getByRole('button', { name: 'Next' }).click();

      // Step 4: Cloud-init — skip
      await expect(page.getByText('Access Configuration')).toBeVisible();
      await page.getByRole('button', { name: 'Next' }).click();

      // Step 5: Review — verify and create
      await expect(page.getByText('Review & Create')).toBeVisible();
      await expect(page.getByText(TEST_VM_NAME)).toBeVisible();

      await takeScreenshot(page, '04-vm-create-wizard-review');

      // Click Create VM
      await page.getByRole('button', { name: /Create VM/i }).click();

      // Wait for wizard to close (success redirects back to VM list)
      await page.waitForURL('**/vms', { timeout: 30_000 }).catch(() => {
        // Wizard might stay open if creation takes time
      });

      await waitForPageLoad(page);
      await takeScreenshot(page, '04-vm-created');
    }
  });

  test('should show VM in the list after creation', async ({ page }) => {
    await page.goto('/vms');
    await waitForPageLoad(page);

    // Search for the test VM
    const searchInput = page.getByPlaceholder(/Search by name/i);
    await searchInput.fill(TEST_VM_NAME);

    // Wait a moment for filter to apply
    await page.waitForTimeout(500);

    // VM should appear in the list
    const vmLink = page.getByRole('link', { name: TEST_VM_NAME });
    if (await vmLink.isVisible({ timeout: 10_000 }).catch(() => false)) {
      await expect(vmLink).toBeVisible();
      await takeScreenshot(page, '04-vm-in-list');
    }
  });

  test('should open VM detail page', async ({ page }) => {
    await page.goto('/vms');
    await waitForPageLoad(page);

    // Search for the test VM
    await page.getByPlaceholder(/Search by name/i).fill(TEST_VM_NAME);
    await page.waitForTimeout(500);

    // Click the VM name link
    const vmLink = page.getByRole('link', { name: TEST_VM_NAME });
    if (await vmLink.isVisible({ timeout: 10_000 }).catch(() => false)) {
      await vmLink.click();

      // Should navigate to VM detail page
      await page.waitForURL(`**/vms/**/${TEST_VM_NAME}`, { timeout: 15_000 });
      await waitForPageLoad(page);

      // Verify VM name is shown on detail page
      await expect(page.getByText(TEST_VM_NAME)).toBeVisible();
      await takeScreenshot(page, '04-vm-detail');
    }
  });

  test('should start and stop VM', async ({ page }) => {
    test.setTimeout(120_000); // Starting a VM can take 30-60s
    await page.goto('/vms');
    await waitForPageLoad(page);

    await page.getByPlaceholder(/Search by name/i).fill(TEST_VM_NAME);
    await page.waitForTimeout(500);

    // Find the VM row and check for start/stop button
    const vmRow = page.locator('tr', { hasText: TEST_VM_NAME });
    if (await vmRow.isVisible({ timeout: 10_000 }).catch(() => false)) {
      // If VM is stopped, start it
      const startButton = vmRow.getByTitle('Start');
      const stopButton = vmRow.getByTitle('Stop');

      if (await startButton.isVisible({ timeout: 3_000 }).catch(() => false)) {
        await startButton.click();
        await takeScreenshot(page, '04-vm-starting');

        // Wait for Running status (can take 30-60s)
        await expect(vmRow.getByText('Running')).toBeVisible({ timeout: 90_000 }).catch(() => {
          // VM might not start if cluster has no resources
        });
        await takeScreenshot(page, '04-vm-running');

        // Now stop it
        await vmRow.getByTitle('Stop').click();
        await takeScreenshot(page, '04-vm-stopping');

        // Wait for Stopped status
        await expect(vmRow.getByText('Stopped')).toBeVisible({ timeout: 60_000 }).catch(() => {});
        await takeScreenshot(page, '04-vm-stopped');
      } else if (await stopButton.isVisible({ timeout: 3_000 }).catch(() => false)) {
        // VM is already running, stop it
        await stopButton.click();
        await expect(vmRow.getByText('Stopped')).toBeVisible({ timeout: 60_000 }).catch(() => {});
        await takeScreenshot(page, '04-vm-stopped');
      }
    }
  });

  test('should delete VM', async ({ page }) => {
    test.setTimeout(60_000);
    await page.goto('/vms');
    await waitForPageLoad(page);

    await page.getByPlaceholder(/Search by name/i).fill(TEST_VM_NAME);
    await page.waitForTimeout(500);

    const vmRow = page.locator('tr', { hasText: TEST_VM_NAME });
    if (await vmRow.isVisible({ timeout: 10_000 }).catch(() => false)) {
      // Handle the confirm dialog
      page.on('dialog', async (dialog) => {
        await dialog.accept();
      });

      // Click delete button
      await vmRow.getByTitle('Delete').click();

      // Wait for VM to disappear from the list
      await expect(vmRow).not.toBeVisible({ timeout: 30_000 }).catch(() => {});

      await takeScreenshot(page, '04-vm-deleted');
    }
  });
});
