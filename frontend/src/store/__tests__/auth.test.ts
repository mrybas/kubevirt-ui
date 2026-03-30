import { describe, it, expect, beforeEach } from 'vitest';
import { useAuthStore } from '../auth';

describe('useAuthStore', () => {
  beforeEach(() => {
    // Reset to initial state
    useAuthStore.setState({
      isAuthenticated: false,
      isLoading: true,
      user: null,
      accessToken: null,
      refreshToken: null,
      idToken: null,
      config: null,
    });
  });

  it('has correct initial state', () => {
    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(false);
    expect(state.isLoading).toBe(true);
    expect(state.user).toBeNull();
    expect(state.accessToken).toBeNull();
  });

  it('setTokens sets tokens and marks authenticated', () => {
    useAuthStore.getState().setTokens('access-1', 'refresh-1', 'id-1');

    const state = useAuthStore.getState();
    expect(state.accessToken).toBe('access-1');
    expect(state.refreshToken).toBe('refresh-1');
    expect(state.idToken).toBe('id-1');
    expect(state.isAuthenticated).toBe(true);
  });

  it('setTokens without optional tokens sets them to null', () => {
    useAuthStore.getState().setTokens('access-only');

    const state = useAuthStore.getState();
    expect(state.accessToken).toBe('access-only');
    expect(state.refreshToken).toBeNull();
    expect(state.idToken).toBeNull();
    expect(state.isAuthenticated).toBe(true);
  });

  it('setUser updates user info', () => {
    const user = { id: '123', email: 'test@example.com', username: 'testuser', groups: [] };
    useAuthStore.getState().setUser(user);

    expect(useAuthStore.getState().user).toEqual(user);
  });

  it('logout clears auth state', () => {
    useAuthStore.getState().setTokens('tok', 'ref', 'id');
    useAuthStore.getState().setUser({ id: '1', email: 'a@b.c', username: 'a', groups: [] });

    useAuthStore.getState().logout();

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(false);
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.idToken).toBeNull();
    expect(state.user).toBeNull();
  });

  it('setLoading updates loading state', () => {
    useAuthStore.getState().setLoading(false);
    expect(useAuthStore.getState().isLoading).toBe(false);

    useAuthStore.getState().setLoading(true);
    expect(useAuthStore.getState().isLoading).toBe(true);
  });
});
