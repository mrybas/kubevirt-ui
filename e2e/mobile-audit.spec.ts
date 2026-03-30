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
interface Finding {
  id: string;
  category: string;
  severity: string;
  page: string;
  description: string;
  screenshot: string;
}

const findings: Finding[] = [];

function logFinding(id: string, category: string, severity: string, pageName: string, description: string, screenshot: string) {
  findings.push({ id, category, severity, page: pageName, description, screenshot });
  console.log(`[${id}] [${category}] [${severity}] ${pageName}: ${description}`);
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

async function safeClick(page: Page, locator: ReturnType<Page['locator']>, timeout = 5000): Promise<boolean> {
  try {
    await locator.waitFor({ state: 'visible', timeout });
    await locator.click();
    return true;
  } catch {
    return false;
  }
}

// =================== MOBILE VIEWPORT CHECKS ===================

async function checkOverflow(page: Page, pageName: string, screenshotName: string) {
  // Check if any element overflows the viewport horizontally
  const overflowInfo = await page.evaluate(() => {
    const vw = window.innerWidth;
    const issues: string[] = [];
    const allElements = document.querySelectorAll('*');
    for (const el of allElements) {
      const rect = el.getBoundingClientRect();
      if (rect.right > vw + 5 && rect.width > 0) {
        const tag = el.tagName.toLowerCase();
        const cls = el.className?.toString?.()?.slice(0, 60) || '';
        const id = el.id || '';
        issues.push(`${tag}${id ? '#' + id : ''}${cls ? '.' + cls.split(' ')[0] : ''} overflows by ${Math.round(rect.right - vw)}px (width=${Math.round(rect.width)}px)`);
      }
    }
    // Deduplicate
    return [...new Set(issues)].slice(0, 10);
  });

  if (overflowInfo.length > 0) {
    logFinding(`OVF-${screenshotName}`, 'LAYOUT', 'P1', pageName,
      `Horizontal overflow: ${overflowInfo.join('; ')}`, screenshotName);
  }

  // Check if page has horizontal scrollbar
  const hasHScroll = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  if (hasHScroll) {
    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    logFinding(`HSCROLL-${screenshotName}`, 'LAYOUT', 'P1', pageName,
      `Page has horizontal scrollbar (scrollWidth=${scrollWidth}px, viewport=375px)`, screenshotName);
  }
}

async function checkSidebar(page: Page, pageName: string, screenshotName: string) {
  // Check sidebar state on mobile
  const sidebarInfo = await page.evaluate(() => {
    // Look for sidebar/nav elements
    const sidebar = document.querySelector('[class*="sidebar" i], [class*="Sidebar"], nav[class*="nav" i], aside');
    if (!sidebar) return { found: false, visible: false, width: 0, overlapsContent: false };

    const rect = sidebar.getBoundingClientRect();
    const style = window.getComputedStyle(sidebar);
    const visible = rect.width > 0 && style.display !== 'none' && style.visibility !== 'hidden';

    // Check if sidebar overlaps main content
    const main = document.querySelector('main, [class*="content" i], [class*="Content"]');
    let overlapsContent = false;
    if (main && visible) {
      const mainRect = main.getBoundingClientRect();
      overlapsContent = rect.right > mainRect.left && rect.left < mainRect.right;
    }

    return { found: true, visible, width: Math.round(rect.width), overlapsContent, display: style.display, position: style.position };
  });

  if (sidebarInfo.found && sidebarInfo.visible && sidebarInfo.width > 80) {
    // Sidebar is expanded on mobile -- is it overlaying or pushing content?
    if (sidebarInfo.position === 'fixed' || sidebarInfo.position === 'absolute') {
      // Overlay is OK for mobile if there's a way to close it
    } else if (sidebarInfo.width > 200) {
      logFinding(`SIDEBAR-${screenshotName}`, 'NAVIGATION', 'P1', pageName,
        `Sidebar is expanded (${sidebarInfo.width}px) on mobile and not overlaying -- pushes content off screen`, screenshotName);
    }
  }

  // Check sidebar toggle/hamburger
  const hasToggle = await page.locator('[aria-label*="menu" i], [aria-label*="sidebar" i], [aria-label*="collapse" i], [aria-label*="toggle" i], button:has(svg[class*="menu" i])').first().isVisible({ timeout: 1000 }).catch(() => false);
  if (!hasToggle && sidebarInfo.found) {
    logFinding(`NOTOGGLE-${screenshotName}`, 'NAVIGATION', 'P2', pageName,
      `No hamburger/toggle button found for sidebar on mobile`, screenshotName);
  }
}

async function checkButtons(page: Page, pageName: string, screenshotName: string) {
  // Check if action buttons are visible and not clipped
  const buttonInfo = await page.evaluate(() => {
    const vw = window.innerWidth;
    const buttons = document.querySelectorAll('button, a[role="button"]');
    const issues: string[] = [];
    for (const btn of buttons) {
      const rect = btn.getBoundingClientRect();
      const text = btn.textContent?.trim() || '';
      if (text.length === 0) continue;

      // Button off screen
      if (rect.right > vw + 2 && rect.width > 0) {
        issues.push(`Button "${text.slice(0, 30)}" overflows viewport by ${Math.round(rect.right - vw)}px`);
      }

      // Button too small for touch (< 32px)
      if (rect.height > 0 && (rect.height < 32 || rect.width < 32)) {
        // Only flag if it looks like an action button
        if (text.match(/create|delete|edit|save|cancel|next|back|submit/i)) {
          issues.push(`Button "${text.slice(0, 30)}" too small for touch: ${Math.round(rect.width)}x${Math.round(rect.height)}px`);
        }
      }
    }
    return [...new Set(issues)].slice(0, 8);
  });

  if (buttonInfo.length > 0) {
    logFinding(`BTN-${screenshotName}`, 'INTERACTION', 'P2', pageName,
      buttonInfo.join('; '), screenshotName);
  }
}

async function checkTables(page: Page, pageName: string, screenshotName: string) {
  const tableInfo = await page.evaluate(() => {
    const vw = window.innerWidth;
    const tables = document.querySelectorAll('table, [role="table"], [class*="DataTable"], [class*="data-table"]');
    const issues: string[] = [];

    for (const table of tables) {
      const rect = table.getBoundingClientRect();
      if (rect.width > vw) {
        const parent = table.parentElement;
        const parentStyle = parent ? window.getComputedStyle(parent) : null;
        const hasScroll = parentStyle && (parentStyle.overflowX === 'auto' || parentStyle.overflowX === 'scroll');
        if (hasScroll) {
          issues.push(`Table wider than viewport (${Math.round(rect.width)}px) with horizontal scroll wrapper -- OK but may need UX improvement`);
        } else {
          issues.push(`Table wider than viewport (${Math.round(rect.width)}px) WITHOUT scroll wrapper -- content clipped or overflows`);
        }
      }

      // Check column count
      const headerCells = table.querySelectorAll('th, [role="columnheader"]');
      if (headerCells.length > 4) {
        issues.push(`Table has ${headerCells.length} columns -- too many for 375px mobile screen`);
      }
    }
    return issues;
  });

  if (tableInfo.length > 0) {
    logFinding(`TABLE-${screenshotName}`, 'TABLE', 'P1', pageName,
      tableInfo.join('; '), screenshotName);
  }
}

async function checkTextReadability(page: Page, pageName: string, screenshotName: string) {
  const textInfo = await page.evaluate(() => {
    const issues: string[] = [];
    // Check h1/h2 truncation
    const headings = document.querySelectorAll('h1, h2, h3');
    for (const h of headings) {
      const style = window.getComputedStyle(h);
      const rect = h.getBoundingClientRect();
      if (style.overflow === 'hidden' && style.textOverflow === 'ellipsis' && rect.width > 0) {
        // Check if text is actually truncated
        if ((h as HTMLElement).scrollWidth > rect.width + 5) {
          issues.push(`Heading "${h.textContent?.trim().slice(0, 40)}" is truncated (ellipsis)`);
        }
      }
      // Check font size
      const fontSize = parseFloat(style.fontSize);
      if (fontSize < 12 && rect.width > 0 && h.textContent?.trim()) {
        issues.push(`Heading "${h.textContent?.trim().slice(0, 30)}" font size ${fontSize}px is too small`);
      }
    }

    // Check body text font size
    const paragraphs = document.querySelectorAll('p, span, td, [class*="cell"]');
    let smallTextCount = 0;
    for (const p of paragraphs) {
      const style = window.getComputedStyle(p);
      const fontSize = parseFloat(style.fontSize);
      const rect = p.getBoundingClientRect();
      if (fontSize < 11 && rect.width > 0 && p.textContent?.trim()) {
        smallTextCount++;
      }
    }
    if (smallTextCount > 5) {
      issues.push(`${smallTextCount} text elements with font-size < 11px -- hard to read on mobile`);
    }

    return issues;
  });

  if (textInfo.length > 0) {
    logFinding(`TEXT-${screenshotName}`, 'READABILITY', 'P2', pageName,
      textInfo.join('; '), screenshotName);
  }
}

async function fullMobileCheck(page: Page, pageName: string, screenshotName: string) {
  await page.waitForTimeout(1500);
  await safeScreenshot(page, screenshotName);
  await checkOverflow(page, pageName, screenshotName);
  await checkSidebar(page, pageName, screenshotName);
  await checkButtons(page, pageName, screenshotName);
  await checkTables(page, pageName, screenshotName);
  await checkTextReadability(page, pageName, screenshotName);
}

// =================== PHASE 1: MOBILE TESTS (375x812) ===================

test.describe.serial('Mobile Audit (iPhone 375x812)', () => {

  test.use({
    viewport: { width: 375, height: 812 },
  });

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // --- Phase 1: Login ---

  test('M-00: Login page mobile', async ({ page }) => {
    // We need to check login page itself, so go back
    // Clear cookies to see login page
    await page.context().clearCookies();
    await page.goto(`${BASE}/login`);
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'mobile-00-login-page');

    // Check login form fits
    await checkOverflow(page, 'Login Page', 'mobile-00-login-page');
    await checkButtons(page, 'Login Page', 'mobile-00-login-page');
    await checkTextReadability(page, 'Login Page', 'mobile-00-login-page');

    // Now do login
    const ssoBtn = page.getByRole('button', { name: /Sign in with SSO/i });
    if (await ssoBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await ssoBtn.click();
      await page.waitForURL(url => !url.href.includes('localhost:3333'), { timeout: 15000 });
      await page.waitForLoadState('domcontentloaded').catch(() => {});

      const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in/i });
      if (await connectorLink.isVisible({ timeout: 3000 }).catch(() => false)) {
        await connectorLink.click();
      }
      await page.waitForTimeout(500);

      // DEX login page screenshot
      await safeScreenshot(page, 'mobile-00-dex-login');
      await checkOverflow(page, 'DEX Login', 'mobile-00-dex-login');

      await page.getByRole('textbox', { name: /username/i }).fill('admin');
      await page.getByRole('textbox', { name: /password/i }).fill('admin_password');
      await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

      await page.waitForURL('**/dashboard', { timeout: 30000 });
      await page.waitForLoadState('domcontentloaded').catch(() => {});
      await page.waitForTimeout(1500);
      await safeScreenshot(page, 'mobile-00-after-login');
    }
  });

  // --- Phase 2: All pages ---

  const pages = [
    { name: 'Dashboard', path: '/dashboard', slug: '01-dashboard' },
    { name: 'Virtual Machines', path: '/vms', slug: '02-vms' },
    { name: 'VM Templates', path: '/vms/templates', slug: '03-vm-templates' },
    { name: 'Storage > Images', path: '/storage/images', slug: '04-storage-images' },
    { name: 'Storage > Classes', path: '/storage/classes', slug: '05-storage-classes' },
    { name: 'Network (VPCs)', path: '/network?tab=vpcs', slug: '06-network-vpcs' },
    { name: 'Network (Subnets)', path: '/network?tab=subnets', slug: '07-network-subnets' },
    { name: 'Network (System)', path: '/network?tab=system', slug: '08-network-system' },
    { name: 'Egress Gateways', path: '/network/egress-gateways', slug: '09-egress-gateways' },
    { name: 'Security Groups', path: '/network/security-groups', slug: '10-security-groups' },
    { name: 'Cluster', path: '/cluster', slug: '11-cluster' },
    { name: 'Folders', path: '/folders', slug: '12-folders' },
    { name: 'Tenants', path: '/tenants', slug: '13-tenants' },
    { name: 'Users', path: '/users', slug: '14-users' },
    { name: 'Groups', path: '/users/groups', slug: '15-groups' },
    { name: 'Profile', path: '/profile', slug: '16-profile' },
    { name: 'CLI Access', path: '/cli-access', slug: '17-cli-access' },
  ];

  for (const p of pages) {
    test(`M-${p.slug}: ${p.name}`, async ({ page }) => {
      try {
        await page.goto(`${BASE}${p.path}`);
        await page.waitForLoadState('networkidle').catch(() => {});
        await fullMobileCheck(page, p.name, `mobile-${p.slug}`);
      } catch (e) {
        console.log(`Error on ${p.name}: ${e}`);
        await safeScreenshot(page, `mobile-${p.slug}-error`);
      }
    });
  }

  // --- Phase 2b: Sidebar toggle on mobile ---

  test('M-18: Sidebar toggle mobile', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`);
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForTimeout(1500);
    await safeScreenshot(page, 'mobile-18-sidebar-initial');

    // Try to find hamburger / toggle button
    const toggleSelectors = [
      '[aria-label*="menu" i]',
      '[aria-label*="sidebar" i]',
      '[aria-label*="collapse" i]',
      '[aria-label*="toggle" i]',
      '[aria-label*="Menu" i]',
      'button[class*="hamburger" i]',
      'button[class*="menu" i]',
    ];

    let toggled = false;
    for (const sel of toggleSelectors) {
      const btn = page.locator(sel).first();
      if (await btn.isVisible({ timeout: 1000 }).catch(() => false)) {
        await btn.click();
        await page.waitForTimeout(500);
        await safeScreenshot(page, 'mobile-18-sidebar-toggled');
        toggled = true;
        console.log(`Sidebar toggled via: ${sel}`);

        // Check if sidebar expanded
        const sidebarState = await page.evaluate(() => {
          const sidebar = document.querySelector('[class*="sidebar" i], [class*="Sidebar"], nav, aside');
          if (!sidebar) return 'not found';
          const rect = sidebar.getBoundingClientRect();
          return `width=${Math.round(rect.width)}, left=${Math.round(rect.left)}`;
        });
        console.log(`After toggle: sidebar ${sidebarState}`);

        // Try to close it
        await btn.click().catch(() => {});
        await page.waitForTimeout(500);
        await safeScreenshot(page, 'mobile-18-sidebar-closed');
        break;
      }
    }

    if (!toggled) {
      logFinding('SIDEBAR-TOGGLE-MISSING', 'NAVIGATION', 'P0', 'All Pages',
        'No sidebar toggle/hamburger button found on mobile -- sidebar may be permanently visible or hidden', 'mobile-18-sidebar-initial');
    }
  });

  // --- Phase 3: Create wizards on mobile ---

  test('M-19: Create VPC wizard mobile', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network?tab=vpcs`);
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(1500);

      const createBtn = page.getByRole('button', { name: /create/i }).first();
      if (await safeClick(page, createBtn)) {
        await page.waitForTimeout(500);
        await safeScreenshot(page, 'mobile-19-vpc-dropdown');

        // Click Create VPC
        const vpcOption = page.locator('button:text("Create VPC"), [role="menuitem"]:text("Create VPC")');
        if (await safeClick(page, vpcOption)) {
          await page.waitForTimeout(1500);
          await page.waitForLoadState('networkidle').catch(() => {});
          await fullMobileCheck(page, 'Create VPC - Step 1', 'mobile-19-vpc-step1');

          // Fill and go to step 2
          const nameInput = page.locator('input[name="name"], input[placeholder*="name" i]').first();
          if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
            await nameInput.fill('mobile-test-vpc');
          }
          const cidrInput = page.locator('input[name="cidr"], input[placeholder*="cidr" i], input[placeholder*="10." i]').first();
          if (await cidrInput.isVisible({ timeout: 2000 }).catch(() => false)) {
            await cidrInput.fill('10.200.0.0/16');
          }
          await safeScreenshot(page, 'mobile-19-vpc-step1-filled');

          const nextBtn = page.getByRole('button', { name: /next/i });
          if (await safeClick(page, nextBtn)) {
            await page.waitForTimeout(1500);
            await fullMobileCheck(page, 'Create VPC - Step 2', 'mobile-19-vpc-step2');

            const nextBtn2 = page.getByRole('button', { name: /next/i });
            if (await safeClick(page, nextBtn2)) {
              await page.waitForTimeout(1000);
              await fullMobileCheck(page, 'Create VPC - Review', 'mobile-19-vpc-step3');
            }
          }
        } else {
          logFinding('WIZARD-VPC-MISSING', 'INTERACTION', 'P1', 'Create VPC',
            'Create VPC option not found in dropdown on mobile', 'mobile-19-vpc-dropdown');
        }
      }
    } catch (e) {
      console.log(`Create VPC wizard error: ${e}`);
    }
  });

  test('M-20: Create Security Group wizard mobile', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network/security-groups`);
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(1500);

      const createBtn = page.getByRole('button', { name: /create/i }).first();
      if (await safeClick(page, createBtn)) {
        await page.waitForTimeout(1500);
        await fullMobileCheck(page, 'Create Security Group - Step 1', 'mobile-20-sg-step1');

        // Click Next to see rules step
        const nextBtn = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn)) {
          await page.waitForTimeout(1500);
          await fullMobileCheck(page, 'Create Security Group - Rules', 'mobile-20-sg-step2');
        }
      }
    } catch (e) {
      console.log(`Create SG wizard error: ${e}`);
    }
  });

  test('M-21: Create Folder modal mobile', async ({ page }) => {
    try {
      await page.goto(`${BASE}/folders`);
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(1500);

      const createBtn = page.getByRole('button', { name: /create/i }).first();
      if (await safeClick(page, createBtn)) {
        await page.waitForTimeout(1500);
        await fullMobileCheck(page, 'Create Folder Modal', 'mobile-21-folder-modal');

        // Check modal fits viewport
        const modalInfo = await page.evaluate(() => {
          const modal = document.querySelector('[role="dialog"], [class*="modal" i], [class*="Modal"], [class*="drawer" i]');
          if (!modal) return { found: false };
          const rect = modal.getBoundingClientRect();
          return {
            found: true,
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            left: Math.round(rect.left),
            top: Math.round(rect.top),
            fitsWidth: rect.right <= window.innerWidth + 5,
            fitsHeight: rect.bottom <= window.innerHeight + 5,
          };
        });

        console.log(`Folder modal info: ${JSON.stringify(modalInfo)}`);
        if (modalInfo.found && !modalInfo.fitsWidth) {
          logFinding('MODAL-FOLDER-OVERFLOW', 'LAYOUT', 'P1', 'Create Folder',
            `Modal overflows viewport: width=${modalInfo.width}px, left=${modalInfo.left}px`, 'mobile-21-folder-modal');
        }
      }
    } catch (e) {
      console.log(`Create Folder error: ${e}`);
    }
  });

  test('M-22: Create VM wizard mobile', async ({ page }) => {
    try {
      await page.goto(`${BASE}/vms`);
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(2000);

      const createBtn = page.getByRole('button', { name: /create/i }).first();
      if (await safeClick(page, createBtn)) {
        await page.waitForTimeout(2000);
        await fullMobileCheck(page, 'Create VM - Step 1', 'mobile-22-vm-step1');

        // Try Next steps
        const nextBtn = page.getByRole('button', { name: /next/i });
        if (await safeClick(page, nextBtn)) {
          await page.waitForTimeout(1500);
          await fullMobileCheck(page, 'Create VM - Step 2', 'mobile-22-vm-step2');

          const nextBtn2 = page.getByRole('button', { name: /next/i });
          if (await safeClick(page, nextBtn2)) {
            await page.waitForTimeout(1000);
            await fullMobileCheck(page, 'Create VM - Step 3', 'mobile-22-vm-step3');
          }
        }
      }
    } catch (e) {
      console.log(`Create VM wizard error: ${e}`);
    }
  });

  // --- Summary ---

  test('M-99: Mobile findings summary', async () => {
    console.log('\n\n========== MOBILE AUDIT FINDINGS (375x812) ==========');
    console.log(`Total findings: ${findings.length}`);
    const byCat: Record<string, number> = {};
    findings.forEach(f => {
      byCat[f.category] = (byCat[f.category] || 0) + 1;
      console.log(`  [${f.severity}] [${f.category}] ${f.page}: ${f.description}`);
      console.log(`    Screenshot: ${f.screenshot}`);
    });
    console.log('\nBy category:');
    Object.entries(byCat).forEach(([cat, count]) => {
      console.log(`  ${cat}: ${count}`);
    });
    console.log('======================================================\n');
  });
});

// =================== PHASE 4: TABLET TESTS (768x1024) ===================

test.describe.serial('Tablet Audit (iPad 768x1024)', () => {

  test.use({
    viewport: { width: 768, height: 1024 },
  });

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  const tabletPages = [
    { name: 'Dashboard', path: '/dashboard', slug: '01-dashboard' },
    { name: 'Virtual Machines', path: '/vms', slug: '02-vms' },
    { name: 'Network (VPCs)', path: '/network?tab=vpcs', slug: '03-network-vpcs' },
    { name: 'Network (Subnets)', path: '/network?tab=subnets', slug: '04-network-subnets' },
    { name: 'Tenants', path: '/tenants', slug: '05-tenants' },
    { name: 'Security Groups', path: '/network/security-groups', slug: '06-security-groups' },
    { name: 'Cluster', path: '/cluster', slug: '07-cluster' },
    { name: 'Users', path: '/users', slug: '08-users' },
  ];

  for (const p of tabletPages) {
    test(`T-${p.slug}: ${p.name}`, async ({ page }) => {
      try {
        await page.goto(`${BASE}${p.path}`);
        await page.waitForLoadState('networkidle').catch(() => {});
        await fullMobileCheck(page, `${p.name} (tablet)`, `tablet-${p.slug}`);
      } catch (e) {
        console.log(`Tablet error on ${p.name}: ${e}`);
        await safeScreenshot(page, `tablet-${p.slug}-error`);
      }
    });
  }

  test('T-09: Create VPC wizard tablet', async ({ page }) => {
    try {
      await page.goto(`${BASE}/network?tab=vpcs`);
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(1500);

      const createBtn = page.getByRole('button', { name: /create/i }).first();
      if (await safeClick(page, createBtn)) {
        await page.waitForTimeout(500);
        const vpcOption = page.locator('button:text("Create VPC"), [role="menuitem"]:text("Create VPC")');
        if (await safeClick(page, vpcOption)) {
          await page.waitForTimeout(1500);
          await fullMobileCheck(page, 'Create VPC tablet', 'tablet-09-vpc-wizard');
        }
      }
    } catch (e) {
      console.log(`Tablet VPC wizard error: ${e}`);
    }
  });

  test('T-99: Tablet findings summary', async () => {
    console.log('\n\n========== TABLET AUDIT FINDINGS (768x1024) ==========');
    console.log(`Total findings: ${findings.length}`);
    findings.forEach(f => {
      console.log(`  [${f.severity}] [${f.category}] ${f.page}: ${f.description}`);
    });
    console.log('======================================================\n');
  });
});
