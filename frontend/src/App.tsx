import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { useEffect, lazy, Suspense } from 'react';
import { Layout } from './components/layout/Layout';
import { Login } from './pages/Login';
import { AuthCallback } from './pages/AuthCallback';
import { Notifications } from './components/common/Notifications';
import ErrorBoundary from './components/common/ErrorBoundary';
import { useAuthStore } from './store/auth';
import { getAuthConfig, getCurrentUser } from './api/auth';
import { useFeatures } from './hooks/useFeatures';
import { Loader2 } from 'lucide-react';

const Dashboard = lazy(() => import('./pages/Dashboard').then(m => ({ default: m.Dashboard })));
const VirtualMachines = lazy(() => import('./pages/VirtualMachines').then(m => ({ default: m.VirtualMachines })));
const VMTemplates = lazy(() => import('./pages/VMTemplates').then(m => ({ default: m.VMTemplates })));
const VMDetail = lazy(() => import('./pages/VMDetail').then(m => ({ default: m.VMDetail })));
const Storage = lazy(() => import('./pages/Storage').then(m => ({ default: m.Storage })));
const StorageClasses = lazy(() => import('./pages/StorageClasses').then(m => ({ default: m.StorageClasses })));
const ImageDetail = lazy(() => import('./pages/ImageDetail').then(m => ({ default: m.ImageDetail })));
const Networks = lazy(() => import('./pages/Networks').then(m => ({ default: m.Networks })));
const NetworkDetail = lazy(() => import('./pages/NetworkDetail').then(m => ({ default: m.NetworkDetail })));
const Cluster = lazy(() => import('./pages/Cluster').then(m => ({ default: m.Cluster })));
const Projects = lazy(() => import('./pages/Projects'));
const Folders = lazy(() => import('./pages/Folders'));
const FolderDetail = lazy(() => import('./pages/FolderDetail'));
const VPCDetail = lazy(() => import('./pages/VPCDetail'));
const EgressGateways = lazy(() => import('./pages/EgressGateways'));
const OvnGateways = lazy(() => import('./pages/OvnGateways'));
const BgpPeering = lazy(() => import('./pages/BgpPeering'));
const SecurityGroups = lazy(() => import('./pages/SecurityGroups'));
const SecurityGroupDetail = lazy(() => import('./pages/SecurityGroupDetail'));
const NetworkFlows = lazy(() => import('./pages/NetworkFlows'));
const CiliumPolicies = lazy(() => import('./pages/CiliumPolicies'));
const SecurityBaseline = lazy(() => import('./pages/SecurityBaseline'));
const Backups = lazy(() => import('./pages/Backups'));
const Tenants = lazy(() => import('./pages/Tenants'));
const TenantDetail = lazy(() => import('./pages/TenantDetail'));
const Users = lazy(() => import('./pages/Users'));
const Groups = lazy(() => import('./pages/Groups'));
const Profile = lazy(() => import('./pages/Profile'));
const CLIAccess = lazy(() => import('./pages/CLIAccess'));
const NotFound = lazy(() => import('./pages/NotFound'));
const AccessDenied = lazy(() => import('./pages/AccessDenied'));

// Protected route wrapper
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const { isAuthenticated, isLoading, config } = useAuthStore();

  // If auth is disabled (type=none), allow access
  if (config?.type === 'none') {
    return <>{children}</>;
  }

  // Show loading while checking auth
  if (isLoading) {
    return (
      <div className="min-h-screen bg-surface-900 flex items-center justify-center">
        <Loader2 className="h-8 w-8 text-primary-400 animate-spin" />
      </div>
    );
  }

  // Redirect to login if not authenticated
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}

// Admin-only route guard
/**
 * Why the network pages are closed to a folder admin.
 *
 * Reported in UAT run 4 (R-3) as a bug, because "Access Denied" with no
 * reason reads like one: the backend lists VPCs for these users happily, and
 * the page is shut. It is deliberate — a VPC carries BGP announcements,
 * address pools and routes that reach the border router — and a boundary
 * that says why is a boundary nobody files twice.
 */
