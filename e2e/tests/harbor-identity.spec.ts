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
// *** WHAT THIS SPEC FOUND, AND WHAT HAPPENED TO IT ***
//
// This file used to carry, in capitals, the claim that the "wrong identity"
// test below was EXPECTED TO FAIL: Harbor's GET /api/v2.0/projects — the
// first call catalog_images() made — returns 200 for any bearer, so
// HarborUnauthorized was never raised and a wrong identity got
// `catalog_available: true` with zero rows, indistinguishable from a
// legitimately empty catalogue.
//
// The finding was real. It is also FIXED, in commit d12ca7f:
// HarborClient.verify_identity() probes the auth-gated GET /users/current
// (401/403 for an unrecognised bearer, 200 for a dex id_token, 412 for a
// robot) and catalog_images() calls it before enumerating anything. Leaving
// the old text in place would have left a prominent, false claim about the
// branch's current state sitting in the branch itself.
//
// The test below is nevertheless marked `fixme` — for a different and
// entirely mechanical reason, explained on it.

const AUTH_STORAGE_KEY = 'kubevirt-ui-auth';

test.describe('Harbor identity enforcement (real Harbor, no mock)', () => {
  // UNREACHABLE THROUGH THE HTTP API, which is why it is fixme rather than
  // deleted or weakened.
  //
  // A syntactically-invalid bearer never reaches Harbor at all: kubevirt-ui's
  // own `require_auth` (backend/app/core/auth.py) validates it against dex
  // and answers 401 before the images handler runs. So the response has no
  // body to read `catalog_available` off — it is `undefined`, and this
  // assertion fails permanently for a reason that has nothing to do with
  // Harbor identity.
  //
  // Proving the real claim needs a token that kubevirt-ui ACCEPTS and Harbor
  // does not — a genuine dex user who is not onboarded into Harbor, or is
  // onboarded with no project membership. That needs Harbor's OIDC bootstrap
  // to work against this compose stack, and it does not yet: dex's single
  // `issuer:` is pinned to the browser-facing LAN address for OAuth
  // redirects, while Harbor's OIDC client fetches discovery over the internal
  // compose hostname and rejects the mismatch (an anti-issuer-confusion check
  // in the OIDC spec, not a bug in either service). See
  // docker-compose.harbor-e2e.yml's harbor-init notes and task-9-report.md.
  //
  // Until a second, internally-consistent issuer exists for Harbor to use,
  // the unit-level cover for this claim is
  // backend/tests/test_the_harbor_client_never_uses_a_shared_identity.py
  // (verify_identity's 401/403/412/5xx handling) and
  // test_the_image_endpoint_wires_the_catalogue_and_the_callers_token.py
  // (a rejected identity degrades to catalog_available: false).
  test.fixme(
    'a real dex identity that Harbor does not recognise is refused the catalogue',
    async () => {
      // Needs: a dex-issued token for a user with no Harbor account.
      // `not-a-real-token` cannot stand in for one — require_auth rejects it
      // first, so this never exercises Harbor at all.
      const res = await apiRequest('/api/v1/images', { token: 'not-a-real-token' });
      const body = await res.json();

      // Cluster rows may still be returned; the catalogue half must not be.
      expect(body.catalog_available).toBe(false);
      expect(
        (body.items ?? []).filter((i: { origin?: string }) => i.origin === 'catalog')
      ).toHaveLength(0);
    }
  );

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
