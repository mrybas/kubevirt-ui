import { useState, useEffect, useCallback, createContext, useContext } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import {
  LayoutDashboard,
  Server,
  HardDrive,
  Network,
  Box,
  Folder,
  FolderOpen,
  ChevronDown,
  ChevronRight,
  ChevronLeft,
  LayoutTemplate,
  Globe,
  Layers,
  Lock,
  User,
  Users,
  Shield,
  Terminal,
  Plus,
  GitMerge,
  Activity,
  ShieldCheck,
  Radio,
  Archive,
} from 'lucide-react';
import clsx from 'clsx';
import { FolderTree } from '../folders/FolderTree';
import { useFoldersTree } from '../../hooks/useFolders';
import { useFeatures } from '../../hooks/useFeatures';

const SidebarCollapsedContext = createContext(false);

function useSidebarCollapsed() {
  return useContext(SidebarCollapsedContext);
}

function getInitialCollapsed(): boolean {
  try {
    return localStorage.getItem('sidebar-collapsed') === 'true';
  } catch {
    return false;
  }
}

interface NavItem {
  name: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  children?: NavItem[];
  end?: boolean; // For exact path matching
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/dashboard', icon: LayoutDashboard },
  {
    name: 'Virtual Machines',
    href: '/vms',
    icon: Server,
    children: [
      { name: 'All VMs', href: '/vms', icon: Server, end: true },
      { name: 'Templates', href: '/vms/templates', icon: LayoutTemplate },
    ],
  },
  {
    name: 'Storage',
    href: '/storage',
    icon: HardDrive,
    children: [
      { name: 'Images', href: '/storage/images', icon: HardDrive },
      { name: 'Classes', href: '/storage/classes', icon: Server },
    ],
  },
  {
    name: 'Network',
    href: '/network',
    icon: Network,
    children: [
      { name: 'Networks', href: '/network', icon: Globe, end: true },
      { name: 'Egress Gateways', href: '/network/egress-gateways', icon: Globe },
      { name: 'OVN Gateways', href: '/network/ovn-gateways', icon: GitMerge },
      { name: 'BGP Peering', href: '/network/bgp', icon: Radio },
    ],
  },
  {
    name: 'Security',
    href: '/security',
    icon: ShieldCheck,
    children: [
      { name: 'Security Groups', href: '/network/security-groups', icon: Shield },
      { name: 'Cilium Policies', href: '/security/cilium-policies', icon: Lock },
      { name: 'Security Baseline', href: '/security/baseline', icon: ShieldCheck },
      { name: 'Network Flows', href: '/security/network-flows', icon: Activity },
    ],
  },
  { name: 'Backups', href: '/backups', icon: Archive },
  { name: 'Cluster', href: '/cluster', icon: Box },
];

const adminNavigation: NavItem[] = [
  { name: 'Tenants', href: '/tenants', icon: Layers },
  {
    name: 'Users',
    href: '/users',
    icon: Users,
    children: [
      { name: 'All Users', href: '/users', icon: Users, end: true },
      { name: 'Groups', href: '/users/groups', icon: Shield },
    ],
  },
];