const NETWORKS_ARE_ADMIN_ONLY =
  'Networks are managed by platform admins. A VPC carries BGP announcements, ' +
  'address pools and routes that reach the border router — one wrong prefix ' +
  'takes traffic that is not yours with it — so creating and changing them is ' +
  'not delegated to folder admins. Ask a platform admin for the network you ' +
  'need; everything inside it is yours.';

function RequireAdmin(
  { children, reason }: { children: React.ReactNode; reason?: string },
) {
  const { user, config, isLoading } = useAuthStore();

  // No auth configured → behave as admin (dev mode)
  if (config?.type === 'none') return <>{children}</>;

  // Still resolving auth state — render nothing to avoid flash
  if (isLoading) return null;

  if (!user?.is_admin) {
    return (
      <Suspense fallback={null}>
        <AccessDenied reason={reason} />
      </Suspense>
    );
  }

  return <>{children}</>;
}

// Auth initializer
function AuthInitializer({ children }: { children: React.ReactNode }) {
  const { setConfig, setUser, setLoading, accessToken, isAuthenticated } = useAuthStore();

  useEffect(() => {
    async function initAuth() {
      try {
        // Load auth config
        const config = await getAuthConfig();
        setConfig(config);

        // With auth disabled there is no login to learn the identity from, so
        // ask the backend directly. Skipping this leaves `user` null forever,
        // and every `user?.is_admin` check in the app then reads as "not an
        // admin" — which is how the tenant wizard came to report "No folders
        // available" on a cluster that had one.
        if (config.type === 'none') {
          try {
            setUser(await getCurrentUser());
          } catch (e) {
            console.error('Failed to load the anonymous identity:', e);
          }
          setLoading(false);
          return;
        }

        // If we have a stored token, validate it
        if (accessToken && isAuthenticated) {
          try {
            const user = await getCurrentUser(accessToken);
            setUser(user);
          } catch {
            // Token invalid, clear auth state
            useAuthStore.getState().logout();
          }
        }
      } catch (e) {
        console.error('Failed to initialize auth:', e);
      } finally {
        setLoading(false);
      }
    }

    initAuth();
  }, []);

  return <>{children}</>;
}

