/**
 * A boundary that says why is a boundary nobody files twice.
 *
 * UAT run 4, R-3: a folder admin found the network pages closed and reported
 * it, reasonably — "Access Denied. You don't have permission to view this
 * page." reads like a mistake, especially since the backend lists VPCs for
 * that user without complaint.
 *
 * It is not a mistake. A VPC carries BGP announcements, address pools and
 * routes that reach the border router, and one wrong prefix takes traffic
 * that is not yours with it, so networks stay with platform admins. That is a
 * decision, and the page now carries it.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { readFileSync } from 'fs';
import { join } from 'path';

import AccessDenied from '../AccessDenied';

vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));

describe('Access Denied', () => {
  it('says why when there is a why', () => {
    render(<AccessDenied reason="Networks are managed by platform admins." />);
    expect(screen.getByText(/Networks are managed by platform admins/))
      .toBeInTheDocument();
  });

  it('still works where there is nothing to add', () => {
    render(<AccessDenied />);
    expect(screen.getByText(/don't have permission/)).toBeInTheDocument();
  });
});

describe('the network routes', () => {
  const app = readFileSync(join(__dirname, '..', '..', 'App.tsx'), 'utf8');

  it('are still admin-only — this is a decision, not an oversight', () => {
    expect(app).toMatch(/<Route path="\/network" element=\{<RequireAdmin/);
  });

  it('name the danger rather than the rule', () => {
    // "You may not" answers nothing. BGP, address pools and the border router
    // are why, and they are what a person needs in order to ask for the right
    // thing instead of asking again.
    expect(app).toMatch(/BGP announcements/);
    expect(app).toMatch(/border router/);
    expect(app).toMatch(/Ask a platform admin/);
  });

  it('say it once', () => {
    // Seven routes carry it; seven copies of a sentence drift into seven
    // sentences.
    const inline = app.match(/reason=\{"Networks are managed/g) ?? [];
    expect(inline).toHaveLength(0);
    expect(app.match(/reason=\{NETWORKS_ARE_ADMIN_ONLY\}/g)?.length)
      .toBeGreaterThanOrEqual(5);
  });
});
