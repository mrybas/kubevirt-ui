import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:3333';
const SCREENSHOT_DIR = '/screenshots';

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
const results: { id: string; status: string; description: string; screenshot: string; comment: string }[] = [];

function logResult(id: string, status: string, description: string, screenshot: string, comment: string = '') {
  results.push({ id, status, description, screenshot, comment });
  console.log(`[${status}] ${id}: ${description}${comment ? ' — ' + comment : ''}`);
}

async function safeScreenshot(page: Page, name: string): Promise<string> {
  const path = `${SCREENSHOT_DIR}/${name}.png`;
  try {
    await page.screenshot({ path, fullPage: true });
  } catch (e) {
    console.log(`Screenshot failed: ${name}: ${e}`);
  }
  return path;
}

// =================== TESTS ===================

test.describe.serial('Retest All Fixes', () => {

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // ======= P1 FIXES =======

  test('P1-a: Folders sidebar "+" button — should open create modal, NOT 404', async ({ page }) => {
    try {
      await page.goto(`${BASE}/dashboard`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1500);

      // Look for sidebar Folders section and its "+" button
      // Try multiple selectors
      const plusSelectors = [
        'a[href="/folders/new"]',
        'a[title="Create Folder"]',
        '[data-testid="sidebar-folders-add"]',
        'nav a[href*="folders/new"]',
        'aside a[href*="folders/new"]',
      ];

      let plusFound = false;
      for (const sel of plusSelectors) {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 1000 }).catch(() => false)) {
          await el.click();
          plusFound = true;
          break;
        }
      }

      if (!plusFound) {
        // Try finding "+" or "add" icon near "Folders" text in sidebar
        const sidebarFolders = page.locator('nav, aside').locator('text=Folders').first();
        if (await sidebarFolders.isVisible({ timeout: 2000 }).catch(() => false)) {
          // Look for sibling/nearby + button
          const parent = sidebarFolders.locator('..').first();
          const addBtn = parent.locator('a, button').filter({ hasText: /\+/ }).first();
          if (await addBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
            await addBtn.click();
            plusFound = true;
          } else {
            // Try icon button nearby
            const iconBtn = parent.locator('a[href*="folder"], button[aria-label*="add" i], button[aria-label*="create" i]').first();
            if (await iconBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
              await iconBtn.click();
              plusFound = true;
            }
          }
        }
      }

      await page.waitForTimeout(2000);
      const shot = await safeScreenshot(page, 'retest-01-folders-plus');
      const currentUrl = page.url();

      if (!plusFound) {
        logResult('BUG-001', 'SKIP', 'Folders sidebar "+" button not found', shot, 'Cannot locate the + button in sidebar');
      } else {
        // Check: NOT a 404 page
        const has404 = await page.locator('text=/not found|404/i').isVisible({ timeout: 1000 }).catch(() => false);
        // Check for modal or create form
        const hasModal = await page.locator('[role="dialog"], [class*="modal" i], [class*="Modal"]').isVisible({ timeout: 1000 }).catch(() => false);
        const hasForm = await page.locator('form, input[name="name"]').isVisible({ timeout: 1000 }).catch(() => false);

        if (has404) {
          logResult('BUG-001', 'FAIL', 'Folders sidebar "+" leads to 404', shot, `URL: ${currentUrl}`);
        } else if (hasModal || hasForm) {
          logResult('BUG-001', 'PASS', 'Folders sidebar "+" opens create modal/form', shot, `URL: ${currentUrl}`);
        } else {
          // Check if it navigated to folders page with create dialog
          const mainContent = await page.locator('main').textContent().catch(() => '');
          if (mainContent && mainContent.length > 30) {
            logResult('BUG-001', 'PARTIAL', 'Folders sidebar "+" navigates somewhere but unclear if create form', shot, `URL: ${currentUrl}, content length: ${mainContent.length}`);
          } else {
            logResult('BUG-001', 'FAIL', 'Folders sidebar "+" leads to empty/broken page', shot, `URL: ${currentUrl}`);
          }
        }
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-01-folders-plus-error');
      logResult('BUG-001', 'ERROR', `Folders sidebar "+" test crashed: ${e}`, shot);
    }
  });

  test('P1-b: Security Groups priorities — should be 100, 200, 300 (not all 100)', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network/security-groups`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1500);

      // Click Create
      const createBtn = page.locator('button:has-text("Create Security Group"), button:has-text("Create")').first();
      await expect(createBtn).toBeVisible({ timeout: 5000 });
      await createBtn.click();
      await page.waitForTimeout(1500);

      // Step 1: Fill name (lowercase letters, numbers, hyphens only)
      const nameInput = page.locator('input[placeholder*="my-security-group"], input[name="name"]').first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill('retest-sg-audit');
        await page.waitForTimeout(500);
      }

      await safeScreenshot(page, 'retest-02a-sg-step1-filled');

      // Wait for Next button to become enabled and click
      const nextBtn = page.getByRole('button', { name: /next/i });
      await expect(nextBtn).toBeEnabled({ timeout: 5000 });
      await nextBtn.click();
      await page.waitForTimeout(2000);

      await safeScreenshot(page, 'retest-02b-sg-step2-empty');

      // Add SSH template
      const sshBtn = page.locator('button:has-text("SSH")').first();
      if (await sshBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
        await sshBtn.click();
        await page.waitForTimeout(800);
      }

      // Add HTTP template
      const httpBtn = page.locator('button:has-text("HTTP")').first();
      if (await httpBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
        await httpBtn.click();
        await page.waitForTimeout(800);
      }

      // Add HTTPS template
      const httpsBtn = page.locator('button:has-text("HTTPS")').first();
      if (await httpsBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
        await httpsBtn.click();
        await page.waitForTimeout(800);
      }

      const shot = await safeScreenshot(page, 'retest-02-sg-priorities');

      // Read all priority values — try multiple selectors
      let priorityInputs = page.locator('input[name*="priority"]');
      let count = await priorityInputs.count();

      if (count === 0) {
        // Try type=number
        priorityInputs = page.locator('input[type="number"]');
        count = await priorityInputs.count();
      }

      const priorities: string[] = [];
      for (let i = 0; i < count; i++) {
        const val = await priorityInputs.nth(i).inputValue().catch(() => '');
        if (val) priorities.push(val);
      }

      console.log(`SG priorities found (${count} inputs): ${priorities.join(', ')}`);

      // Also try to read priority from text content (in case it's not an input)
      if (priorities.length === 0) {
        const allText = await page.locator('[class*="rule"], [class*="Rule"], tr').allTextContents().catch(() => []);
        console.log(`Rule row texts: ${allText.slice(0, 5).join(' | ')}`);
      }

      if (priorities.length >= 3) {
        const allSame = priorities.every(p => p === priorities[0]);
        if (allSame) {
          logResult('BUG-002', 'FAIL', `SG priorities all same: ${priorities.join(', ')}`, shot, 'Expected 100, 200, 300');
        } else {
          // Check if they are incrementing
          const expected = ['100', '200', '300'];
          const match = priorities.slice(0, 3).every((p, i) => p === expected[i]);
          if (match) {
            logResult('BUG-002', 'PASS', `SG priorities correct: ${priorities.join(', ')}`, shot);
          } else {
            logResult('BUG-002', 'PARTIAL', `SG priorities not all same but unexpected: ${priorities.join(', ')}`, shot, 'Expected 100, 200, 300');
          }
        }
      } else if (priorities.length > 0) {
        logResult('BUG-002', 'PARTIAL', `Only ${priorities.length} priority inputs found: ${priorities.join(', ')}`, shot, 'Expected 3 rules');
      } else {
        logResult('BUG-002', 'SKIP', 'No priority inputs found on SG step 2', shot, 'Cannot verify priorities — check screenshot manually');
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-02-sg-priorities-error');
      logResult('BUG-002', 'ERROR', `SG priorities test crashed: ${e}`, shot);
    }
  });

  test('P1-c: VPC Create — should open wizard, not spinner/404', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network?tab=vpcs`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1500);

      // Click Create dropdown
      const createBtn = page.getByRole('button', { name: /create/i }).first();
      await expect(createBtn).toBeVisible({ timeout: 5000 });
      await createBtn.click();
      await page.waitForTimeout(500);

      await safeScreenshot(page, 'retest-03a-vpc-dropdown');

      // Click "Create VPC" in dropdown
      const vpcOption = page.locator('button:text("Create VPC"), [role="menuitem"]:text("Create VPC"), li:text("Create VPC")').first();
      const vpcOptionVisible = await vpcOption.isVisible({ timeout: 3000 }).catch(() => false);

      if (!vpcOptionVisible) {
        // Try alternative selectors
        const altOption = page.locator('text="Create VPC"').first();
        if (await altOption.isVisible({ timeout: 2000 }).catch(() => false)) {
          await altOption.click();
        } else {
          const shot = await safeScreenshot(page, 'retest-03-vpc-create');
          logResult('BUG-003', 'FAIL', 'Create VPC option not found in dropdown', shot);
          return;
        }
      } else {
        await vpcOption.click();
      }

      await page.waitForTimeout(2000);
      const shot = await safeScreenshot(page, 'retest-03-vpc-create');
      const currentUrl = page.url();

      // Check for spinner stuck
      const spinner = page.locator('[class*="spinner" i], [class*="loading" i], [role="progressbar"]');
      const spinnerVisible = await spinner.isVisible({ timeout: 1000 }).catch(() => false);

      // Check for 404
      const has404 = await page.locator('text=/not found|404/i').isVisible({ timeout: 1000 }).catch(() => false);

      // Check for wizard/form/modal — broader selectors
      const hasDialog = await page.locator('[role="dialog"]').isVisible({ timeout: 2000 }).catch(() => false);
      const hasWizard = await page.locator('form, [class*="wizard" i], [class*="stepper" i]').isVisible({ timeout: 1000 }).catch(() => false);
      const hasInput = await page.locator('input[name="name"], input[placeholder*="my-vpc" i], input[placeholder*="name" i]').isVisible({ timeout: 2000 }).catch(() => false);
      const hasCreateVpcTitle = await page.locator('text="Create VPC"').isVisible({ timeout: 1000 }).catch(() => false);

      if (has404) {
        logResult('BUG-003', 'FAIL', 'VPC Create leads to 404', shot, `URL: ${currentUrl}`);
      } else if (spinnerVisible && !hasDialog && !hasWizard && !hasInput) {
        logResult('BUG-003', 'FAIL', 'VPC Create stuck on spinner', shot, `URL: ${currentUrl}`);
      } else if (hasDialog || hasWizard || hasInput || hasCreateVpcTitle) {
        logResult('BUG-003', 'PASS', 'VPC Create wizard opens correctly', shot, `URL: ${currentUrl}`);
      } else {
        logResult('BUG-003', 'PARTIAL', 'VPC Create opened something but no clear wizard', shot, `URL: ${currentUrl}`);
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-03-vpc-create-error');
      logResult('BUG-003', 'ERROR', `VPC Create test crashed: ${e}`, shot);
    }
  });

  test('P1-d: Subnet Create — should open wizard/modal, not spinner', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network?tab=subnets`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1500);

      // Click Create dropdown
      const createBtn = page.getByRole('button', { name: /create/i }).first();
      await expect(createBtn).toBeVisible({ timeout: 5000 });
      await createBtn.click();
      await page.waitForTimeout(500);

      await safeScreenshot(page, 'retest-04a-subnet-dropdown');

      // Click "Create Subnet"
      const subnetOption = page.locator('button:text("Create Subnet"), [role="menuitem"]:text("Create Subnet"), li:text("Create Subnet")').first();
      const found = await subnetOption.isVisible({ timeout: 3000 }).catch(() => false);

      if (!found) {
        const altOption = page.locator('text="Create Subnet"').first();
        if (await altOption.isVisible({ timeout: 2000 }).catch(() => false)) {
          await altOption.click();
        } else {
          const shot = await safeScreenshot(page, 'retest-04-subnet-create');
          logResult('BUG-004', 'FAIL', 'Create Subnet option not found in dropdown', shot);
          return;
        }
      } else {
        await subnetOption.click();
      }

      await page.waitForTimeout(2000);
      const shot = await safeScreenshot(page, 'retest-04-subnet-create');
      const currentUrl = page.url();

      // Check for spinner stuck
      const spinner = page.locator('[class*="spinner" i], [class*="loading" i], [role="progressbar"]');
      const spinnerStuck = await spinner.isVisible({ timeout: 1000 }).catch(() => false);

      // Check for wizard/form/modal
      const hasWizard = await page.locator('form, [class*="wizard" i], [class*="stepper" i], [role="dialog"]').isVisible({ timeout: 2000 }).catch(() => false);
      const hasInput = await page.locator('input[name="name"], input[placeholder*="name" i]').isVisible({ timeout: 2000 }).catch(() => false);

      if (spinnerStuck && !hasWizard && !hasInput) {
        logResult('BUG-004', 'FAIL', 'Subnet Create stuck on spinner', shot, `URL: ${currentUrl}`);
      } else if (hasWizard || hasInput) {
        logResult('BUG-004', 'PASS', 'Subnet Create wizard/modal opens correctly', shot, `URL: ${currentUrl}`);
      } else {
        logResult('BUG-004', 'PARTIAL', 'Subnet Create opened something but unclear', shot, `URL: ${currentUrl}`);
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-04-subnet-create-error');
      logResult('BUG-004', 'ERROR', `Subnet Create test crashed: ${e}`, shot);
    }
  });

  // ======= P2 FIXES =======

  test('P2-e: VPCs expandable rows — should show subnets with Name/CIDR/Gateway', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network?tab=vpcs`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(2000);

      const shot1 = await safeScreenshot(page, 'retest-05a-vpcs-list');

      // Try to find and click an expand button
      const expandSelectors = [
        '[aria-label*="expand" i]',
        'button:has(svg[class*="chevron"])',
        'button:has(svg[class*="arrow"])',
        '[class*="expand"] button',
        'tr button:first-child',
        'td:first-child button',
      ];

      let expanded = false;
      for (const sel of expandSelectors) {
        const btn = page.locator(sel).first();
        if (await btn.isVisible({ timeout: 1000 }).catch(() => false)) {
          await btn.click();
          await page.waitForTimeout(1500);
          expanded = true;
          break;
        }
      }

      // If no expand button found, try clicking the row itself
      if (!expanded) {
        const firstRow = page.locator('table tbody tr, [role="row"]').first();
        if (await firstRow.isVisible({ timeout: 1000 }).catch(() => false)) {
          await firstRow.click();
          await page.waitForTimeout(1500);
          expanded = true;
        }
      }

      const shot = await safeScreenshot(page, 'retest-05-vpcs-expanded');

      if (expanded) {
        // Check for subnet info in expanded area — broader check
        const subnetText = page.locator('text=/subnet|cidr|gateway|10\\./i');
        const hasSubnetInfo = await subnetText.isVisible({ timeout: 2000 }).catch(() => false);

        // Also check if a nested table or detail section appeared
        const nestedContent = page.locator('table table, [class*="expanded"], [class*="detail"], [class*="nested"]');
        const hasNestedContent = await nestedContent.isVisible({ timeout: 1000 }).catch(() => false);

        if (hasSubnetInfo || hasNestedContent) {
          logResult('BUG-005', 'PASS', 'VPCs expandable rows show subnet info', shot);
        } else {
          logResult('BUG-005', 'PARTIAL', 'VPCs row expanded but no subnet Name/CIDR/Gateway visible', shot, 'Check screenshot — may need different expand mechanism');
        }
      } else {
        // Check if there are any VPCs at all
        const rows = page.locator('table tr, [role="row"]');
        const rowCount = await rows.count();
        if (rowCount <= 1) {
          logResult('BUG-005', 'SKIP', 'No VPC rows to expand (empty table)', shot, `Row count: ${rowCount}`);
        } else {
          logResult('BUG-005', 'FAIL', 'No expandable button found on VPC rows', shot, `Rows found: ${rowCount}`);
        }
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-05-vpcs-expanded-error');
      logResult('BUG-005', 'ERROR', `VPCs expandable rows test crashed: ${e}`, shot);
    }
  });

  test('P2-f: Egress Gateways — should be DataTable, not custom cards', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network/egress-gateways`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(2000);

      const shot = await safeScreenshot(page, 'retest-06-egress-gateways');

      // Check for DataTable
      const hasTable = await page.locator('table, [role="table"], [class*="DataTable"]').isVisible({ timeout: 2000 }).catch(() => false);
      // Check for cards (bad)
      const hasCards = await page.locator('[class*="card-grid" i], [class*="CardGrid"]').isVisible({ timeout: 1000 }).catch(() => false);
      // Check for ActionBar
      const hasActionBar = await page.locator('[class*="ActionBar"], [class*="action-bar"], [class*="toolbar"]').isVisible({ timeout: 1000 }).catch(() => false);
      // Check for search
      const hasSearch = await page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i]').isVisible({ timeout: 1000 }).catch(() => false);
      // Check for Create button
      const hasCreate = await page.getByRole('button', { name: /create/i }).isVisible({ timeout: 1000 }).catch(() => false);
      // Check for empty state
      const hasEmptyState = await page.locator('text=/no .*(found|gateways|data|results)/i').isVisible({ timeout: 1000 }).catch(() => false);

      console.log(`Egress GW: table=${hasTable}, cards=${hasCards}, actionBar=${hasActionBar}, search=${hasSearch}, create=${hasCreate}, empty=${hasEmptyState}`);

      if (hasCards && !hasTable) {
        logResult('BUG-006', 'FAIL', 'Egress Gateways uses custom cards instead of DataTable', shot);
      } else if (hasTable) {
        logResult('BUG-006', 'PASS', 'Egress Gateways uses DataTable correctly', shot, `ActionBar=${hasActionBar}, search=${hasSearch}, create=${hasCreate}`);
      } else if (hasEmptyState) {
        logResult('BUG-006', 'PARTIAL', 'Egress Gateways shows empty state — check if DataTable is used when data exists', shot);
      } else {
        logResult('BUG-006', 'PARTIAL', 'Egress Gateways: neither table nor cards found', shot, 'May be a different layout');
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-06-egress-gateways-error');
      logResult('BUG-006', 'ERROR', `Egress Gateways test crashed: ${e}`, shot);
    }
  });

  test('P2-g: Subnets tab — should be DataTable, not hierarchical tree', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network?tab=subnets`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(2000);

      const shot = await safeScreenshot(page, 'retest-07-subnets-tab');

      // Check for DataTable
      const hasTable = await page.locator('table, [role="table"], [class*="DataTable"]').isVisible({ timeout: 2000 }).catch(() => false);
      // Check for tree view (bad)
      const hasTree = await page.locator('[class*="tree" i], [role="tree"], [class*="Tree"]').isVisible({ timeout: 1000 }).catch(() => false);
      // Check for summary cards (OK at top)
      const hasSummaryCards = await page.locator('[class*="summary" i], [class*="stat-card" i], [class*="kpi" i]').isVisible({ timeout: 1000 }).catch(() => false);

      console.log(`Subnets: table=${hasTable}, tree=${hasTree}, summaryCards=${hasSummaryCards}`);

      if (hasTree && !hasTable) {
        logResult('BUG-007', 'FAIL', 'Subnets tab uses hierarchical tree instead of DataTable', shot);
      } else if (hasTable) {
        logResult('BUG-007', 'PASS', 'Subnets tab uses DataTable correctly', shot, `Summary cards on top: ${hasSummaryCards}`);
      } else {
        logResult('BUG-007', 'PARTIAL', 'Subnets tab: no clear DataTable or tree found', shot, 'Check screenshot');
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-07-subnets-tab-error');
      logResult('BUG-007', 'ERROR', `Subnets tab test crashed: ${e}`, shot);
    }
  });

  test('P2-h: VMs Create button — wizard should open', async ({ page }) => {
    try {
      await page.goto(`${BASE}/vms`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(2000);

      // Check page loaded without error
      const hasError = await page.locator('text=/Cannot access|error occurred/i').isVisible({ timeout: 1000 }).catch(() => false);
      if (hasError) {
        const shot = await safeScreenshot(page, 'retest-08-vms-error');
        logResult('BUG-008', 'FAIL', 'VMs page shows error, cannot test Create', shot);
        return;
      }

      const createBtn = page.getByRole('button', { name: /create/i }).first();
      const createVisible = await createBtn.isVisible({ timeout: 3000 }).catch(() => false);

      if (!createVisible) {
        // Try "Create VM" specifically
        const createVmBtn = page.locator('button:has-text("Create VM")').first();
        if (await createVmBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
          await createVmBtn.click();
        } else {
          const shot = await safeScreenshot(page, 'retest-08-vms-create');
          logResult('BUG-008', 'FAIL', 'Create VM button not found on VMs page', shot);
          return;
        }
      } else {
        await createBtn.click();
      }

      await page.waitForTimeout(2000);
      const shot = await safeScreenshot(page, 'retest-08-vms-create');

      // Check for wizard
      const hasWizard = await page.locator('form, [class*="wizard" i], [class*="stepper" i], [role="dialog"]').isVisible({ timeout: 2000 }).catch(() => false);
      const hasInput = await page.locator('input[name="name"], input[placeholder*="name" i]').isVisible({ timeout: 2000 }).catch(() => false);

      if (hasWizard || hasInput) {
        logResult('BUG-008', 'PASS', 'Create VM wizard opens correctly', shot);
      } else {
        logResult('BUG-008', 'FAIL', 'Create VM button clicked but no wizard appeared', shot);
      }
    } catch (e) {
      const shot = await safeScreenshot(page, 'retest-08-vms-create-error');
      logResult('BUG-008', 'ERROR', `VMs Create test crashed: ${e}`, shot);
    }
  });

  // ======= CONSISTENCY CHECK =======

  test('Consistency: Screenshot all pages and check h1/ActionBar/DataTable/Breadcrumbs', async ({ page }) => {
    test.setTimeout(180000); // 3 minutes for 17 pages
    const allPages = [
      { name: 'dashboard', path: '/dashboard', isList: false },
      { name: 'vms', path: '/vms', isList: true },
      { name: 'templates', path: '/vms/templates', isList: true },
      { name: 'storage-images', path: '/storage/images', isList: true },
      { name: 'storage-classes', path: '/storage/classes', isList: true },
      { name: 'network-vpcs', path: '/network?tab=vpcs', isList: true },
      { name: 'network-subnets', path: '/network?tab=subnets', isList: true },
      { name: 'network-system', path: '/network?tab=system', isList: true },
      { name: 'egress-gateways', path: '/network/egress-gateways', isList: true },
      { name: 'security-groups', path: '/network/security-groups', isList: true },
      { name: 'cluster', path: '/cluster', isList: false },
      { name: 'folders', path: '/folders', isList: true },
      { name: 'tenants', path: '/tenants', isList: true },
      { name: 'users', path: '/users', isList: true },
      { name: 'groups', path: '/users/groups', isList: true },
      { name: 'profile', path: '/profile', isList: false },
      { name: 'cli-access', path: '/cli-access', isList: false },
    ];

    const consistencyIssues: string[] = [];

    for (const p of allPages) {
      try {
        await page.goto(`${BASE}${p.path}`, { timeout: 15000 });
        await page.waitForLoadState('networkidle').catch(() => {});
        await page.waitForTimeout(1000);

        await safeScreenshot(page, `retest-page-${p.name}`);

        // Check h1
        const h1Count = await page.locator('h1').count();
        const h2Count = await page.locator('h2').count();
        if (h1Count === 0) {
          if (h2Count > 0) {
            const h2text = await page.locator('h2').first().textContent().catch(() => '?');
            consistencyIssues.push(`${p.name}: Uses h2 ("${h2text}") instead of h1 for title`);
          } else {
            consistencyIssues.push(`${p.name}: No h1 or h2 page title found`);
          }
        }

        // For list pages: check ActionBar and DataTable
        if (p.isList) {
          // Look for any button containing "Create" text (e.g. "Create VM", "Create VPC", "+ Create Folder")
          const hasCreateBtn = await page.locator('button:has-text("Create"), a:has-text("Create")').first().isVisible({ timeout: 1500 }).catch(() => false);
          if (!hasCreateBtn) {
            // Check for "Add" or "New" button variants
            const hasAltCreate = await page.locator('button:has-text("Add"), button:has-text("New"), button:has-text("Invite")').isVisible({ timeout: 500 }).catch(() => false);
            if (!hasAltCreate) {
              consistencyIssues.push(`${p.name}: No Create/Add button found in ActionBar`);
            } else {
              consistencyIssues.push(`${p.name}: Uses "Add/New/Invite" instead of "Create X"`);
            }
          }

          const hasTable = await page.locator('table, [role="table"]').isVisible({ timeout: 1000 }).catch(() => false);
          if (!hasTable) {
            const hasEmptyState = await page.locator('text=/no .*(found|data|results|items)/i').isVisible({ timeout: 500 }).catch(() => false);
            if (!hasEmptyState) {
              consistencyIssues.push(`${p.name}: No DataTable found (not empty state either)`);
            }
          }
        }
      } catch (e) {
        consistencyIssues.push(`${p.name}: Page crashed during check — ${e}`);
      }
    }

    // Log all issues
    console.log(`\n========== CONSISTENCY CHECK ==========`);
    console.log(`Total issues: ${consistencyIssues.length}`);
    consistencyIssues.forEach(i => console.log(`  - ${i}`));
    console.log(`=======================================\n`);

    // Store for report
    if (consistencyIssues.length > 0) {
      logResult('CONSISTENCY', 'ISSUES', `Found ${consistencyIssues.length} consistency issues`, '', consistencyIssues.join('\n'));
    } else {
      logResult('CONSISTENCY', 'PASS', 'All pages consistent', '');
    }
  });

  // ======= SUMMARY =======
  test('SUMMARY: Print all retest results', async () => {
    console.log('\n\n========== RETEST RESULTS SUMMARY ==========');
    results.forEach(r => {
      console.log(`[${r.status}] ${r.id}: ${r.description}`);
      if (r.comment) console.log(`         ${r.comment}`);
      if (r.screenshot) console.log(`         Screenshot: ${r.screenshot}`);
    });
    console.log('=============================================\n');
  });
});
