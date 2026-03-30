import { useState, useRef, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { User, LogOut, ChevronDown, Menu } from 'lucide-react';
import { useNamespaces } from '@/hooks/useNamespaces';
import { useProjects } from '@/hooks/useProjects';
import { useAppStore } from '@/store';
import { useAuthStore } from '@/store/auth';
import { CustomSelect } from '@/components/common/CustomSelect';
import type { SelectOption } from '@/components/common/CustomSelect';
import { triggerSidebarToggle } from './Sidebar';

const pageTitles: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/vms': 'Virtual Machines',
  '/storage': 'Storage',
  '/network': 'Network',
  '/cluster': 'Cluster',
  '/settings': 'Settings',
  '/projects': 'Projects',
};

export function Header() {
  const location = useLocation();
  const navigate = useNavigate();
  const { data: namespaces } = useNamespaces();
  const { data: projectsData } = useProjects();
  const { selectedNamespace, setSelectedNamespace } = useAppStore();
  const { user, logout, config } = useAuthStore();
  const [showUserMenu, setShowUserMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const pageTitle = pageTitles[location.pathname] ?? 'KubeVirt UI';

  // Close menu when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setShowUserMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const displayName = user?.username || user?.email || 'User';
  const isAdmin = user?.groups?.includes('kubevirt-ui-admins');
  return (
    <header className="flex h-16 items-center justify-between border-b border-surface-800 bg-surface-950/50 px-3 md:px-6 backdrop-blur-sm">
      <div className="flex items-center gap-2 md:gap-4">
        {/* Hamburger menu for mobile */}
        <button
          onClick={triggerSidebarToggle}
          className="md:hidden p-1.5 rounded-md text-surface-400 hover:text-surface-200 hover:bg-surface-800 transition-colors"
          aria-label="Toggle sidebar"
        >
          <Menu className="h-5 w-5" />
        </button>
        <h2 className="font-display text-lg md:text-xl font-semibold text-surface-100 truncate">{pageTitle}</h2>
      </div>

      <div className="flex items-center gap-2 md:gap-4 min-w-0">
        {/* Namespace selector (grouped by project) — hidden on very small screens */}
        <CustomSelect
          value={selectedNamespace}
          onChange={setSelectedNamespace}
          className="hidden sm:block w-40 md:w-52"
          placeholder="All namespaces"
          options={(() => {
            const opts: SelectOption[] = [{ value: '', label: 'All namespaces' }];
            if (projectsData && projectsData.items.length > 0) {
              for (const project of projectsData.items) {
                for (const env of project.environments) {
                  opts.push({ value: env.name, label: `${env.environment} (${env.name})`, group: project.display_name });
                }
              }
            } else if (namespaces?.items) {
              for (const ns of namespaces.items) {
                opts.push({ value: ns.name, label: ns.name });
              }
            }
            return opts;
          })()}
        />

        {/* User menu */}
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setShowUserMenu(!showUserMenu)}
            className="flex items-center gap-2 rounded-lg bg-surface-800 px-2 md:px-3 py-2 hover:bg-surface-700 transition-colors"
          >
            <User className="h-5 w-5 text-surface-400 shrink-0" />
            <span className="hidden md:inline text-sm font-medium">{displayName}</span>
            {isAdmin && (
              <span className="hidden sm:inline text-xs px-1.5 py-0.5 bg-primary-500/20 text-primary-400 rounded">
                Admin
              </span>
            )}
            <ChevronDown className="h-4 w-4 text-surface-500 shrink-0" />
          </button>

          {/* Dropdown menu */}
          {showUserMenu && (
            <div className="absolute right-0 top-full mt-2 w-64 bg-surface-800 border border-surface-700 rounded-lg shadow-xl z-50">
              <div className="p-3 border-b border-surface-700">
                <p className="font-medium text-surface-100">{displayName}</p>
                <p className="text-sm text-surface-400">{user?.email}</p>
                {user?.groups && user.groups.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {user.groups.slice(0, 3).map((group) => (
                      <span
                        key={group}
                        className="text-xs px-2 py-0.5 bg-surface-700 text-surface-400 rounded"
                      >
                        {group}
                      </span>
                    ))}
                    {user.groups.length > 3 && (
                      <span className="text-xs text-surface-500">
                        +{user.groups.length - 3} more
                      </span>
                    )}
                  </div>
                )}
              </div>

              {config?.type !== 'none' && (
                <div className="p-2">
                  <button
                    onClick={handleLogout}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-surface-300 hover:bg-surface-700 rounded-lg transition-colors"
                  >
                    <LogOut className="h-4 w-4" />
                    Sign out
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
