import { useNavigate } from 'react-router-dom';
import { ShieldOff } from 'lucide-react';

/**
 * A wall, with the reason on it.
 *
 * "You don't have permission to view this page" tells somebody they are in
 * the wrong place and nothing else, so a folder admin who finds the network
 * pages closed concludes it is a bug and reports it — which is what happened
 * in UAT run 4 (R-3). Where there is a reason worth giving, the caller gives
 * it, and the boundary reads as deliberate rather than broken.
 */
export default function AccessDenied({ reason }: { reason?: string }) {
  const navigate = useNavigate();

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 px-6 text-center">
      <ShieldOff className="h-16 w-16 text-surface-500" />
      <h1 className="text-2xl font-bold text-surface-100">Access Denied</h1>
      <p className="text-surface-400 max-w-xl">
        {reason ?? "You don't have permission to view this page."}
      </p>
      <button
        onClick={() => navigate('/dashboard')}
        className="btn btn-primary"
      >
        Go to Dashboard
      </button>
    </div>
  );
}