function AppRoutes() {
  const { data: features, isLoading: featuresLoading } = useFeatures();

  return (
    <Routes>
      {/* Public routes */}
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />

      {/* Protected routes */}
      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <Layout>
              <ErrorBoundary>
                <Suspense fallback={<div className="flex items-center justify-center min-h-[60vh]"><Loader2 className="h-8 w-8 text-primary-400 animate-spin" /></div>}>
                  <Routes>
                    <Route path="/" element={<Navigate to="/dashboard" replace />} />
                    <Route path="/dashboard" element={<Dashboard />} />
                    {/* Virtual Machines */}
                    <Route path="/vms" element={<VirtualMachines />} />
                    <Route path="/vms/templates" element={<VMTemplates />} />
                    <Route path="/vms/:namespace/:name" element={<VMDetail />} />
                    {/* Storage */}
                    <Route path="/storage" element={<Navigate to="/storage/images" replace />} />
                    <Route path="/storage/images" element={<Storage />} />
                    <Route path="/storage/classes" element={<RequireAdmin><StorageClasses /></RequireAdmin>} />
                    <Route path="/storage/:namespace/:name" element={<ImageDetail />} />
                    {/* Network — admin only */}
                    <Route path="/network" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Networks /></RequireAdmin>} />
                    {/* The breadcrumb reads "Networking" while every route is
                        /network/*, so the plural is a natural thing to type
                        (and to paste from a doc) — it used to land on a 404
                        that still drew the breadcrumb. */}
                    <Route path="/networking/*" element={<Navigate to="/network" replace />} />
                    <Route path="/network/vpcs" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Navigate to="/network?tab=vpcs" replace /></RequireAdmin>} />
                    <Route path="/network/subnets" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Navigate to="/network?tab=subnets" replace /></RequireAdmin>} />
                    <Route path="/network/underlay" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Navigate to="/network?tab=underlay" replace /></RequireAdmin>} />
                    <Route path="/network/system" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Navigate to="/network?tab=system" replace /></RequireAdmin>} />
                    <Route path="/network/subnets/create" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Navigate to="/network?tab=subnets&create=true" replace /></RequireAdmin>} />
                    <Route path="/network/subnets/:name" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><NetworkDetail /></RequireAdmin>} />
                    <Route path="/network/vpcs/create" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><Navigate to="/network?tab=vpcs&create=true" replace /></RequireAdmin>} />
                    <Route path="/network/vpcs/:name" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><VPCDetail /></RequireAdmin>} />
                    <Route path="/network/egress-gateways" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><EgressGateways /></RequireAdmin>} />
                    <Route path="/network/ovn-gateways" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><OvnGateways /></RequireAdmin>} />
                    <Route path="/network/bgp" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><BgpPeering /></RequireAdmin>} />
                    <Route path="/network/security-groups" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><SecurityGroups /></RequireAdmin>} />
                    <Route path="/network/security-groups/:name" element={<RequireAdmin reason={NETWORKS_ARE_ADMIN_ONLY}><SecurityGroupDetail /></RequireAdmin>} />
                    {/* Backups — admin only */}
                    <Route path="/backups" element={<RequireAdmin><Backups /></RequireAdmin>} />
                    {/* Security — admin only */}
                    <Route path="/security/network-flows" element={<RequireAdmin><NetworkFlows /></RequireAdmin>} />
                    <Route path="/security/cilium-policies" element={<RequireAdmin><CiliumPolicies /></RequireAdmin>} />
                    <Route path="/security/baseline" element={<RequireAdmin><SecurityBaseline /></RequireAdmin>} />
                    {/* Cluster — admin only */}
                    <Route path="/cluster" element={<RequireAdmin><Cluster /></RequireAdmin>} />
                    {/* Other */}
                    <Route path="/projects" element={<Projects />} />
                    <Route path="/folders" element={<Folders />} />
                    <Route path="/folders/new" element={<Navigate to="/folders?create=true" replace />} />
                    <Route path="/folders/:name" element={<FolderDetail />} />
                    {/* Tenants — only when the feature is enabled. NOT admin
                        only: the list endpoint scopes itself to folders the
                        caller may at least view, and creating one is answered
                        per folder by `can_create_tenant`. Wrapping these in
                        RequireAdmin meant folder-admin — the role the backend
                        has always allowed to create a tenant — was bounced to
                        the dashboard, so the right existed and could not be
                        used by anyone who had it.
                        While the flag is still being fetched the route must
                        render nothing rather than redirect: a `<Navigate>`
                        here races the features request, and opening /tenants
                        directly bounces to the dashboard and rewrites the URL
                        with it, so a reload does not recover — it just races
                        again. */}
                    {featuresLoading ? (
                      <>
                        <Route path="/tenants" element={null} />
                        <Route path="/tenants/:name" element={null} />
                      </>
                    ) : features?.enableTenants ? (
                      <>
                        <Route path="/tenants" element={<Tenants />} />
                        <Route path="/tenants/:name" element={<TenantDetail />} />
                      </>
                    ) : (
                      <>
                        <Route path="/tenants" element={<Navigate to="/dashboard" replace />} />
                        <Route path="/tenants/:name" element={<Navigate to="/dashboard" replace />} />
                      </>
                    )}
                    {/* Users — admin only */}
                    <Route path="/users" element={<RequireAdmin><Users /></RequireAdmin>} />
                    <Route path="/users/groups" element={<RequireAdmin><Groups /></RequireAdmin>} />
                    <Route path="/profile" element={<Profile />} />
                    <Route path="/cli-access" element={<CLIAccess />} />
                    <Route path="*" element={<NotFound />} />
                  </Routes>
                </Suspense>
              </ErrorBoundary>
            </Layout>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthInitializer>
      <AppRoutes />

      {/* Global Notifications */}
      <Notifications />
    </AuthInitializer>
  );
}
