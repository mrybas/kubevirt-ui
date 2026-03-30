import { useNavigate } from 'react-router-dom';

export default function NotFound() {
  const navigate = useNavigate();

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4">
      <h1 className="text-4xl font-bold text-surface-100">404</h1>
      <p className="text-surface-400">Page not found</p>
      <button
        onClick={() => navigate('/dashboard')}
        className="btn btn-primary"
      >
        Go to Dashboard
      </button>
    </div>
  );
}
