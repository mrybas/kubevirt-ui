import { create } from 'zustand';

export type NotificationType = 'success' | 'error' | 'warning' | 'info' | 'progress';

export interface Notification {
  id: string;
  type: NotificationType;
  title: string;
  message?: string;
  progress?: number; // 0-100 for progress type
  duration?: number; // ms, 0 for persistent
  createdAt: number;
}

interface NotificationState {
  notifications: Notification[];
  addNotification: (notification: Omit<Notification, 'id' | 'createdAt'>) => string;
  removeNotification: (id: string) => void;
  updateNotification: (id: string, updates: Partial<Notification>) => void;
  clearAll: () => void;
}

let notificationId = 0;

export const useNotificationStore = create<NotificationState>((set, get) => ({
  notifications: [],
  
  addNotification: (notification) => {
    const id = `notification-${++notificationId}`;
    const newNotification: Notification = {
      ...notification,
      id,
      createdAt: Date.now(),
      duration: notification.duration ?? (notification.type === 'progress' ? 0 : 5000),
    };
    
    set((state) => ({
      notifications: [...state.notifications, newNotification],
    }));
    
    // Auto-remove after duration (if not persistent)
    if (newNotification.duration && newNotification.duration > 0) {
      setTimeout(() => {
        get().removeNotification(id);
      }, newNotification.duration);
    }
    
    return id;
  },
  
  removeNotification: (id) => {
    set((state) => ({
      notifications: state.notifications.filter((n) => n.id !== id),
    }));
  },
  
  updateNotification: (id, updates) => {
    set((state) => ({
      notifications: state.notifications.map((n) =>
        n.id === id ? { ...n, ...updates } : n
      ),
    }));
  },
  
  clearAll: () => {
    set({ notifications: [] });
  },
}));

// Helper functions for common notification types
export const notify = {
  success: (title: string, message?: string) =>
    useNotificationStore.getState().addNotification({ type: 'success', title, message }),
  
  error: (title: string, message?: string) =>
    useNotificationStore.getState().addNotification({ type: 'error', title, message, duration: 0 }),
  
  warning: (title: string, message?: string) =>
    useNotificationStore.getState().addNotification({ type: 'warning', title, message }),
  
  info: (title: string, message?: string) =>
    useNotificationStore.getState().addNotification({ type: 'info', title, message }),
  
  progress: (title: string, message?: string, progress?: number) =>
    useNotificationStore.getState().addNotification({ type: 'progress', title, message, progress, duration: 0 }),
};

// Alias for hooks that use useNotifications name
export const useNotifications = () => useNotificationStore();
