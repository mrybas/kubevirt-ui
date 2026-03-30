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

// Auth initializer
function AuthInitializer({ children }: { children: React.ReactNode }) {
  const { setConfig, setUser, setLoading, accessToken, isAuthenticated } = useAuthStore();

  useEffect(() => {
    async function initAuth() {
      try {
        // Load auth config
        const config = await getAuthConfig();
        setConfig(config);

        // If no auth required, we're done
        if (config.type === 'none') {
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
  const { data: features } = useFeatures();

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
                    <Route path="/storage/classes" element={<StorageClasses />} />
                    <Route path="/storage/:namespace/:name" element={<ImageDetail />} />
                    {/* Network */}
                    <Route path="/network" element={<Networks />} />
                    <Route path="/network/vpcs" element={<Navigate to="/network?tab=vpcs" replace />} />
                    <Route path="/network/subnets" element={<Navigate to="/network?tab=subnets" replace />} />
                    <Route path="/network/system" element={<Navigate to="/network?tab=system" replace />} />
                    <Route path="/network/subnets/create" element={<Navigate to="/network?tab=subnets&create=true" replace />} />
                    <Route path="/network/subnets/:name" element={<NetworkDetail />} />
                    <Route path="/network/vpcs/create" element={<Navigate to="/network?tab=vpcs&create=true" replace />} />
                    <Route path="/network/vpcs/:name" element={<VPCDetail />} />
                    <Route path="/network/egress-gateways" element={<EgressGateways />} />
                    <Route path="/network/ovn-gateways" element={<OvnGateways />} />
                    <Route path="/network/bgp" element={<BgpPeering />} />
                    <Route path="/network/security-groups" element={<SecurityGroups />} />
                    <Route path="/network/security-groups/:name" element={<SecurityGroupDetail />} />
                    {/* Backups */}
                    <Route path="/backups" element={<Backups />} />
                    {/* Security */}
                    <Route path="/security/network-flows" element={<NetworkFlows />} />
                    <Route path="/security/cilium-policies" element={<CiliumPolicies />} />
                    <Route path="/security/baseline" element={<SecurityBaseline />} />
                    {/* Other */}
                    <Route path="/cluster" element={<Cluster />} />
                    <Route path="/projects" element={<Projects />} />
                    <Route path="/folders" element={<Folders />} />
                    <Route path="/folders/new" element={<Navigate to="/folders?create=true" replace />} />
                    <Route path="/folders/:name" element={<FolderDetail />} />
                    {/* Tenants — only when feature enabled */}
                    {features?.enableTenants ? (
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
                    <Route path="/users" element={<Users />} />
                    <Route path="/users/groups" element={<Groups />} />
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
