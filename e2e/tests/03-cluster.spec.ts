import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot } from './helpers';

test.describe('Cluster', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.goto('/cluster');
    await waitForPageLoad(page);
  });

  test('should show cluster page with status', async ({ page }) => {
    // Page heading
    await expect(page.getByRole('heading', { name: /Cluster/i })).toBeVisible();
    await expect(page.getByText('Cluster status, nodes, and components')).toBeVisible();

    // KubeVirt component card
    await expect(page.getByText('KubeVirt')).toBeVisible();

    // CDI component card
    await expect(page.getByText('CDI (Containerized Data Importer)')).toBeVisible();

    await takeScreenshot(page, '03-cluster-status');
  });

  test('should show nodes list', async ({ page }) => {
    // Nodes section with table
    const nodesHeading = page.getByText(/Nodes \(\d+\)/);
    await expect(nodesHeading).toBeVisible({ timeout: 15_000 });

    // Table headers
    await expect(page.getByRole('columnheader', { name: 'Name' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Status' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Roles' })).toBeVisible();

    // At least one row in the table body
    const rows = page.locator('table tbody tr');
    await expect(rows.first()).toBeVisible({ timeout: 15_000 });

    await takeScreenshot(page, '03-cluster-nodes');
  });

  test('should show 3 nodes with correct count', async ({ page }) => {
    // Wait for nodes section
    const nodesHeading = page.getByText(/Nodes \((\d+)\)/);
    await expect(nodesHeading).toBeVisible({ timeout: 15_000 });

    // Verify exactly 3 nodes (matching infra config: .221-.223)
    await expect(page.getByText(/Nodes \(3\)/)).toBeVisible();

    // Verify ready status badge
    await expect(page.getByText(/3 \/ 3 Ready/)).toBeVisible();

    // Verify 3 rows in nodes table
    const rows = page.locator('table tbody tr');
    await expect(rows).toHaveCount(3);

    await takeScreenshot(page, '03-cluster-3-nodes');
  });
});
