import { defineConfig } from '@playwright/test';

import base from './playwright.config';

/**
 * The Harbor spec, and only the Harbor spec.
 *
 * Run by `make test-e2e-harbor`, which composes docker-compose.harbor-e2e.yml
 * on top of the ordinary overlay. That overlay is what brings up Harbor and
 * turns HARBOR_IMAGE_ENABLED on — for this run alone, because it sets that
 * flag on the shared backend service and every other spec would otherwise
 * inherit it.
 */
export default defineConfig({
  ...base,
  testIgnore: undefined,
  testMatch: '**/harbor-identity.spec.ts',
});
