import { AlertTriangle, TrendingUp, X } from 'lucide-react';
import { useState } from 'react';

interface ResourceQuotaWarningProps {
  cpuUsage?: number;
  memoryUsage?: number;
  storageUsage?: number;
  maxSchedulableCpu?: number;
  maxSchedulableMemory?: number;
  threshold?: number; // Percentage threshold to show warning (default 80)
}

export function ResourceQuotaWarning({
  cpuUsage = 0,
  memoryUsage = 0,
  storageUsage = 0,
  maxSchedulableCpu,
  maxSchedulableMemory,
  threshold = 80,
}: ResourceQuotaWarningProps) {
  const [dismissed, setDismissed] = useState(false);

  // Find resources above threshold
  const warnings: { resource: string; usage: number; color: string }[] = [];

  if (cpuUsage >= threshold) {
    warnings.push({ resource: 'CPU', usage: cpuUsage, color: cpuUsage >= 90 ? 'text-red-400' : 'text-amber-400' });
  }
  if (memoryUsage >= threshold) {
    warnings.push({ resource: 'Memory', usage: memoryUsage, color: memoryUsage >= 90 ? 'text-red-400' : 'text-amber-400' });
  }
  if (storageUsage >= threshold) {
    warnings.push({ resource: 'Storage', usage: storageUsage, color: storageUsage >= 90 ? 'text-red-400' : 'text-amber-400' });
  }

  // Check if max schedulable slot is too small for a typical VM
  const slotTooSmall = maxSchedulableCpu !== undefined && maxSchedulableCpu < 1;

  if (warnings.length === 0 && !slotTooSmall || dismissed) {
    return null;
  }

  const isCritical = warnings.some((w) => w.usage >= 90) || slotTooSmall;

  return (
    <div
      className={`rounded-lg border p-4 ${
        isCritical
          ? 'bg-red-500/10 border-red-500/30'
          : 'bg-amber-500/10 border-amber-500/30'
      }`}
    >
      <div className="flex items-start gap-3">
        <AlertTriangle
          className={`h-5 w-5 flex-shrink-0 mt-0.5 ${
            isCritical ? 'text-red-400' : 'text-amber-400'
          }`}
        />
        <div className="flex-1">
          <h4
            className={`font-medium ${
              isCritical ? 'text-red-400' : 'text-amber-400'
            }`}
          >
            {slotTooSmall ? 'Cannot Create New VMs' : isCritical ? 'Resource Limit Critical' : 'Resource Limit Warning'}
          </h4>
          {slotTooSmall && (
            <p className="text-sm text-surface-300 mt-1">
              No node has enough free CPU for a new VM.
              Largest available slot: <span className="font-mono font-medium text-red-400">{maxSchedulableCpu?.toFixed(2)} cores</span>
              {maxSchedulableMemory !== undefined && (
                <>, <span className="font-mono font-medium">{maxSchedulableMemory?.toFixed(1)} Gi RAM</span></>
              )}
              . Stop or delete existing VMs to free up resources.
            </p>
          )}
          {warnings.length > 0 && !slotTooSmall && (
            <p className="text-sm text-surface-300 mt-1">
              {warnings.length === 1
                ? `Your ${warnings[0].resource.toLowerCase()} usage is at ${warnings[0].usage.toFixed(0)}%`
                : `Multiple resources are near their limits:`}
            </p>
          )}
          {warnings.length > 1 && !slotTooSmall && (
            <div className="flex flex-wrap gap-3 mt-2">
              {warnings.map((w) => (
                <span
                  key={w.resource}
                  className={`flex items-center gap-1 text-sm ${w.color}`}
                >
                  <TrendingUp className="h-4 w-4" />
                  {w.resource}: {w.usage.toFixed(0)}%
                </span>
              ))}
            </div>
          )}
          {!slotTooSmall && (
            <p className="text-xs text-surface-500 mt-2">
              Consider freeing up resources or requesting a quota increase.
            </p>
          )}
        </div>
        <button
          onClick={() => setDismissed(true)}
          className="flex-shrink-0 p-1 text-surface-400 hover:text-surface-200 rounded transition-colors"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
