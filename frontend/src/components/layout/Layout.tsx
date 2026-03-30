import { ReactNode } from 'react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import ErrorBoundary from '../common/ErrorBoundary';
import { Breadcrumbs } from '../common/Breadcrumbs';

interface LayoutProps {
  children: ReactNode;
}

export function Layout({ children }: LayoutProps) {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-auto p-3 sm:p-6">
          <div className="mx-auto max-w-7xl animate-fade-in">
            <Breadcrumbs />
            <ErrorBoundary>{children}</ErrorBoundary>
          </div>
        </main>
      </div>
    </div>
  );
}
