import { useNavigate } from 'react-router-dom';
import { ShieldOff } from 'lucide-react';

export default function AccessDenied() {
  const navigate = useNavigate();

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4">
      <ShieldOff className="h-16 w-16 text-surface-500" />
      <h1 className="text-2xl font-bold text-surface-100">Access Denied</h1>
      <p className="text-surface-400">You don't have permission to view this page.</p>
      <button
        onClick={() => navigate('/dashboard')}
        className="btn btn-primary"
      >
        Go to Dashboard
      </button>
    </div>
  );
}
