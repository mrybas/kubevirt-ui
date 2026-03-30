import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:3333';
const SC_DIR = '/work/screenshots/full-flow';

// ──────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────

let stepIdx = 0;
function sc(label: string) {
  stepIdx++;
  return `${SC_DIR}/${String(stepIdx).padStart(2, '0')}-${label}.png`;
}

const findings: { step: string; status: 'OK' | 'WARN' | 'ERROR'; note: string }[] = [];

function log(step: string, status: 'OK' | 'WARN' | 'ERROR', note: string) {
  findings.push({ step, status, note });
  console.log(`[${status}] ${step}: ${note}`);
}

async function screenshot(page: Page, label: string, note = '') {
  const path = sc(label);
  await page.screenshot({ path, fullPage: true });
  if (note) log(label, 'OK', note);
  return path;
}

async function waitIdle(page: Page) {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(800);
}

// ──────────────────────────────────────────────
// Login via DEX / LLDAP
// ──────────────────────────────────────────────

async function login(page: Page) {
  await page.goto(`${BASE}/login`);
  await waitIdle(page);

  // Check for SSO button OR direct form
  const ssoBtn = page.getByRole('button', { name: /Sign in with SSO/i });
  const directUser = page.getByRole('textbox', { name: /username/i });

  if (await ssoBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await screenshot(page, 'login-page', 'Login page with SSO button');
    await ssoBtn.click();

    // Wait for redirect to DEX
    await page.waitForURL(url => !url.href.includes('localhost:3333'), { timeout: 15000 });
    await waitIdle(page);

    // DEX might show connector list — click the LDAP connector
    const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in with|LDAP/i });
    if (await connectorLink.first().isVisible({ timeout: 3000 }).catch(() => false)) {
      await connectorLink.first().click();
      await waitIdle(page);
    }

    await screenshot(page, 'dex-login-form', 'DEX login form');

    await page.getByRole('textbox', { name: /username/i }).fill('admin');
    await page.getByRole('textbox', { name: /password/i }).fill('admin_password');
    await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

    await page.waitForURL('**/dashboard', { timeout: 30000 });
    await waitIdle(page);
    log('login', 'OK', 'Logged in via DEX/LLDAP as admin');
  } else if (await directUser.isVisible({ timeout: 3000 }).catch(() => false)) {
    // Direct login form
    await screenshot(page, 'login-page', 'Direct login form');
    await directUser.fill('admin');
    await page.getByRole('textbox', { name: /password/i }).fill('admin123');
    await page.getByRole('button', { name: /Login|Sign in/i }).click();
    await page.waitForURL('**/dashboard', { timeout: 30000 });
    await waitIdle(page);
    log('login', 'OK', 'Logged in via direct form');
  } else {
    log('login', 'ERROR', 'Could not find login form');
    throw new Error('Login failed: no form found');
  }
}

// ──────────────────────────────────────────────
// TEST SUITE
// ──────────────────────────────────────────────

