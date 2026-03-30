import { Link, useLocation } from 'react-router-dom';

const LABELS: Record<string, string> = {
  dashboard: 'Dashboard',
  vms: 'Virtual Machines',
  templates: 'Templates',
  storage: 'Storage',
  images: 'Images',
  classes: 'Classes',
  network: 'Networks',
  'egress-gateways': 'Egress Gateways',
  'security-groups': 'Security Groups',
  cluster: 'Cluster',
  projects: 'Projects',
  folders: 'Folders',
  tenants: 'Tenants',
  users: 'Users',
  groups: 'Groups',
  profile: 'Profile',
  'cli-access': 'CLI Access',
};

function toLabel(segment: string): string {
  return LABELS[segment] ?? segment.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function Breadcrumbs() {
  const location = useLocation();
  const segments = location.pathname.split('/').filter(Boolean);

  if (segments.length <= 1) return null;

  const crumbs = segments.map((seg, i) => ({
    label: toLabel(seg),
    href: '/' + segments.slice(0, i + 1).join('/'),
    isLast: i === segments.length - 1,
  }));

  return (
    <nav className="flex items-center gap-1.5 text-sm mb-4" aria-label="Breadcrumb">
      {crumbs.map((crumb, i) => (
        <span key={crumb.href} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-surface-600 select-none">›</span>}
          {crumb.isLast ? (
            <span className="text-surface-100">{crumb.label}</span>
          ) : (
            <Link to={crumb.href} className="text-surface-400 hover:text-surface-200 transition-colors">
              {crumb.label}
            </Link>
          )}
        </span>
      ))}
    </nav>
  );
}
