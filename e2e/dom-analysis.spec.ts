import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:3333';

interface PageAnalysis {
  url: string;
  title: string;
  layout: any;
  actionBar: any;
  contentType: string;
  buttons: any[];
  spacing: any;
  typography: any;
  colors: any;
  elements: any;
}

async function analyzePage(page: any, path: string, name: string): Promise<PageAnalysis> {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);

  const analysis = await page.evaluate(() => {
    const main = document.querySelector('main') || document.querySelector('[class*="content"]') || document.body;
    const sidebar = document.querySelector('aside, nav, [class*="sidebar"], [class*="Sidebar"]');

    // Get computed style helper
    const cs = (el: Element) => window.getComputedStyle(el);

    // Layout
    const mainStyle = main ? cs(main) : null;
    const layout = {
      display: mainStyle?.display,
      flexDirection: mainStyle?.flexDirection,
      gridTemplateColumns: mainStyle?.gridTemplateColumns,
      padding: mainStyle?.padding,
      gap: mainStyle?.gap,
      width: main?.clientWidth,
      height: main?.clientHeight,
    };

    // Sidebar
    const sidebarInfo = sidebar ? {
      width: sidebar.clientWidth,
      background: cs(sidebar).backgroundColor,
      items: Array.from(sidebar.querySelectorAll('a, button')).map(el => ({
        text: el.textContent?.trim().substring(0, 50),
        isActive: el.classList.contains('active') || el.getAttribute('aria-current') === 'page' || cs(el).backgroundColor !== cs(sidebar).backgroundColor,
      })).filter(i => i.text),
    } : null;

    // Action bar (first section with buttons at top of main content)
    const allButtons = Array.from(main.querySelectorAll('button'));
    const headerArea = main.querySelector('h1, h2, [class*="header"], [class*="Header"], [class*="title"]');

    const buttons = allButtons.map(btn => {
      const rect = btn.getBoundingClientRect();
      const style = cs(btn);
      return {
        text: btn.textContent?.trim().substring(0, 40),
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        bg: style.backgroundColor,
        color: style.color,
        position: rect.x < window.innerWidth / 2 ? 'left' : 'right',
        isTopArea: rect.y < 200,
      };
    }).filter(b => b.text && b.width > 0);

    // Content type detection
    const tables = main.querySelectorAll('table');
    const cards = main.querySelectorAll('[class*="card"], [class*="Card"]');
    const grids = main.querySelectorAll('[class*="grid"], [class*="Grid"]');
    let contentType = 'unknown';
    if (tables.length > 0) contentType = 'table';
    else if (cards.length > 3) contentType = 'cards';
    else if (grids.length > 0) contentType = 'grid';

    const tableInfo = tables.length > 0 ? {
      columns: tables[0].querySelectorAll('th, thead td').length,
      rows: tables[0].querySelectorAll('tbody tr').length,
      headers: Array.from(tables[0].querySelectorAll('th')).map(th => th.textContent?.trim()),
    } : null;

    // Typography
    const h1 = main.querySelector('h1');
    const h2 = main.querySelector('h2');
    const p = main.querySelector('p, span, td');
    const typography = {
      h1: h1 ? { size: cs(h1).fontSize, weight: cs(h1).fontWeight, color: cs(h1).color } : null,
      h2: h2 ? { size: cs(h2).fontSize, weight: cs(h2).fontWeight, color: cs(h2).color } : null,
      body: p ? { size: cs(p).fontSize, weight: cs(p).fontWeight, color: cs(p).color } : null,
    };

    // Colors
    const colors = {
      mainBg: mainStyle?.backgroundColor,
      bodyBg: cs(document.body).backgroundColor,
      text: mainStyle?.color,
    };

    // Empty state
    const emptyState = main.querySelector('[class*="empty"], [class*="Empty"], [class*="no-data"], [class*="placeholder"]');
    const emptyStateText = emptyState?.textContent?.trim().substring(0, 100);

    // Forms
    const forms = main.querySelectorAll('form, [class*="form"], [class*="Form"]');
    const inputs = main.querySelectorAll('input, select, textarea');
    const labels = main.querySelectorAll('label');

    return {
      layout,
      sidebar: sidebarInfo,
      buttons,
      contentType,
      tableInfo,
      typography,
      colors,
      emptyStateText,
      formInfo: {
        formCount: forms.length,
        inputCount: inputs.length,
        labelCount: labels.length,
      },
      elementCounts: {
        buttons: allButtons.length,
        links: main.querySelectorAll('a').length,
        images: main.querySelectorAll('img, svg').length,
        tables: tables.length,
        cards: cards.length,
        inputs: inputs.length,
        modals: document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"]').length,
      },
    };
  });

  return {
    url: path,
    title: name,
    ...analysis,
  };
}

