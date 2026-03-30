import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:3333';
const SCREENSHOT_DIR = '/screenshots';
let screenshotCounter = 0;

function sc(name: string): string {
  screenshotCounter++;
  const num = String(screenshotCounter).padStart(2, '0');
  return `${SCREENSHOT_DIR}/audit-${num}-${name}.png`;
}

// =================== LOGIN ===================
async function login(page: Page) {
  await page.goto(`${BASE}/login`);
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(1000);

  const ssoBtn = page.getByRole('button', { name: /Sign in with SSO/i });
  await expect(ssoBtn).toBeVisible({ timeout: 15000 });
  await ssoBtn.click();

  await page.waitForURL(url => !url.href.includes('localhost:3333'), { timeout: 15000 });
  await page.waitForLoadState('domcontentloaded').catch(() => {});

  const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in/i });
  if (await connectorLink.isVisible({ timeout: 3000 }).catch(() => false)) {
    await connectorLink.click();
  }

  await page.waitForTimeout(500);
  await page.getByRole('textbox', { name: /username/i }).fill('admin');
  await page.getByRole('textbox', { name: /password/i }).fill('admin_password');
  await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

  await page.waitForURL('**/dashboard', { timeout: 30000 });
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  await page.waitForTimeout(1000);
}

// =================== HELPERS ===================
const findings: string[] = [];

function logFinding(id: string, severity: string, page: string, description: string, expected: string, actual: string, screenshot: string) {
  findings.push(JSON.stringify({ id, severity, page, description, expected, actual, screenshot }));
  console.log(`[${id}] [${severity}] ${page}: ${description}`);
}

async function safeClick(page: Page, locator: ReturnType<Page['locator']>, timeout = 5000): Promise<boolean> {
  try {
    await locator.waitFor({ state: 'visible', timeout });
    await locator.click();
    return true;
  } catch {
    return false;
  }
}

async function safeScreenshot(page: Page, name: string): Promise<string> {
  const path = sc(name);
  try {
    await page.screenshot({ path, fullPage: true });
  } catch (e) {
    console.log(`Screenshot failed: ${name}: ${e}`);
  }
  return path;
}

// =================== PHASE 1: NAVIGATE ALL PAGES ===================

