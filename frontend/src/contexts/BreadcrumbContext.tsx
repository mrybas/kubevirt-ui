import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';

interface BreadcrumbContextValue {
  overrides: Record<string, string>;
  setOverride: (path: string, label: string) => void;
  clearOverride: (path: string) => void;
}

const BreadcrumbContext = createContext<BreadcrumbContextValue>({
  overrides: {},
  setOverride: () => {},
  clearOverride: () => {},
});

export function BreadcrumbProvider({ children }: { children: ReactNode }) {
  const [overrides, setOverrides] = useState<Record<string, string>>({});

  const setOverride = useCallback((path: string, label: string) => {
    setOverrides((prev) => ({ ...prev, [path]: label }));
  }, []);

  const clearOverride = useCallback((path: string) => {
    setOverrides((prev) => {
      const next = { ...prev };
      delete next[path];
      return next;
    });
  }, []);

  return (
    <BreadcrumbContext.Provider value={{ overrides, setOverride, clearOverride }}>
      {children}
    </BreadcrumbContext.Provider>
  );
}

/**
 * Register a display-name override for a specific URL path in the breadcrumb trail.
 * Automatically clears when the component unmounts.
 *
 * Usage (call unconditionally at the top of the component, before any early returns):
 *   useBreadcrumbOverride(vm ? `/vms/${vm.namespace}/${vm.name}` : undefined, vm?.display_name || vm?.name);
 */
export function useBreadcrumbOverride(
  path: string | undefined,
  label: string | undefined,
) {
  const { setOverride, clearOverride } = useContext(BreadcrumbContext);

  useEffect(() => {
    if (!path || !label) return;
    setOverride(path, label);
    return () => clearOverride(path);
  }, [path, label, setOverride, clearOverride]);
}

export function useBreadcrumbContext() {
  return useContext(BreadcrumbContext);
}