test.describe.serial('Full E2E Flow', () => {

  // ── STEP 1: Dashboard ──
  test('01: Dashboard', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/dashboard`);
    await waitIdle(page);
    await screenshot(page, 'dashboard', 'Dashboard loaded');

    const title = await page.title();
    log('dashboard', 'OK', `Title: "${title}"`);
  });

  // ── STEP 2: Create Folder + Environment ──
  test('02: Create Folder with Environment', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/folders`);
    await waitIdle(page);
    await screenshot(page, 'folders-list', 'Folders page before create');

    const createBtn = page.getByRole('button', { name: /Create Folder/i });
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await createBtn.click();
    await page.waitForTimeout(500);
    await screenshot(page, 'folder-create-modal-open', 'Create Folder modal open');

    // Fill "Folder Name" (display name — auto-populates slug via handleDisplayNameChange)
    const displayNameInput = page.locator('input[placeholder="My Team"]');
    await displayNameInput.fill('E2E Test Folder');
    await page.waitForTimeout(300);

    // The slug (Identifier) should auto-populate. Override it to be safe.
    const slugInput = page.locator('input[placeholder="my-team"]');
    if (await slugInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await slugInput.clear();
      await slugInput.fill('e2e-test');
    }

    // Check environment (should have 'dev' by default)
    await screenshot(page, 'folder-create-modal-filled', 'Folder modal filled');

    // Add second environment 'staging'
    const addEnvBtn = page.getByRole('button', { name: /Add/i }).or(page.locator('button').filter({ hasText: /\+/ })).first();
    if (await addEnvBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await addEnvBtn.click();
      const envInputs = page.locator('input[placeholder*="env"]');
      const count = await envInputs.count();
      if (count > 0) {
        await envInputs.last().fill('staging');
        log('folder-create', 'OK', 'Added staging environment');
      }
    }

    // Submit
    const submitBtn = page.getByRole('button', { name: /Create Folder/i }).last();
    await submitBtn.click();
    await page.waitForTimeout(3000);
    await waitIdle(page);
    await screenshot(page, 'folder-created', 'After folder creation');

    // Check success
    const folderVisible = await page.getByText('e2e-test').isVisible({ timeout: 5000 }).catch(() => false)
      || await page.getByText('E2E Test Folder').isVisible({ timeout: 2000 }).catch(() => false);
    if (folderVisible) {
      log('folder-create', 'OK', 'Folder "e2e-test" created successfully');
    } else {
      log('folder-create', 'WARN', 'Folder may have been created but not visible immediately');
    }
  });

  // ── STEP 3: Import Image ──
  test('03: Import Image from URL', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/storage/images`);
    await waitIdle(page);
    await screenshot(page, 'storage-images', 'Storage images page');

    const importBtn = page.getByRole('button', { name: /Import Image/i }).first();
    await expect(importBtn).toBeVisible({ timeout: 10000 });
    await importBtn.click();
    await page.waitForTimeout(600);
    await screenshot(page, 'import-image-modal', 'Import Image modal open');

    // Select project — CustomSelect trigger is a button containing "Select a project..."
    const projectTrigger = page.getByRole('button', { name: /Select a project/i });
    if (await projectTrigger.isVisible({ timeout: 3000 }).catch(() => false)) {
      await projectTrigger.click();
      await page.waitForTimeout(300);
      // Click first non-placeholder option in the dropdown
      // Options are buttons inside the dropdown (they appear after the trigger is clicked)
      const dropdownOption = page.locator('div.absolute button').filter({ hasNotText: /Select a project/i }).first();
      if (await dropdownOption.isVisible({ timeout: 3000 }).catch(() => false)) {
        const projectName = await dropdownOption.textContent() || 'unknown';
        await dropdownOption.click();
        await page.waitForTimeout(300);
        log('import-image', 'OK', `Selected project: ${projectName.trim()}`);
      } else {
        log('import-image', 'WARN', 'No project options available in dropdown');
      }
    } else {
      log('import-image', 'WARN', 'Project selector not found');
    }

    // Fill image name
    const nameInput = page.locator('input[placeholder="ubuntu-22-04"]');
    await nameInput.fill('alpine-3-19');

    // Click Alpine 3.19 quick-fill (sets sourceUrl + displayName)
    const alpineBtn = page.getByRole('button', { name: /Alpine 3\.19/i });
    if (await alpineBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await alpineBtn.click();
      log('import-image', 'OK', 'Alpine 3.19 quick-fill clicked');
    } else {
      // Fallback: fill URL manually
      const urlInput = page.locator('input[placeholder*="cloud-images"]');
      await urlInput.fill('https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/cloud/nocloud_alpine-3.19.1-x86_64-bios-cloudinit-r0.qcow2');
    }

    await screenshot(page, 'import-image-filled', 'Import Image modal filled');

    // Submit
    const submitBtn = page.getByRole('button', { name: /^Import Image$/i }).last();
    const isEnabled = await submitBtn.isEnabled({ timeout: 3000 }).catch(() => false);
    if (isEnabled) {
      await submitBtn.click();
      await page.waitForTimeout(4000);
      await waitIdle(page);
      await screenshot(page, 'import-image-submitted', 'After image import submission');
      log('import-image', 'OK', 'Image import submitted');
    } else {
      await screenshot(page, 'import-image-disabled', 'Submit still disabled');
      // Log what's in the form state for debugging
      const projectVal = await page.locator('button').filter({ hasText: /alpine|e2e|dev|staging/i }).first().textContent().catch(() => 'none');
      log('import-image', 'WARN', `Submit disabled — project selected: ${projectVal}`);
      await page.keyboard.press('Escape');
    }
  });

  // ── STEP 4: Create Egress Gateway ──
  test('04: Egress Gateway wizard', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/network/egress-gateways`);
    await waitIdle(page);
    await screenshot(page, 'egress-gateways-list', 'Egress Gateways page');

    const createBtn = page.getByRole('button', { name: /create/i });
    if (!await createBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      log('egress-gateway', 'WARN', 'No Create button on egress gateways page');
      return;
    }
    await createBtn.click();
    await page.waitForTimeout(800);
    await screenshot(page, 'egress-gateway-wizard-step1', 'Egress Gateway wizard step 1');

    // Fill name
    const nameField = page.getByLabel(/name/i).first();
    if (await nameField.isVisible({ timeout: 3000 }).catch(() => false)) {
      await nameField.fill('e2e-egw');
    }

    // Fill external IP / Interface if present
    const ipField = page.locator('input[placeholder*="192.168"], input[placeholder*="ip"], input[name*="ip"]').first();
    if (await ipField.isVisible({ timeout: 2000 }).catch(() => false)) {
      await ipField.fill('192.168.196.221');
      log('egress-gateway', 'OK', 'Filled external IP');
    }

    await screenshot(page, 'egress-gateway-wizard-filled', 'Egress Gateway wizard filled');

    // Try Next
    const nextBtn = page.getByRole('button', { name: /next/i });
    if (await nextBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await nextBtn.click();
      await page.waitForTimeout(500);
      await screenshot(page, 'egress-gateway-wizard-step2', 'EGW wizard step 2');
    }

    // Cancel or close (don't actually submit to avoid breaking cluster)
    const cancelBtn = page.getByRole('button', { name: /cancel|close/i });
    if (await cancelBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await cancelBtn.click();
    } else {
      await page.keyboard.press('Escape');
    }
    await page.waitForTimeout(300);

    log('egress-gateway', 'OK', 'Egress Gateway wizard opened and documented');
  });

  // ── STEP 5: Create VM Template ──
  test('05: Create VM Template', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/vms/templates`);
    await waitIdle(page);
    await screenshot(page, 'vm-templates-list', 'VM Templates page');

    const createBtn = page.getByRole('button', { name: /Create Template/i }).first();
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await createBtn.click();
    await page.waitForTimeout(600);
    await screenshot(page, 'template-modal-open', 'Create Template modal');

    // Template name
    const nameInput = page.locator('input[placeholder*="ubuntu"], input[name="name"], input[id*="name"]').first();
    if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await nameInput.fill('alpine-e2e');
    }

    // Display name
    const displayInput = page.getByLabel(/Display Name/i).first();
    if (await displayInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await displayInput.fill('Alpine E2E Template');
    }

    // Project/namespace selection
    const projectSelect = page.locator('[role="combobox"]').first();
    if (await projectSelect.isVisible({ timeout: 2000 }).catch(() => false)) {
      await projectSelect.click();
      await page.waitForTimeout(300);
      // Pick first available option
      const option = page.locator('[role="option"]').first();
      if (await option.isVisible({ timeout: 2000 }).catch(() => false)) {
        await option.click();
      }
    }

    await page.waitForTimeout(500);
    await screenshot(page, 'template-modal-filled', 'Template modal filled');

    // Golden image selection
    const imageSelect = page.locator('[role="combobox"]').nth(1);
    if (await imageSelect.isVisible({ timeout: 2000 }).catch(() => false)) {
      await imageSelect.click();
      await page.waitForTimeout(300);
      const imgOption = page.locator('[role="option"]').first();
      if (await imgOption.isVisible({ timeout: 2000 }).catch(() => false)) {
        await imgOption.click();
        log('template-create', 'OK', 'Selected golden image');
      } else {
        log('template-create', 'WARN', 'No golden image options available yet (import may not be complete)');
      }
    }

    await screenshot(page, 'template-modal-image-selected', 'Template modal with image selected');

    // Submit
    const submitBtn = page.getByRole('button', { name: /Create Template|Save/i }).last();
    if (await submitBtn.isEnabled({ timeout: 2000 }).catch(() => false)) {
      await submitBtn.click();
      await page.waitForTimeout(3000);
      await waitIdle(page);
      await screenshot(page, 'template-created', 'After template creation');
      log('template-create', 'OK', 'Template creation submitted');
    } else {
      log('template-create', 'WARN', 'Submit button disabled — golden image may not be imported yet');
      await screenshot(page, 'template-submit-disabled', 'Submit disabled state');
      const cancelBtn = page.getByRole('button', { name: /cancel|close/i });
      if (await cancelBtn.isVisible({ timeout: 2000 }).catch(() => false)) await cancelBtn.click();
      else await page.keyboard.press('Escape');
    }
  });

  // ── STEP 6: Create VM from Template ──
  test('06: Create VM from Template', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/vms`);
    await waitIdle(page);
    await screenshot(page, 'vms-list', 'VMs list page');

    const createBtn = page.getByRole('button', { name: /Create VM/i }).first();
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await createBtn.click();
    await page.waitForTimeout(600);
    await screenshot(page, 'vm-wizard-step1-template', 'VM wizard — template selection');

    // The wizard modal is open — work inside it
    const modal = page.locator('div.fixed.inset-0').last();

    // Step 1: Select Environment inside the wizard
    // CustomSelect with placeholder "Select an environment..."
    const envTrigger = modal.getByRole('button', { name: /Select an environment/i });
    if (await envTrigger.isVisible({ timeout: 3000 }).catch(() => false)) {
      await envTrigger.click();
      await page.waitForTimeout(300);
      // Pick first non-placeholder option
      const envOption = page.locator('div.absolute button').filter({ hasNotText: /Select an environment/i }).first();
      if (await envOption.isVisible({ timeout: 3000 }).catch(() => false)) {
        const envName = await envOption.textContent() || 'unknown';
        await envOption.click();
        await page.waitForTimeout(600);
        log('vm-create', 'OK', `Selected environment: ${envName.trim()}`);
      }
    }

    await screenshot(page, 'vm-wizard-project-selected', 'VM wizard — environment selected');

    // Wait for templates to load
    await page.waitForTimeout(1000);

    // Select template card — look inside the modal's grid
    const templateCard = modal.locator('div.grid > div').first();
    const anyTemplate = await templateCard.isVisible({ timeout: 3000 }).catch(() => false);
    if (anyTemplate) {
      await templateCard.click();
      await page.waitForTimeout(300);
      log('vm-create', 'OK', 'Template selected');
    } else {
      log('vm-create', 'WARN', 'No templates available — image import may not be complete yet');
    }

    // Next: Customize — only if enabled (requires template selection)
    let nextBtn = page.getByRole('button', { name: /next/i });
    const nextEnabled = await nextBtn.isVisible({ timeout: 3000 }).catch(() => false)
      && await nextBtn.isEnabled({ timeout: 1000 }).catch(() => false);
    if (nextEnabled) {
      await nextBtn.click();
      await page.waitForTimeout(500);
      await screenshot(page, 'vm-wizard-step2-customize', 'VM wizard — customize step');

      // Fill VM name
      const vmNameInput = page.locator('input[placeholder*="my-vm"], input[name="name"], input[placeholder*="vm"]').first();
      if (await vmNameInput.isVisible({ timeout: 2000 }).catch(() => false)) {
        await vmNameInput.fill('e2e-test-vm');
        log('vm-create', 'OK', 'VM name filled: e2e-test-vm');
      }

      await screenshot(page, 'vm-wizard-customize-filled', 'VM wizard customize filled');

      // Next: Network
      nextBtn = page.getByRole('button', { name: /next/i });
      if (await nextBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
        await nextBtn.click();
        await page.waitForTimeout(500);
        await screenshot(page, 'vm-wizard-step3-network', 'VM wizard — network step');

        // Next: Cloud Init
        nextBtn = page.getByRole('button', { name: /next/i });
        if (await nextBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
          await nextBtn.click();
          await page.waitForTimeout(500);
          await screenshot(page, 'vm-wizard-step4-cloudinit', 'VM wizard — cloud-init step');

          // Next: Review
          nextBtn = page.getByRole('button', { name: /next/i });
          if (await nextBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
            await nextBtn.click();
            await page.waitForTimeout(500);
            await screenshot(page, 'vm-wizard-step5-review', 'VM wizard — review step');
          }
        }
      }
    }

    // Create VM
    const createVMBtn = page.getByRole('button', { name: /Create VM|Launch/i });
    if (await createVMBtn.isVisible({ timeout: 3000 }).catch(() => false)
        && await createVMBtn.isEnabled({ timeout: 1000 }).catch(() => false)) {
      await createVMBtn.click();
      await page.waitForTimeout(5000);
      await waitIdle(page);
      await screenshot(page, 'vm-created', 'After VM creation');
      log('vm-create', 'OK', 'VM creation submitted');
    } else {
      log('vm-create', 'WARN', 'Create VM button not available — template/image may not be ready');
      const cancelBtn = page.getByRole('button', { name: /cancel|close/i });
      if (await cancelBtn.isVisible({ timeout: 2000 }).catch(() => false)) await cancelBtn.click();
      else await page.keyboard.press('Escape');
    }
  });

  // ── STEP 7: Verify VM Status ──
  test('07: Verify VM Running', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/vms`);
    await waitIdle(page);
    await screenshot(page, 'vms-after-create', 'VMs list after creation');

    const runningBadge = page.getByText(/running/i).first();
    const vmRow = page.getByText('e2e-test-vm').first();

    if (await vmRow.isVisible({ timeout: 5000 }).catch(() => false)) {
      log('vm-status', 'OK', 'VM e2e-test-vm is visible in list');
      await vmRow.click();
      await waitIdle(page);
      await screenshot(page, 'vm-detail', 'VM detail page');

      if (await runningBadge.isVisible({ timeout: 60000 }).catch(() => false)) {
        log('vm-status', 'OK', 'VM status: Running');
      } else {
        const status = await page.locator('[class*="status"], [class*="badge"]').first().textContent().catch(() => 'unknown');
        log('vm-status', 'WARN', `VM status: ${status} (may still be provisioning)`);
      }
    } else {
      log('vm-status', 'WARN', 'VM e2e-test-vm not found — creation may have been skipped');
    }
  });

  // ── STEP 8: Full page tour ──
  test('08: Full Page Tour', async ({ page }) => {
    await login(page);

    const pages = [
      { name: 'dashboard', path: '/dashboard' },
      { name: 'vms', path: '/vms' },
      { name: 'vm-templates', path: '/vms/templates' },
      { name: 'storage-images', path: '/storage/images' },
      { name: 'storage-classes', path: '/storage/classes' },
      { name: 'network-vpcs', path: '/network/vpcs' },
      { name: 'network-subnets', path: '/network/subnets' },
      { name: 'network-system', path: '/network/system' },
      { name: 'egress-gateways', path: '/network/egress-gateways' },
      { name: 'security-groups', path: '/network/security-groups' },
      { name: 'folders', path: '/folders' },
      { name: 'tenants', path: '/tenants' },
      { name: 'users', path: '/users' },
      { name: 'groups', path: '/users/groups' },
      { name: 'profile', path: '/profile' },
      { name: 'cli-access', path: '/cli-access' },
      { name: 'cluster', path: '/cluster' },
    ];

    for (const p of pages) {
      try {
        await page.goto(`${BASE}${p.path}`);
        await waitIdle(page);
        const path = await screenshot(page, `tour-${p.name}`, `Page: ${p.name}`);

        // Basic checks
        const is404 = await page.getByText(/404|not found|page not found/i).isVisible({ timeout: 1000 }).catch(() => false);
        const hasError = await page.getByText(/error|failed/i).first().isVisible({ timeout: 500 }).catch(() => false);

        if (is404) {
          log(`tour-${p.name}`, 'ERROR', `404 on ${p.path}`);
        } else if (hasError) {
          log(`tour-${p.name}`, 'WARN', `Error message on ${p.path}`);
        } else {
          log(`tour-${p.name}`, 'OK', `${p.path} loaded fine`);
        }
      } catch (e) {
        log(`tour-${p.name}`, 'ERROR', `Exception: ${e}`);
      }
    }
  });

  // ── STEP 9: Export findings ──
  test('09: Export findings', async ({ page }) => {
    // Just visit dashboard so page is valid
    await login(page);
    await page.goto(`${BASE}/dashboard`);

    console.log('\n\n======== FINDINGS SUMMARY ========');
    for (const f of findings) {
      console.log(`[${f.status}] ${f.step}: ${f.note}`);
    }
    console.log('==================================\n');

    const okCount = findings.filter(f => f.status === 'OK').length;
    const warnCount = findings.filter(f => f.status === 'WARN').length;
    const errCount = findings.filter(f => f.status === 'ERROR').length;

    log('summary', 'OK', `Total: ${findings.length} | OK: ${okCount} | WARN: ${warnCount} | ERROR: ${errCount}`);
    expect(errCount).toBeLessThanOrEqual(3); // allow some non-critical errors
  });
});
