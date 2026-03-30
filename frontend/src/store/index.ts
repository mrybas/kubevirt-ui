import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface AppState {
  selectedNamespace: string;
  setSelectedNamespace: (namespace: string) => void;
  theme: 'dark' | 'light';
  setTheme: (theme: 'dark' | 'light') => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      selectedNamespace: '',
      setSelectedNamespace: (namespace) => set({ selectedNamespace: namespace }),
      theme: 'dark',
      setTheme: (theme) => set({ theme }),
    }),
    {
      name: 'kubevirt-ui-storage',
    }
  )
);
