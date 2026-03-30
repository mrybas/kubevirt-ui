import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Monitor, LogIn, Key, Loader2, AlertCircle } from 'lucide-react';
import { useAuthStore } from '../store/auth';
import { getAuthConfig, buildAuthorizationUrl, getCurrentUser } from '../api/auth';

export function Login() {
  const navigate = useNavigate();
  const { config, setConfig, setTokens, setUser, setLoading, isAuthenticated, accessToken } =
    useAuthStore();
  const [error, setError] = useState<string | null>(null);
  const [isLoadingConfig, setIsLoadingConfig] = useState(true);
  const [tokenInput, setTokenInput] = useState('');
  const [isLoggingIn, setIsLoggingIn] = useState(false);

  // Load auth config on mount
  useEffect(() => {
    async function loadConfig() {
      try {
        const authConfig = await getAuthConfig();
        setConfig(authConfig);

        // If auth is disabled, auto-login
        if (authConfig.type === 'none') {
          setTokens('anonymous');
          setUser({
            id: 'anonymous',
            email: 'anonymous@local',
            username: 'Anonymous',
            groups: ['kubevirt-ui-admins'],
          });
          navigate('/dashboard');
        }
      } catch (e) {
        setError('Failed to load authentication configuration');
      } finally {
        setIsLoadingConfig(false);
        setLoading(false);
      }
    }

    loadConfig();
  }, []);

  // Redirect if already authenticated
  useEffect(() => {
    if (isAuthenticated && accessToken) {
      navigate('/dashboard');
    }
  }, [isAuthenticated, accessToken, navigate]);

  const handleOIDCLogin = () => {
    if (!config || config.type !== 'oidc') return;

    const redirectUri = `${window.location.origin}/auth/callback`;
    const authUrl = buildAuthorizationUrl(config, redirectUri);
    window.location.href = authUrl;
  };

  const handleTokenLogin = async () => {
    if (!tokenInput.trim()) {
      setError('Please enter a token');
      return;
    }

    setIsLoggingIn(true);
    setError(null);

    try {
      const user = await getCurrentUser(tokenInput);
      setTokens(tokenInput);
      setUser(user);
      navigate('/dashboard');
    } catch (e) {
      setError('Invalid token or unauthorized');
    } finally {
      setIsLoggingIn(false);
    }
  };

  if (isLoadingConfig) {
    return (
      <div className="min-h-screen bg-surface-900 flex items-center justify-center">
        <Loader2 className="h-8 w-8 text-primary-400 animate-spin" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-surface-900 flex">
      {/* Left side - branding */}
      <div className="hidden lg:flex lg:w-1/2 bg-gradient-to-br from-surface-800 to-surface-900 items-center justify-center p-12">
        <div className="max-w-md text-center">
          <div className="flex items-center justify-center gap-3 mb-8">
            <div className="p-3 bg-primary-500/20 rounded-xl">
              <Monitor className="h-12 w-12 text-primary-400" />
            </div>
          </div>
          <h1 className="text-4xl font-bold text-surface-100 mb-4">KubeVirt UI</h1>
          <p className="text-lg text-surface-400">
            Manage virtual machines on Kubernetes with a modern, intuitive interface.
          </p>
          <div className="mt-12 grid grid-cols-3 gap-6 text-center">
            <div>
              <div className="text-2xl font-bold text-primary-400">Simple</div>
              <div className="text-sm text-surface-500">Easy to use</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-primary-400">Secure</div>
              <div className="text-sm text-surface-500">RBAC native</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-primary-400">GitOps</div>
              <div className="text-sm text-surface-500">Compatible</div>
            </div>
          </div>
        </div>
      </div>

      {/* Right side - login form */}
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-md">
          <div className="lg:hidden flex items-center justify-center gap-3 mb-8">
            <div className="p-2 bg-primary-500/20 rounded-xl">
              <Monitor className="h-8 w-8 text-primary-400" />
            </div>
            <h1 className="text-2xl font-bold text-surface-100">KubeVirt UI</h1>
          </div>

          <div className="card">
            <div className="card-body space-y-6">
              <div className="text-center">
                <h2 className="text-xl font-semibold text-surface-100">Sign In</h2>
                <p className="text-surface-400 mt-1">Access your virtual machines</p>
              </div>

              {error && (
                <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2 text-red-400 text-sm">
                  <AlertCircle className="h-4 w-4 flex-shrink-0" />
                  {error}
                </div>
              )}

              {config?.type === 'oidc' && (
                <button
                  onClick={handleOIDCLogin}
                  className="btn-primary w-full justify-center py-3"
                >
                  <LogIn className="h-5 w-5" />
                  Sign in with SSO
                </button>
              )}

              {config?.type === 'token' && (
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-surface-300 mb-1.5">
                      Service Account Token
                    </label>
                    <textarea
                      value={tokenInput}
                      onChange={(e) => setTokenInput(e.target.value)}
                      placeholder="Paste your Kubernetes token here..."
                      className="input w-full h-24 font-mono text-sm"
                    />
                    <p className="text-xs text-surface-500 mt-1">
                      Get token: kubectl get secret TOKEN_NAME -o jsonpath='{'{.data.token}'}' |
                      base64 -d
                    </p>
                  </div>
                  <button
                    onClick={handleTokenLogin}
                    disabled={isLoggingIn}
                    className="btn-primary w-full justify-center py-3"
                  >
                    {isLoggingIn ? (
                      <>
                        <Loader2 className="h-5 w-5 animate-spin" />
                        Signing in...
                      </>
                    ) : (
                      <>
                        <Key className="h-5 w-5" />
                        Sign in with Token
                      </>
                    )}
                  </button>
                </div>
              )}

              {config?.type === 'none' && (
                <div className="text-center text-surface-400">
                  <p>Authentication is disabled.</p>
                  <p className="text-sm mt-2">Redirecting to dashboard...</p>
                </div>
              )}

              {/* Dev mode hint */}
              {import.meta.env.DEV && config?.type === 'oidc' && (
                <div className="pt-4 border-t border-surface-700">
                  <p className="text-xs text-surface-500 text-center">
                    Development credentials: admin / admin_password
                  </p>
                </div>
              )}
            </div>
          </div>

          <p className="text-center text-sm text-surface-500 mt-6">
            KubeVirt UI v0.1.0
          </p>
        </div>
      </div>
    </div>
  );
}