async function login(page: any) {
  await page.goto(`${BASE}/login`);
  await page.waitForLoadState('networkidle');

  // Click "Sign in with SSO"
  const ssoBtn = page.getByRole('button', { name: /Sign in with SSO/i });
  await ssoBtn.click();

  // Wait for redirect to DEX
  await page.waitForURL((url: URL) => !url.href.includes('localhost:3333'), { timeout: 15000 });

  // DEX may show connector selection
  const connectorLink = page.getByRole('link', { name: /KubeVirt UI|Log in/i });
  if (await connectorLink.isVisible({ timeout: 3000 }).catch(() => false)) {
    await connectorLink.click();
  }

  // Fill DEX LDAP login form
  await page.getByRole('textbox', { name: /username/i }).fill('admin');
  await page.getByRole('textbox', { name: /password/i }).fill('admin_password');
  await page.getByRole('button', { name: /Login|Log in|Sign in/i }).click();

  // Wait for redirect back to dashboard
  await page.waitForURL('**/dashboard', { timeout: 30000 });
  await page.waitForLoadState('networkidle');
}

test('Full DOM analysis', async ({ page }) => {
  await login(page);

  const pages = [
    { path: '/dashboard', name: 'Dashboard' },
    { path: '/vms', name: 'Virtual Machines' },
    { path: '/vms/templates', name: 'VM Templates' },
    { path: '/storage/images', name: 'Storage Images' },
    { path: '/storage/classes', name: 'Storage Classes' },
    { path: '/network', name: 'User Networks' },
    { path: '/network/system', name: 'System Networks' },
    { path: '/network/vpcs', name: 'VPCs' },
    { path: '/network/egress-gateways', name: 'Egress Gateways' },
    { path: '/network/security-groups', name: 'Security Groups' },
    { path: '/cluster', name: 'Cluster' },
    { path: '/folders', name: 'Folders' },
    { path: '/tenants', name: 'Tenants' },
    { path: '/users', name: 'Users' },
    { path: '/users/groups', name: 'Groups' },
    { path: '/profile', name: 'Profile' },
    { path: '/cli-access', name: 'CLI Access' },
  ];

  const results: PageAnalysis[] = [];

  for (const p of pages) {
    try {
      const analysis = await analyzePage(page, p.path, p.name);
      results.push(analysis);
    } catch (e) {
      results.push({ url: p.path, title: p.name, error: String(e) } as any);
    }
  }

  // Also analyze wizard modals
  // Try Create VM
  await page.goto(`${BASE}/vms`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);
  const createVMBtn = page.locator('button').filter({ hasText: /create|new/i }).first();
  if (await createVMBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
    await createVMBtn.click();
    await page.waitForTimeout(1000);
    const wizardAnalysis = await page.evaluate(() => {
      const dialog = document.querySelector('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="wizard"], [class*="Wizard"]');
      if (!dialog) return null;
      const style = window.getComputedStyle(dialog);
      return {
        width: dialog.clientWidth,
        height: dialog.clientHeight,
        bg: style.backgroundColor,
        steps: Array.from(dialog.querySelectorAll('[class*="step"], [class*="Step"], button')).map(el => el.textContent?.trim()).filter(Boolean),
        inputs: dialog.querySelectorAll('input, select, textarea').length,
        buttons: Array.from(dialog.querySelectorAll('button')).map(b => b.textContent?.trim()),
      };
    });
    results.push({ url: '/vms/create-wizard', title: 'Create VM Wizard', wizard: wizardAnalysis } as any);
  }

  // Write results as JSON
  const fs = require('fs');
  fs.writeFileSync('/screenshots/dom-analysis.json', JSON.stringify(results, null, 2));
});
