import type { ReactNode } from 'react';

interface PageTitleProps {
  title: string;
  subtitle?: string;
  children?: ReactNode; // action buttons (right-aligned)
}

export function PageTitle({ title, subtitle, children }: PageTitleProps) {
  return (
    <div className="flex flex-col sm:flex-row justify-between sm:items-center gap-3">
      <div className="min-w-0">
        <h1 className="text-xl sm:text-2xl font-bold text-surface-100 truncate">{title}</h1>
        {subtitle && <p className="text-xs sm:text-sm text-surface-400 mt-1">{subtitle}</p>}
      </div>
      {children && <div className="flex items-center gap-2 sm:gap-3 flex-wrap shrink-0">{children}</div>}
    </div>
  );
}
