import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Loader2, AlertCircle } from 'lucide-react';
import { useAuthStore } from '../store/auth';
import { exchangeToken, getCurrentUser, validateState } from '../api/auth';

export function AuthCallback() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { setTokens, setUser } = useAuthStore();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function handleCallback() {
      const code = searchParams.get('code');
      const state = searchParams.get('state');
      const errorParam = searchParams.get('error');
      const errorDescription = searchParams.get('error_description');

      // Handle error from OIDC provider
      if (errorParam) {
        setError(errorDescription || errorParam);
        return;
      }

      // Validate required params
      if (!code) {
        setError('Missing authorization code');
        return;
      }

      // Validate state (CSRF protection)
      if (state && !validateState(state)) {
        setError('Invalid state parameter');
        return;
      }

      try {
        // Exchange code for tokens
        const redirectUri = `${window.location.origin}/auth/callback`;
        const tokens = await exchangeToken(code, redirectUri);

        // Save tokens
        setTokens(tokens.access_token, tokens.refresh_token, tokens.id_token);

        // Get user info
        const user = await getCurrentUser(tokens.access_token);
        setUser(user);

        // Redirect to dashboard
        navigate('/dashboard');
      } catch (e) {
        console.error('Auth callback error:', e);
        setError('Authentication failed. Please try again.');
      }
    }

    handleCallback();
  }, [searchParams, navigate, setTokens, setUser]);

  if (error) {
    return (
      <div className="min-h-screen bg-surface-900 flex items-center justify-center p-8">
        <div className="card max-w-md w-full">
          <div className="card-body text-center">
            <AlertCircle className="h-12 w-12 text-red-400 mx-auto mb-4" />
            <h2 className="text-xl font-semibold text-surface-100 mb-2">Authentication Failed</h2>
            <p className="text-surface-400 mb-6">{error}</p>
            <button onClick={() => navigate('/login')} className="btn-primary">
              Try Again
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-surface-900 flex items-center justify-center">
      <div className="text-center">
        <Loader2 className="h-12 w-12 text-primary-400 animate-spin mx-auto mb-4" />
        <p className="text-surface-400">Completing sign in...</p>
      </div>
    </div>
  );
}
