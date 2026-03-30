import clsx from 'clsx';

interface StatusBadgeProps {
  status: string;
  className?: string;
}

const statusStyles: Record<string, string> = {
  Running: 'badge-success',
  Starting: 'badge-info',
  Stopping: 'badge-warning',
  Stopped: 'badge-neutral',
  Paused: 'badge-warning',
  Migrating: 'badge-info',
  Failed: 'badge-error',
  Error: 'badge-error',
  ErrorUnschedulable: 'badge-error',
  Unknown: 'badge-neutral',
  Pending: 'badge-warning',
  Scheduling: 'badge-info',
  Scheduled: 'badge-info',
  WaitingForVolumeBinding: 'badge-warning',
};

export function StatusBadge({ status, className }: StatusBadgeProps) {
  const style = statusStyles[status] ?? 'badge-neutral';

  return <span className={clsx(style, className)}>{status}</span>;
}
