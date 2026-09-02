import { test, expect } from '@playwright/test';
import { login, apiRequest } from './helpers';

// ---------------------------------------------------------------------------
// Task 9 — prove token forwarding against a real Harbor.
//
// Every other test in this feature runs against a fake HarborClient that
// accepts any bearer. A fake cannot fail this claim, because it was never
// wired to reject anything — so a green unit-test suite proves nothing about
// whether an identity is actually enforced. Only a real Harbor, reachable at
// HARBOR_URL (see docker-compose.e2e.yml's `harbor-core` service), can show
// that a wrong or absent identity is genuinely refused the catalogue.
//
// TEST LOCATION: the brief (task-9-brief.md) said to create this file at
// `e2e/harbor-identity.spec.ts` (top level). That path is NOT picked up by
// `npx playwright test harbor-identity` — this repo's playwright.config.ts
// sets `testDir: './tests'`, so only e2e/tests/**/*.spec.ts is discovered by
// the command the brief itself specifies (see e2e/walkthrough.config.ts for
// the separate, `testDir: '.'` config that DOES discover the top-level
// exploratory specs — that one is not what the brief's Step 3 command runs).
// Filed here instead so the brief's own acceptance command actually finds it.
//
// AUTH: the brief's snippet calls the bare Playwright `request` fixture with
// no attached credential for the "valid identity" case, and a raw
// `Authorization` header for the "wrong identity" case. Neither reaches the
// code path this test means to exercise under this backend's real auth
// wiring: `require_auth` (backend/app/core/auth.py) 401s any request with NO
// bearer at all before the handler runs, and `validate_oidc_token` calls
// dex's own `/userinfo` endpoint to check ANY bearer it is given — so a
// syntactically-random string is already rejected by kubevirt-ui's own OIDC
// gate, never reaching the Harbor-forwarding code in
// backend/app/api/v1/images_catalog.py at all. This file uses this repo's
// own `login()` + `apiRequest(..., { token })` helpers (already used by
// every other spec under e2e/tests/) to attach a real bearer, which is the
// only way to reach the code under test.
//
// *** THE HEADLINE FINDING (see task-9-report.md for the full reproduction,
// run against a real Harbor 2.15.2 core+db+redis and this repo's own,
// unmodified HarborClient/catalog_images code): the "wrong identity" test
// below is expected to FAIL on this codebase as shipped, and that failure is
// real, not a fixture problem. Harbor's GET /api/v2.0/projects — the first
// call catalog_images() makes — returns HTTP 200 for ANY bearer (garbage,
// absent, or valid), filtered to whatever that identity can see (empty once
// no project is public). It never returns 401/403 from that endpoint, so
// HarborUnauthorized is never raised, catalog_available never flips to
// false, and a wrong identity silently gets catalog_available: true with
// zero catalog rows — indistinguishable from a legitimately empty
// catalogue. Harbor's per-project endpoints (e.g.
// GET /projects/<name>/repositories) DO correctly 401 a bad bearer, but
// catalog_images()'s loop never reaches them for a project the caller
// cannot see. This test is left asserting the INTENDED behaviour (matching
// the brief) rather than the observed one, precisely so it keeps failing
// until that gap is closed — weakening the assertion to match what the code
// currently does would be the exact failure mode this whole task exists to
// avoid. ***

const AUTH_STORAGE_KEY = 'kubevirt-ui-auth';

test.describe('Harbor identity enforcement (real Harbor, no mock)', () => {
  test('a request carrying the wrong identity is refused, not served', async () => {
    const res = await apiRequest('/api/v1/images', { token: 'not-a-real-token' });
    const body = await res.json();

    // Cluster rows may still be returned; the catalogue half must not be.
    expect(body.catalog_available).toBe(false);
    expect(
      (body.items ?? []).filter((i: { origin?: string }) => i.origin === 'catalog')
    ).toHaveLength(0);
  });

  test('a valid identity sees the catalogue', async ({ page }) => {
    await login(page);

    const raw = await page.evaluate(
      (key) => window.localStorage.getItem(key),
      AUTH_STORAGE_KEY
    );
    const accessToken: string | undefined = raw
      ? JSON.parse(raw)?.state?.accessToken
      : undefined;
    expect(accessToken, 'expected a real access token in localStorage after login()').toBeTruthy();

    const res = await apiRequest('/api/v1/images', { token: accessToken });
    const body = await res.json();

    expect(body.catalog_available).toBe(true);
  });
});