function NavItemComponent({ item, level: _level = 0 }: { item: NavItem; level?: number }) {
  const location = useLocation();
  const collapsed = useSidebarCollapsed();
  const [isExpanded, setIsExpanded] = useState(() => {
    if (item.children) {
      return item.children.some(child => location.pathname === child.href) ||
             location.pathname.startsWith(item.href);
    }
    return false;
  });

  const hasChildren = item.children && item.children.length > 0;
  const isParentActive = hasChildren && (
    item.children!.some(child => location.pathname === child.href) ||
    location.pathname.startsWith(item.href)
  );

  if (hasChildren) {
    return (
      <div>
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          title={collapsed ? item.name : undefined}
          className={clsx(
            'sidebar-link w-full',
            collapsed ? 'justify-center' : 'justify-between',
            isParentActive && 'text-primary-400'
          )}
        >
          <div className="flex items-center gap-2.5">
            <item.icon className="h-4.5 w-4.5 shrink-0" />
            {!collapsed && <span>{item.name}</span>}
          </div>
          {!collapsed && (isExpanded ? (
            <ChevronDown className="h-4 w-4 text-surface-500" />
          ) : (
            <ChevronRight className="h-4 w-4 text-surface-500" />
          ))}
        </button>
        {!collapsed && isExpanded && (
          <div className="ml-5 mt-0.5 space-y-0.5 border-l border-surface-700/50 pl-2.5">
            {item.children!.map((child) => (
              <NavLink
                key={child.href}
                to={child.href}
                end={child.end}
                className={({ isActive }) =>
                  clsx(
                    'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm transition-colors',
                    isActive
                      ? 'bg-primary-500/10 text-primary-400'
                      : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200'
                  )
                }
              >
                <child.icon className="h-3.5 w-3.5" />
                <span>{child.name}</span>
              </NavLink>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <NavLink
      to={item.href}
      end={item.end}
      title={collapsed ? item.name : undefined}
      className={({ isActive }) =>
        clsx('sidebar-link', collapsed && 'justify-center', isActive && 'active')
      }
    >
      <item.icon className="h-4.5 w-4.5 shrink-0" />
      {!collapsed && <span>{item.name}</span>}
    </NavLink>
  );
}

function FoldersSidebarSection() {
  const location = useLocation();
  const collapsed = useSidebarCollapsed();
  const { data: treeData, isLoading } = useFoldersTree();
  const isInFolders = location.pathname.startsWith('/folders');
  const [expanded, setExpanded] = useState(isInFolders);

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        title={collapsed ? 'Folders' : undefined}
        className={clsx(
          'sidebar-link w-full',
          collapsed ? 'justify-center' : 'justify-between',
          isInFolders && 'text-primary-400'
        )}
      >
        <div className="flex items-center gap-2.5">
          {isInFolders ? (
            <FolderOpen className="h-4.5 w-4.5 shrink-0" />
          ) : (
            <Folder className="h-4.5 w-4.5 shrink-0" />
          )}
          {!collapsed && <span>Folders</span>}
        </div>
        {!collapsed && (
          <div className="flex items-center gap-1">
            <NavLink
              to="/folders/new"
              onClick={(e) => e.stopPropagation()}
              className="p-0.5 rounded hover:bg-surface-700 text-surface-500 hover:text-surface-300 transition-colors"
              title="Create Folder"
            >
              <Plus className="h-3 w-3" />
            </NavLink>
            {expanded ? (
              <ChevronDown className="h-4 w-4 text-surface-500" />
            ) : (
              <ChevronRight className="h-4 w-4 text-surface-500" />
            )}
          </div>
        )}
      </button>

      {!collapsed && expanded && (
        <div className="ml-3 mt-0.5 pl-2.5 border-l border-surface-700/50">
          <FolderTree folders={treeData?.items ?? []} isLoading={isLoading} />
          <NavLink
            to="/folders"
            end
            className={({ isActive }) =>
              clsx(
                'flex items-center gap-2 rounded-md px-2 py-1 text-xs mt-1 transition-colors',
                isActive
                  ? 'text-primary-400'
                  : 'text-surface-600 hover:text-surface-400'
              )
            }
          >
            All Folders
          </NavLink>
        </div>
      )}
    </div>
  );
}

export function Sidebar() {
  const { data: features } = useFeatures();
  const [collapsed, setCollapsed] = useState(() => {
    // Auto-collapse on mobile regardless of localStorage
    if (typeof window !== 'undefined' && window.innerWidth < 768) return true;
    return getInitialCollapsed();
  });
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < 768
  );
  const location = useLocation();

  // Track viewport size
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 767px)');
    const handler = (e: MediaQueryListEvent) => {
      setIsMobile(e.matches);
      if (e.matches && !collapsed) setCollapsed(true);
    };
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [collapsed]);

  // Close sidebar on navigation (mobile only)
  useEffect(() => {
    if (isMobile && !collapsed) {
      setCollapsed(true);
    }
  }, [location.pathname]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = useCallback(() => {
    const next = !collapsed;
    setCollapsed(next);
    // Only persist preference on desktop
    if (!isMobile) {
      try { localStorage.setItem('sidebar-collapsed', String(next)); } catch {}
    }
  }, [collapsed, isMobile]);

  // Listen for toggle events from Header hamburger button
  useEffect(() => {
    const handler = () => toggle();
    window.addEventListener(SIDEBAR_TOGGLE_EVENT, handler);
    return () => window.removeEventListener(SIDEBAR_TOGGLE_EVENT, handler);
  }, [toggle]);

  return (
    <SidebarCollapsedContext.Provider value={collapsed}>
      {/* Backdrop overlay for mobile */}
      {isMobile && !collapsed && (
        <div
          className="fixed inset-0 bg-black/50 z-40 transition-opacity duration-200"
          onClick={() => setCollapsed(true)}
          aria-hidden="true"
        />
      )}
      <aside
        className={clsx(
          'flex flex-col border-r border-surface-800 bg-surface-950 transition-all duration-200',
          collapsed ? 'w-16' : 'w-60',
          isMobile
            ? clsx(
                'fixed inset-y-0 left-0 z-50',
                collapsed && '-translate-x-full'
              )
            : 'relative'
        )}
      >
        {/* Logo + toggle */}
        <div className="flex h-16 items-center border-b border-surface-800 px-3 gap-2">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-primary-500 to-primary-700 shadow-lg shadow-primary-500/25 shrink-0">
            <Server className="h-5 w-5 text-white" />
          </div>
          {!collapsed && (
            <div className="flex-1 min-w-0">
              <h1 className="font-display text-lg font-bold text-surface-100 truncate">
                KubeVirt
              </h1>
              <p className="text-xs text-surface-500">UI Console</p>
            </div>
          )}
          <button
            onClick={toggle}
            className="p-1.5 rounded-md text-surface-500 hover:text-surface-200 hover:bg-surface-800 transition-colors shrink-0"
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={!collapsed}
          >
            {collapsed ? (
              <ChevronRight className="h-4 w-4" />
            ) : (
              <ChevronLeft className="h-4 w-4" />
            )}
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto space-y-1 p-2">
          {navigation.map((item) => (
            <NavItemComponent key={item.href} item={item} />
          ))}

          {/* Folders Section */}
          {!collapsed && (
            <div className="pt-2">
              <FoldersSidebarSection />
            </div>
          )}

          {/* Admin Section */}
          <div className={clsx('pt-4 mt-4 border-t border-surface-800')}>
            {!collapsed && (
              <p className="px-3 py-2 text-xs font-semibold text-surface-500 uppercase tracking-wider">
                Admin
              </p>
            )}
            {adminNavigation
              .filter((item) => item.href !== '/tenants' || features?.enableTenants)
              .map((item) => (
                <NavItemComponent key={item.href} item={item} />
              ))}
          </div>
        </nav>

        {/* Footer */}
        <div className="border-t border-surface-800 p-2 space-y-1">
          <NavLink
            to="/profile"
            title={collapsed ? 'Profile' : undefined}
            className={({ isActive }) =>
              clsx('sidebar-link', collapsed && 'justify-center', isActive && 'active')
            }
          >
            <User className="h-4.5 w-4.5 shrink-0" />
            {!collapsed && <span>Profile</span>}
          </NavLink>
          <NavLink
            to="/cli-access"
            title={collapsed ? 'CLI Access' : undefined}
            className={({ isActive }) =>
              clsx('sidebar-link', collapsed && 'justify-center', isActive && 'active')
            }
          >
            <Terminal className="h-4.5 w-4.5 shrink-0" />
            {!collapsed && <span>CLI Access</span>}
          </NavLink>
          {!collapsed && (
            <div className="mt-3 px-3 text-xs text-surface-500">
              <p>KubeVirt UI v0.1.0</p>
            </div>
          )}
        </div>
      </aside>
    </SidebarCollapsedContext.Provider>
  );
}

// Custom event for mobile sidebar toggle (used by Header hamburger button)
export const SIDEBAR_TOGGLE_EVENT = 'sidebar-toggle';

export function triggerSidebarToggle() {
  window.dispatchEvent(new CustomEvent(SIDEBAR_TOGGLE_EVENT));
}

export function useSidebarWidth() {
  try {
    if (typeof window !== 'undefined' && window.innerWidth < 768) return 0;
    return localStorage.getItem('sidebar-collapsed') === 'true' ? 64 : 240;
  } catch {
    return 240;
  }
}