test.describe.serial('Full UI Audit', () => {

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // ---------- PHASE 1: Navigation ----------

  test('P1-01: Dashboard', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    const shot = await safeScreenshot(page, 'dashboard');

    // Check KPI cards render
    const cards = page.locator('[class*="card"], [class*="stat"], [class*="kpi"], [class*="metric"]');
    const cardCount = await cards.count();
    console.log(`Dashboard cards found: ${cardCount}`);

    // Check for "loading" text still visible
    const loadingText = page.locator('text=loading');
    const loadingCount = await loadingText.count();
    if (loadingCount > 0) {
      logFinding('BUG-DASH-01', 'P1', 'Dashboard', 'Metrics still showing "loading"', 'All metrics loaded', `${loadingCount} elements still loading`, shot);
    }

    // Check for error text
    const errorText = page.locator('text=/error|Error|Cannot access|failed/i');
    const errorCount = await errorText.count();
    if (errorCount > 0) {
      const texts = [];
      for (let i = 0; i < Math.min(errorCount, 3); i++) {
        texts.push(await errorText.nth(i).textContent());
      }
      logFinding('BUG-DASH-02', 'P0', 'Dashboard', 'Error visible on dashboard', 'No errors', texts.join('; '), shot);
    }

    // Check page title is h1
    const h1 = page.locator('h1');
    const h1Count = await h1.count();
    if (h1Count === 0) {
      logFinding('BUG-DASH-03', 'P2', 'Dashboard', 'No h1 title on dashboard', 'Page should have h1 title', 'No h1 found', shot);
    }
  });

  test('P1-02: Virtual Machines', async ({ page }) => {
    await page.goto(`${BASE}/vms`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    const shot = await safeScreenshot(page, 'vms');

    // Check for "Cannot access" error
    const errorText = page.locator('text=/Cannot access|error|Error|undefined/i');
    const errorCount = await errorText.count();
    if (errorCount > 0) {
      const texts = [];
      for (let i = 0; i < Math.min(errorCount, 3); i++) {
        texts.push(await errorText.nth(i).textContent());
      }
      logFinding('BUG-VM-01', 'P0', 'Virtual Machines', 'Error on VMs page', 'VMs list renders correctly', texts.join('; '), shot);
    }

    // Check Create button exists
    const createBtn = page.getByRole('button', { name: /create/i });
    if (!(await createBtn.isVisible({ timeout: 3000 }).catch(() => false))) {
      logFinding('BUG-VM-02', 'P1', 'Virtual Machines', 'No Create button on VMs page', 'Create VM button visible', 'Button not found', shot);
    }
  });

  test('P1-03: VM Templates', async ({ page }) => {
    await page.goto(`${BASE}/vms/templates`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'vm-templates');
  });

  test('P1-04: Storage > Images', async ({ page }) => {
    await page.goto(`${BASE}/storage/images`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'storage-images');
  });

  test('P1-05: Storage > Classes', async ({ page }) => {
    await page.goto(`${BASE}/storage/classes`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'storage-classes');
  });

  test('P1-06: Network - VPCs tab', async ({ page }) => {
    await page.goto(`${BASE}/network?tab=vpcs`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    const shot = await safeScreenshot(page, 'network-vpcs-tab');

    // Check if VPCs show their subnets
    const expandButtons = page.locator('[aria-label*="expand"], [class*="expand"], button:has(svg[class*="chevron"])');
    const expandCount = await expandButtons.count();
    console.log(`VPCs tab: expandable rows found: ${expandCount}`);
  });

  test('P1-07: Network - Subnets tab', async ({ page }) => {
    await page.goto(`${BASE}/network?tab=subnets`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    const shot = await safeScreenshot(page, 'network-subnets-tab');

    // Compare UI style with VPCs tab
    const table = page.locator('table, [role="table"], [class*="DataTable"]');
    const tableCount = await table.count();
    console.log(`Subnets tab: tables found: ${tableCount}`);
  });

  test('P1-08: Network - System tab', async ({ page }) => {
    await page.goto(`${BASE}/network?tab=system`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'network-system-tab');
  });

  test('P1-09: Network > Egress Gateways', async ({ page }) => {
    await page.goto(`${BASE}/network/egress-gateways`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'egress-gateways');
  });

  test('P1-10: Network > Security Groups', async ({ page }) => {
    await page.goto(`${BASE}/network/security-groups`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'security-groups');
  });

  test('P1-11: Cluster', async ({ page }) => {
    await page.goto(`${BASE}/cluster`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'cluster');
  });

  test('P1-12: Folders', async ({ page }) => {
    await page.goto(`${BASE}/folders`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    const shot = await safeScreenshot(page, 'folders');

    // Check "+" button in sidebar
    const plusBtn = page.locator('a[href="/folders/new"], a[title="Create Folder"]');
    const plusVisible = await plusBtn.isVisible({ timeout: 2000 }).catch(() => false);
    console.log(`Folders sidebar + button visible: ${plusVisible}`);

    if (plusVisible) {
      await plusBtn.click();
      await page.waitForTimeout(1500);
      const currentUrl = page.url();
      const shotAfterPlus = await safeScreenshot(page, 'folders-plus-click');
      console.log(`After sidebar + click, URL: ${currentUrl}`);

      // Check if /folders/new route exists and renders something
      if (currentUrl.includes('/folders/new')) {
        const content = await page.locator('main, [class*="content"]').textContent().catch(() => '');
        if (!content || content.trim().length < 10) {
          logFinding('BUG-FOLD-01', 'P1', 'Folders', 'Sidebar + navigates to /folders/new but page is empty', 'Should show create folder form or modal', `URL: ${currentUrl}, content length: ${content?.length ?? 0}`, shotAfterPlus);
        }
      }
    } else {
      logFinding('BUG-FOLD-02', 'P2', 'Folders', 'Sidebar + button not found', 'Plus icon should be in sidebar for folders', 'Not visible', shot);
    }
  });

  test('P1-13: Tenants', async ({ page }) => {
    await page.goto(`${BASE}/tenants`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'tenants');
  });

  test('P1-14: Users', async ({ page }) => {
    await page.goto(`${BASE}/users`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'users');
  });

  test('P1-15: Groups', async ({ page }) => {
    await page.goto(`${BASE}/users/groups`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'groups');
  });

  test('P1-16: Profile', async ({ page }) => {
    await page.goto(`${BASE}/profile`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'profile');
  });

  test('P1-17: CLI Access', async ({ page }) => {
    await page.goto(`${BASE}/cli-access`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'cli-access');
  });

  // ---------- PHASE 2: Create Resources ----------

  test('P2-01: Create VPC wizard', async ({ page }) => {
    await page.goto(`${BASE}/network?tab=vpcs`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // Click Create dropdown
    const createBtn = page.getByRole('button', { name: /create/i }).first();
    await safeClick(page, createBtn);
    await page.waitForTimeout(500);
    await safeScreenshot(page, 'vpc-create-dropdown');

    // Click "Create VPC" in dropdown
    const vpcOption = page.locator('button:text("Create VPC")');
    if (await safeClick(page, vpcOption)) {
      await page.waitForTimeout(1500);
      await page.waitForLoadState('networkidle');
      await safeScreenshot(page, 'vpc-wizard-step1');

      // Step 1: Fill name and CIDR
      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i], input[id*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-vpc');
        await page.waitForTimeout(300);
      } else {
        // Try label-based
        const nameByLabel = page.getByLabel(/name/i).first();
        if (await nameByLabel.isVisible({ timeout: 2000 }).catch(() => false)) {
          await nameByLabel.fill('audit-test-vpc');
        } else {
          logFinding('BUG-VPC-01', 'P1', 'Create VPC', 'Cannot find name input', 'Name input should be visible', 'Not found', '');
        }
      }

      const cidrInput = page.locator('input[name="cidr"], input[placeholder*="cidr" i], input[placeholder*="10." i]').first();
      if (await cidrInput.isVisible({ timeout: 2000 }).catch(() => false)) {
        await cidrInput.fill('10.100.0.0/16');
      } else {
        const cidrByLabel = page.getByLabel(/cidr/i).first();
        if (await cidrByLabel.isVisible({ timeout: 2000 }).catch(() => false)) {
          await cidrByLabel.fill('10.100.0.0/16');
        }
      }

      await safeScreenshot(page, 'vpc-wizard-step1-filled');

      // Click Next
      const nextBtn = page.getByRole('button', { name: /next/i });
      if (await safeClick(page, nextBtn)) {
        await page.waitForTimeout(1500);
        await safeScreenshot(page, 'vpc-wizard-step2');

        // Step 2: Add subnet
        const addSubnetBtn = page.getByRole('button', { name: /add subnet/i });
        if (await safeClick(page, addSubnetBtn, 3000)) {
          await page.waitForTimeout(500);

          const subNameInput = page.locator('input[name*="subnet"], input[placeholder*="subnet" i]').first();
          if (await subNameInput.isVisible({ timeout: 2000 }).catch(() => false)) {
            await subNameInput.fill('audit-test-sub');
          }

          const subCidrInput = page.locator('input[name*="cidr"], input[placeholder*="10." i]').last();
          if (await subCidrInput.isVisible({ timeout: 2000 }).catch(() => false)) {
            await subCidrInput.fill('10.100.1.0/24');
          }
        }

        await safeScreenshot(page, 'vpc-wizard-step2-filled');

        // Click Next to review
        const nextBtn2 = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn2)) {
          await page.waitForTimeout(1000);
          await safeScreenshot(page, 'vpc-wizard-step3-review');

          // Submit
          const submitBtn = page.getByRole('button', { name: /create|submit/i }).last();
          if (await safeClick(page, submitBtn)) {
            await page.waitForTimeout(3000);
            await safeScreenshot(page, 'vpc-wizard-result');

            // Check if VPC appeared in list
            await page.goto(`${BASE}/network?tab=vpcs`);
            await page.waitForLoadState('networkidle');
            await page.waitForTimeout(2000);
            await safeScreenshot(page, 'vpc-list-after-create');

            const vpcInList = page.locator('text=audit-test-vpc');
            if (!(await vpcInList.isVisible({ timeout: 3000 }).catch(() => false))) {
              logFinding('BUG-VPC-02', 'P0', 'Create VPC', 'Created VPC not visible in list', 'VPC should appear in list', 'Not found', '');
            }

            // Check subnets tab
            await page.goto(`${BASE}/network?tab=subnets`);
            await page.waitForLoadState('networkidle');
            await page.waitForTimeout(2000);
            await safeScreenshot(page, 'subnets-after-vpc-create');

            const subInList = page.locator('text=audit-test-sub');
            if (!(await subInList.isVisible({ timeout: 3000 }).catch(() => false))) {
              logFinding('BUG-VPC-03', 'P1', 'Create VPC', 'Created subnet not visible in Subnets tab', 'Subnet should appear in Subnets tab', 'Not found', '');
            }
          }
        }
      }
    } else {
      logFinding('BUG-VPC-04', 'P0', 'Create VPC', 'Create VPC option not found in dropdown', 'Dropdown should show "Create VPC"', 'Not found', '');
    }
  });

  test('P2-02: Create Security Group wizard', async ({ page }) => {
    await page.goto(`${BASE}/network/security-groups`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const createBtn = page.getByRole('button', { name: /create/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'sg-wizard-step1');

      // Step 1: Name + Description
      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-sg');
      }

      const descInput = page.locator('textarea, input[name="description"], input[placeholder*="description" i]').first();
      if (await descInput.isVisible({ timeout: 2000 }).catch(() => false)) {
        await descInput.fill('Test Security Group');
      }

      await safeScreenshot(page, 'sg-wizard-step1-filled');

      // Click Next
      const nextBtn = page.getByRole('button', { name: /next/i });
      if (await safeClick(page, nextBtn)) {
        await page.waitForTimeout(1500);
        await safeScreenshot(page, 'sg-wizard-step2');

        // Step 2: Add rules using templates
        // Click SSH template
        const sshBtn = page.locator('button:text("SSH"), button:has-text("SSH")').first();
        if (await safeClick(page, sshBtn, 3000)) {
          await page.waitForTimeout(500);
          await safeScreenshot(page, 'sg-wizard-ssh-added');

          // Check SSH priority
          const priorityInputs = page.locator('input[name*="priority"], input[type="number"]');
          const priorityCount = await priorityInputs.count();
          console.log(`After SSH template, priority inputs: ${priorityCount}`);

          if (priorityCount > 0) {
            const firstPriority = await priorityInputs.first().inputValue().catch(() => '');
            console.log(`First rule priority: ${firstPriority}`);
          }
        }

        // Click HTTP template
        const httpBtn = page.locator('button:text("HTTP"), button:has-text("HTTP")').first();
        if (await safeClick(page, httpBtn, 3000)) {
          await page.waitForTimeout(500);
          await safeScreenshot(page, 'sg-wizard-http-added');

          // Check all priorities - known bug: all rules get priority 100
          const priorityInputs = page.locator('input[name*="priority"], input[type="number"]');
          const priorities: string[] = [];
          const count = await priorityInputs.count();
          for (let i = 0; i < count; i++) {
            const val = await priorityInputs.nth(i).inputValue().catch(() => '');
            if (val) priorities.push(val);
          }
          console.log(`All priorities after HTTP: ${priorities.join(', ')}`);

          // Check if all priorities are 100 (known bug)
          if (priorities.length >= 2 && priorities.every(p => p === '100')) {
            logFinding('BUG-SG-01', 'P1', 'Create Security Group', 'All rule priorities are 100', 'Priorities should auto-increment: 100, 200, 300...', `All priorities: ${priorities.join(', ')}`, '');
          }
        }

        // Click HTTPS template
        const httpsBtn = page.locator('button:text("HTTPS"), button:has-text("HTTPS")').first();
        if (await safeClick(page, httpsBtn, 3000)) {
          await page.waitForTimeout(500);
          await safeScreenshot(page, 'sg-wizard-https-added');

          const priorityInputs = page.locator('input[name*="priority"], input[type="number"]');
          const priorities: string[] = [];
          const count = await priorityInputs.count();
          for (let i = 0; i < count; i++) {
            const val = await priorityInputs.nth(i).inputValue().catch(() => '');
            if (val) priorities.push(val);
          }
          console.log(`All priorities after HTTPS: ${priorities.join(', ')}`);
        }

        // Try to add manual rule
        const addRuleBtn = page.locator('button:text("Add Rule"), button:has-text("Add Rule"), button:has-text("Add rule")').first();
        if (await safeClick(page, addRuleBtn, 3000)) {
          await page.waitForTimeout(500);
          await safeScreenshot(page, 'sg-wizard-manual-rule');
        }

        await safeScreenshot(page, 'sg-wizard-step2-all-rules');

        // Click Next to review
        const nextBtn2 = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn2)) {
          await page.waitForTimeout(1000);
          await safeScreenshot(page, 'sg-wizard-step3-review');

          // Submit
          const submitBtn = page.getByRole('button', { name: /create|submit/i }).last();
          if (await safeClick(page, submitBtn)) {
            await page.waitForTimeout(3000);
            await safeScreenshot(page, 'sg-wizard-result');

            // Verify in list
            await page.goto(`${BASE}/network/security-groups`);
            await page.waitForLoadState('networkidle');
            await page.waitForTimeout(2000);
            await safeScreenshot(page, 'sg-list-after-create');

            // Check expandable row with rules
            const sgRow = page.locator('text=audit-test-sg');
            if (await sgRow.isVisible({ timeout: 3000 }).catch(() => false)) {
              // Try to expand row
              const expandBtn = sgRow.locator('..').locator('button').first();
              if (await safeClick(page, expandBtn)) {
                await page.waitForTimeout(1000);
                await safeScreenshot(page, 'sg-expanded-row');
              }
            }
          }
        }
      }
    }
  });

  test('P2-03: Create Folder', async ({ page }) => {
    await page.goto(`${BASE}/folders`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const createBtn = page.getByRole('button', { name: /create/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'folder-create-modal');

      // Fill folder name
      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-folder');
      }

      const descInput = page.locator('textarea, input[name="description"], input[placeholder*="description" i]').first();
      if (await descInput.isVisible({ timeout: 2000 }).catch(() => false)) {
        await descInput.fill('Test folder from audit');
      }

      await safeScreenshot(page, 'folder-create-filled');

      // Submit
      const submitBtn = page.getByRole('button', { name: /create|submit|save/i }).last();
      if (await safeClick(page, submitBtn)) {
        await page.waitForTimeout(2000);
        await safeScreenshot(page, 'folder-create-result');
      }
    } else {
      logFinding('BUG-FOLD-03', 'P1', 'Folders', 'Create Folder button not found', 'Create button should be visible', 'Not found', '');
    }
  });

  test('P2-04: Create Tenant wizard', async ({ page }) => {
    await page.goto(`${BASE}/tenants`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const createBtn = page.getByRole('button', { name: /create/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'tenant-wizard-step1');

      // Fill name
      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-tenant');
      }

      const descInput = page.locator('textarea, input[name="description"], input[placeholder*="description" i]').first();
      if (await descInput.isVisible({ timeout: 2000 }).catch(() => false)) {
        await descInput.fill('Test tenant');
      }

      await safeScreenshot(page, 'tenant-wizard-step1-filled');

      // Try Next
      const nextBtn = page.getByRole('button', { name: /next/i });
      if (await safeClick(page, nextBtn)) {
        await page.waitForTimeout(1000);
        await safeScreenshot(page, 'tenant-wizard-step2');

        // Try Next again
        const nextBtn2 = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn2)) {
          await page.waitForTimeout(1000);
          await safeScreenshot(page, 'tenant-wizard-step3');
        }

        // Try to submit
        const submitBtn = page.getByRole('button', { name: /create|submit/i }).last();
        if (await safeClick(page, submitBtn)) {
          await page.waitForTimeout(2000);
          await safeScreenshot(page, 'tenant-wizard-result');
        }
      }
    } else {
      logFinding('BUG-TENANT-01', 'P1', 'Tenants', 'Create Tenant button not found', 'Create button should be visible', 'Not found', '');
    }
  });

  test('P2-05: Create User', async ({ page }) => {
    await page.goto(`${BASE}/users`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const createBtn = page.getByRole('button', { name: /create|add|new|invite/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'user-create-modal');

      // Fill form
      const nameInput = page.locator('input[name="name"], input[name="username"], input[placeholder*="name" i], input[placeholder*="user" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-user');
      }

      const emailInput = page.locator('input[name="email"], input[type="email"], input[placeholder*="email" i]').first();
      if (await emailInput.isVisible({ timeout: 2000 }).catch(() => false)) {
        await emailInput.fill('audit@test.com');
      }

      await safeScreenshot(page, 'user-create-filled');

      const submitBtn = page.getByRole('button', { name: /create|submit|save|add/i }).last();
      if (await safeClick(page, submitBtn)) {
        await page.waitForTimeout(2000);
        await safeScreenshot(page, 'user-create-result');
      }
    } else {
      logFinding('BUG-USER-01', 'P1', 'Users', 'Create User button not found', 'Create button should be visible', 'Not found', '');
    }
  });

  test('P2-06: Create Group', async ({ page }) => {
    await page.goto(`${BASE}/users/groups`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const createBtn = page.getByRole('button', { name: /create|add|new/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'group-create-modal');

      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-group');
      }

      await safeScreenshot(page, 'group-create-filled');

      const submitBtn = page.getByRole('button', { name: /create|submit|save/i }).last();
      if (await safeClick(page, submitBtn)) {
        await page.waitForTimeout(2000);
        await safeScreenshot(page, 'group-create-result');
      }
    } else {
      logFinding('BUG-GROUP-01', 'P1', 'Groups', 'Create Group button not found', 'Create button should be visible', 'Not found', '');
    }
  });

  test('P2-07: Create Egress Gateway wizard', async ({ page }) => {
    await page.goto(`${BASE}/network/egress-gateways`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const createBtn = page.getByRole('button', { name: /create/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'egw-wizard-step1');

      // Fill name
      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-egw');
      }

      await safeScreenshot(page, 'egw-wizard-step1-filled');

      // Try Next
      const nextBtn = page.getByRole('button', { name: /next/i });
      if (await safeClick(page, nextBtn)) {
        await page.waitForTimeout(1000);
        await safeScreenshot(page, 'egw-wizard-step2');

        const nextBtn2 = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn2)) {
          await page.waitForTimeout(1000);
          await safeScreenshot(page, 'egw-wizard-step3');
        }
      }

      // Try Submit
      const submitBtn = page.getByRole('button', { name: /create|submit/i }).last();
      if (await safeClick(page, submitBtn)) {
        await page.waitForTimeout(2000);
        await safeScreenshot(page, 'egw-wizard-result');
      }
    } else {
      logFinding('BUG-EGW-01', 'P1', 'Egress Gateways', 'Create button not found', 'Create button should be visible', 'Not found', '');
    }
  });

  test('P2-08: Create Subnet', async ({ page }) => {
    await page.goto(`${BASE}/network?tab=subnets`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // The Create dropdown is on the Networks page, need to click "Create Subnet"
    const createBtn = page.getByRole('button', { name: /create/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(500);

      const subnetOption = page.locator('button:text("Create Subnet")');
      if (await safeClick(page, subnetOption)) {
        await page.waitForTimeout(1500);
        await page.waitForLoadState('networkidle');
        await safeScreenshot(page, 'subnet-create-form');

        // Fill form
        const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
        if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
          await nameInput.fill('audit-test-subnet');
        }

        await safeScreenshot(page, 'subnet-create-filled');
      } else {
        logFinding('BUG-SUB-01', 'P1', 'Create Subnet', 'Create Subnet option not found in dropdown', 'Should show "Create Subnet" option', 'Not found', '');
      }
    }
  });

  test('P2-09: Create VM (if page works)', async ({ page }) => {
    await page.goto(`${BASE}/vms`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // Check if page has errors
    const errorText = page.locator('text=/Cannot access|error|Error/i');
    const hasError = await errorText.isVisible({ timeout: 2000 }).catch(() => false);

    if (hasError) {
      const shotErr = await safeScreenshot(page, 'vm-create-page-error');
      logFinding('BUG-VMCR-01', 'P0', 'Create VM', 'VMs page has error, cannot access Create VM', 'Page should render', 'Error visible', shotErr);
      return;
    }

    const createBtn = page.getByRole('button', { name: /create/i }).first();
    if (await safeClick(page, createBtn)) {
      await page.waitForTimeout(2000);
      await safeScreenshot(page, 'vm-wizard-step1');

      // Fill VM name
      const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('audit-test-vm');
      }

      await safeScreenshot(page, 'vm-wizard-step1-filled');

      // Check SSH key picker
      const sshSection = page.locator('text=/SSH|ssh key/i');
      const sshVisible = await sshSection.isVisible({ timeout: 2000 }).catch(() => false);
      console.log(`VM wizard SSH key section visible: ${sshVisible}`);

      // Try Next
      const nextBtn = page.getByRole('button', { name: /next/i });
      if (await safeClick(page, nextBtn)) {
        await page.waitForTimeout(1000);
        await safeScreenshot(page, 'vm-wizard-step2');

        const nextBtn2 = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn2)) {
          await page.waitForTimeout(1000);
          await safeScreenshot(page, 'vm-wizard-step3');
        }
      }
    } else {
      logFinding('BUG-VMCR-02', 'P1', 'Create VM', 'Create VM button not found', 'Button should be visible', 'Not found', '');
    }
  });

  // ---------- PHASE 3: Consistency Checks ----------

  test('P3-01: Button naming consistency', async ({ page }) => {
    const pagesToCheck = [
      { url: '/vms', name: 'VMs' },
      { url: '/network?tab=vpcs', name: 'Network' },
      { url: '/network/egress-gateways', name: 'Egress Gateways' },
      { url: '/network/security-groups', name: 'Security Groups' },
      { url: '/folders', name: 'Folders' },
      { url: '/tenants', name: 'Tenants' },
      { url: '/users', name: 'Users' },
      { url: '/users/groups', name: 'Groups' },
    ];

    const issues: string[] = [];

    for (const p of pagesToCheck) {
      await page.goto(`${BASE}${p.url}`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1000);

      // Check for "New X" buttons (should be "Create X")
      const newBtn = page.locator('button:has-text("New ")');
      const newCount = await newBtn.count();
      if (newCount > 0) {
        for (let i = 0; i < newCount; i++) {
          const text = await newBtn.nth(i).textContent();
          issues.push(`${p.name}: Button says "${text}" instead of "Create X"`);
        }
      }

      // Check page title is h1
      const h1 = page.locator('h1');
      const h1Count = await h1.count();
      if (h1Count === 0) {
        issues.push(`${p.name}: Missing h1 page title`);
      }

      // Check for h2 used as page title (should be h1)
      const h2Title = page.locator('h2').first();
      if (h1Count === 0 && await h2Title.isVisible({ timeout: 500 }).catch(() => false)) {
        const h2text = await h2Title.textContent().catch(() => '');
        issues.push(`${p.name}: Uses h2 "${h2text}" instead of h1 for page title`);
      }
    }

    if (issues.length > 0) {
      const shot = await safeScreenshot(page, 'consistency-buttons');
      logFinding('BUG-CON-01', 'P2', 'All Pages', 'Button naming or heading inconsistencies', 'All create buttons should say "Create X", all page titles should be h1', issues.join('; '), shot);
    }
    console.log(`Consistency issues: ${issues.length}`);
    issues.forEach(i => console.log(`  - ${i}`));
  });

  test('P3-02: VPCs tab vs Subnets tab data relationship', async ({ page }) => {
    // Go to VPCs tab
    await page.goto(`${BASE}/network?tab=vpcs`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    const shotVpc = await safeScreenshot(page, 'consistency-vpcs-tab');

    // Check if VPCs show subnet info
    const subnetMention = page.locator('text=/subnet/i');
    const subnetVisibleInVpc = await subnetMention.isVisible({ timeout: 2000 }).catch(() => false);
    console.log(`VPCs tab shows subnet info: ${subnetVisibleInVpc}`);

    if (!subnetVisibleInVpc) {
      logFinding('BUG-CON-02', 'P1', 'Network > VPCs tab', 'VPCs tab does not show related subnets', 'Each VPC should show its subnets (expandable row or column)', 'No subnet info visible in VPCs tab', shotVpc);
    }

    // Go to Subnets tab and check if VPC info is shown
    await page.goto(`${BASE}/network?tab=subnets`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    const shotSub = await safeScreenshot(page, 'consistency-subnets-tab');

    // Check UI style consistency
    const vpcTabTable = await page.goto(`${BASE}/network?tab=vpcs`).then(() => page.waitForTimeout(1000)).then(() =>
      page.locator('table, [role="table"]').count()
    );
    await page.goto(`${BASE}/network?tab=subnets`);
    await page.waitForTimeout(1000);
    const subTabTable = await page.locator('table, [role="table"]').count();

    console.log(`VPCs tab tables: ${vpcTabTable}, Subnets tab tables: ${subTabTable}`);
  });

  test('P3-03: DataTable consistency across pages', async ({ page }) => {
    const pagesToCheck = [
      { url: '/vms', name: 'VMs' },
      { url: '/network?tab=vpcs', name: 'VPCs' },
      { url: '/network?tab=subnets', name: 'Subnets' },
      { url: '/network/security-groups', name: 'Security Groups' },
      { url: '/network/egress-gateways', name: 'Egress Gateways' },
      { url: '/folders', name: 'Folders' },
      { url: '/tenants', name: 'Tenants' },
      { url: '/users', name: 'Users' },
      { url: '/users/groups', name: 'Groups' },
    ];

    const tableStyles: { page: string; hasDataTable: boolean; hasPlainTable: boolean; hasCards: boolean }[] = [];

    for (const p of pagesToCheck) {
      await page.goto(`${BASE}${p.url}`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1000);

      const dataTable = await page.locator('[class*="DataTable"], [data-testid*="table"]').count();
      const plainTable = await page.locator('table').count();
      const cards = await page.locator('[class*="card-grid"], [class*="CardGrid"]').count();

      tableStyles.push({ page: p.name, hasDataTable: dataTable > 0, hasPlainTable: plainTable > 0, hasCards: cards > 0 });
    }

    console.log('Table styles across pages:');
    tableStyles.forEach(s => console.log(`  ${s.page}: DataTable=${s.hasDataTable}, table=${s.hasPlainTable}, cards=${s.hasCards}`));

    // Check inconsistency
    const stylesUsed = new Set(tableStyles.map(s => s.hasDataTable ? 'DataTable' : s.hasPlainTable ? 'table' : s.hasCards ? 'cards' : 'none'));
    if (stylesUsed.size > 1) {
      logFinding('BUG-CON-03', 'P2', 'All list pages', 'Inconsistent table components across pages', 'All list pages should use the same DataTable component', `Styles used: ${[...stylesUsed].join(', ')}`, '');
    }
  });

  test('P3-04: Breadcrumbs on detail pages', async ({ page }) => {
    const detailPages = [
      { url: '/network/vpcs/ovn-cluster', name: 'VPC Detail' },
      { url: '/network/security-groups/test', name: 'SG Detail' },
      { url: '/tenants/test', name: 'Tenant Detail' },
      { url: '/folders/test', name: 'Folder Detail' },
    ];

    for (const p of detailPages) {
      await page.goto(`${BASE}${p.url}`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1000);

      const breadcrumb = page.locator('[class*="breadcrumb" i], nav[aria-label="breadcrumb"], [class*="Breadcrumb"]');
      const hasBreadcrumb = await breadcrumb.isVisible({ timeout: 2000 }).catch(() => false);

      if (!hasBreadcrumb) {
        // Check for back link at least
        const backLink = page.locator('a:has-text("Back"), a:has-text("back"), button:has-text("Back")');
        const hasBack = await backLink.isVisible({ timeout: 1000 }).catch(() => false);
        if (!hasBack) {
          console.log(`${p.name}: No breadcrumb or back navigation`);
        }
      }
    }

    await safeScreenshot(page, 'consistency-breadcrumbs');
  });

  test('P3-05: Sidebar folders + button test', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // Expand Folders in sidebar
    const foldersBtn = page.locator('button:has-text("Folders")').first();
    if (await safeClick(page, foldersBtn)) {
      await page.waitForTimeout(500);
    }

    // Find the + button
    const plusBtn = page.locator('a[href="/folders/new"], a[title="Create Folder"]');
    const plusVisible = await plusBtn.isVisible({ timeout: 2000 }).catch(() => false);

    if (plusVisible) {
      await plusBtn.click();
      await page.waitForTimeout(2000);
      const url = page.url();
      const shot = await safeScreenshot(page, 'sidebar-folders-plus');

      console.log(`Sidebar + button navigates to: ${url}`);

      // Check if page renders anything useful
      const pageContent = await page.locator('main').textContent().catch(() => '');
      const has404 = await page.locator('text=/not found|404/i').isVisible({ timeout: 1000 }).catch(() => false);

      if (has404) {
        logFinding('BUG-FOLD-04', 'P1', 'Sidebar', 'Folders + button leads to 404 page', 'Should open create folder form', `URL: ${url}, shows 404`, shot);
      } else if (!pageContent || pageContent.trim().length < 20) {
        logFinding('BUG-FOLD-05', 'P1', 'Sidebar', 'Folders + button leads to empty page', 'Should open create folder form or modal', `URL: ${url}, content empty`, shot);
      }
    } else {
      logFinding('BUG-FOLD-06', 'P2', 'Sidebar', 'Folders + button not visible in sidebar', 'Should have + icon next to Folders', 'Not visible', '');
    }
  });

  // ---------- Summary ----------
  test('SUMMARY: Print all findings', async ({ page }) => {
    console.log('\n\n========== FULL AUDIT SUMMARY ==========');
    console.log(`Total findings: ${findings.length}`);
    findings.forEach(f => {
      const parsed = JSON.parse(f);
      console.log(`[${parsed.severity}] ${parsed.id}: ${parsed.page} - ${parsed.description}`);
    });
    console.log('==========================================\n');
  });
});
