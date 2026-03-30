import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { AuthConfig, UserInfo } from '../types/auth';

interface AuthStore {
  // State
  isAuthenticated: boolean;
  isLoading: boolean;
  user: UserInfo | null;
  accessToken: string | null;
  refreshToken: string | null;
  idToken: string | null;
  config: AuthConfig | null;

  // Actions
  setConfig: (config: AuthConfig) => void;
  setTokens: (accessToken: string, refreshToken?: string, idToken?: string) => void;
  setUser: (user: UserInfo) => void;
  setLoading: (loading: boolean) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthStore>()(
  persist(
    (set) => ({
      isAuthenticated: false,
      isLoading: true,
      user: null,
      accessToken: null,
      refreshToken: null,
      idToken: null,
      config: null,

      setConfig: (config) => set({ config }),

      setTokens: (accessToken, refreshToken, idToken) =>
        set({
          accessToken,
          refreshToken: refreshToken ?? null,
          idToken: idToken ?? null,
          isAuthenticated: true,
        }),

      setUser: (user) => set({ user }),

      setLoading: (isLoading) => set({ isLoading }),

      logout: () =>
        set({
          isAuthenticated: false,
          user: null,
          accessToken: null,
          refreshToken: null,
          idToken: null,
        }),
    }),
    {
      name: 'kubevirt-ui-auth',
      partialize: (state) => ({
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        idToken: state.idToken,
        user: state.user,
        isAuthenticated: state.isAuthenticated,
      }),
    }
  )
);
