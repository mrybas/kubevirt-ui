import { 
  X, 
  CheckCircle, 
  AlertCircle, 
  AlertTriangle, 
  Info, 
  Loader2 
} from 'lucide-react';
import { useNotificationStore, type NotificationType } from '@/store/notifications';

const icons: Record<NotificationType, typeof CheckCircle> = {
  success: CheckCircle,
  error: AlertCircle,
  warning: AlertTriangle,
  info: Info,
  progress: Loader2,
};

const colors: Record<NotificationType, string> = {
  success: 'border-emerald-500/50 bg-emerald-500/10',
  error: 'border-red-500/50 bg-red-500/10',
  warning: 'border-amber-500/50 bg-amber-500/10',
  info: 'border-primary-500/50 bg-primary-500/10',
  progress: 'border-primary-500/50 bg-primary-500/10',
};

const iconColors: Record<NotificationType, string> = {
  success: 'text-emerald-400',
  error: 'text-red-400',
  warning: 'text-amber-400',
  info: 'text-primary-400',
  progress: 'text-primary-400',
};

export function Notifications() {
  const { notifications, removeNotification } = useNotificationStore();

  if (notifications.length === 0) return null;

  const visible = notifications.slice(-5);

  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-3 max-w-sm">
      {visible.map((notification, index) => {
        const Icon = icons[notification.type];
        
        return (
          <div
            key={notification.id}
            className={`
              flex items-start gap-3 p-4 rounded-lg border backdrop-blur-sm shadow-lg
              animate-slide-in-right
              ${colors[notification.type]}
            `}
            style={{ animationDelay: `${index * 50}ms` }}
          >
            <Icon 
              className={`h-5 w-5 flex-shrink-0 ${iconColors[notification.type]} ${
                notification.type === 'progress' ? 'animate-spin' : ''
              }`} 
            />
            
            <div className="flex-1 min-w-0">
              <p className="font-medium text-surface-100">{notification.title}</p>
              {notification.message && (
                <p className="text-sm text-surface-400 mt-0.5">{notification.message}</p>
              )}
              
              {/* Progress bar for progress type */}
              {notification.type === 'progress' && notification.progress !== undefined && (
                <div className="mt-2">
                  <div className="h-1.5 bg-surface-700 rounded-full overflow-hidden">
                    <div 
                      className="h-full bg-primary-500 rounded-full transition-all duration-300"
                      style={{ width: `${notification.progress}%` }}
                    />
                  </div>
                  <p className="text-xs text-surface-500 mt-1">{notification.progress}%</p>
                </div>
              )}
            </div>
            
            {/* Close button (not for progress notifications unless complete) */}
            {(notification.type !== 'progress' || notification.progress === 100) && (
              <button
                onClick={() => removeNotification(notification.id)}
                className="flex-shrink-0 p-1 text-surface-400 hover:text-surface-200 rounded transition-colors"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
