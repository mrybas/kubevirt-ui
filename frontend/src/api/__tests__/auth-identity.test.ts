/**
 * With auth disabled there is no login flow, so the app used to bail out of
 * initialisation before ever setting a user. `user` stayed null, and every
 * `user?.is_admin` check in the app then read as "not an admin" — which is how
 * the Create Tenant wizard came to report "No folders available. Ask an admin
 * to create one." on a cluster whose API listed a folder just fine.
 *
 * The identity has to come from the backend, which answers /auth/me without a
 * token in that mode.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import { getCurrentUser } from '../auth';

const ANONYMOUS_ADMIN = {
  id: 'anonymous',
  email: 'anonymous@local',
  username: 'anonymous',
  groups: ['kubevirt-ui-admins'],
  is_admin: true,
};

describe('getCurrentUser', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ANONYMOUS_ADMIN,
      text: async () => JSON.stringify(ANONYMOUS_ADMIN),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function headersOf(call: unknown[]): Record<string, string> {
    return ((call[1] as RequestInit | undefined)?.headers ?? {}) as Record<string, string>;
  }

  it('works without a token, so auth-none can learn who it is', async () => {
    const user = await getCurrentUser();

    expect(user.is_admin).toBe(true);
    expect(headersOf(fetchMock.mock.calls[0])).not.toHaveProperty('Authorization');
  });

  it('still sends the token when there is one', async () => {
    await getCurrentUser('tok-123');

    expect(headersOf(fetchMock.mock.calls[0])).toMatchObject({
      Authorization: 'Bearer tok-123',
    });
  });

  it('asks the backend rather than assuming an identity', async () => {
    await getCurrentUser();

    expect(String(fetchMock.mock.calls[0][0])).toContain('/auth/me');
  });
});
