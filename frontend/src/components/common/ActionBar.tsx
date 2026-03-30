import type { ReactNode } from 'react';

interface ActionBarProps {
  title: string;
  subtitle?: string;
  children?: ReactNode;   // buttons (right-aligned)
  filters?: ReactNode;    // second row: search/filters (optional)
}

export function ActionBar({ title, subtitle, children, filters }: ActionBarProps) {
  return (
    <div className="space-y-3">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-surface-100">{title}</h1>
          {subtitle && <p className="text-sm text-surface-400 mt-1">{subtitle}</p>}
        </div>
        {children && <div className="flex items-center gap-3">{children}</div>}
      </div>
      {filters && <div className="flex items-center gap-3">{filters}</div>}
    </div>
  );
}
