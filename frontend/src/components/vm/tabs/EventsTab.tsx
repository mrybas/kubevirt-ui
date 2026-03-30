import { useRef } from 'react';
import { AlertTriangle, Clock, Loader2 } from 'lucide-react';
import { useVMEvents } from '@/hooks/useVMs';
import { useVirtualizer } from '@tanstack/react-virtual';

export function EventsTab({ namespace, name }: { namespace: string; name: string }) {
  const { data: eventsData, isLoading } = useVMEvents(namespace, name);
  const events = eventsData?.items || [];
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 80,
  });

  const formatTime = (ts: string | null) => {
    if (!ts) return '';
    const d = new Date(ts);
    const now = new Date();
    const diff = Math.floor((now.getTime() - d.getTime()) / 1000);
    if (diff < 60) return 'Just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return d.toLocaleString();
  };

  const sourceLabel = (source: string) => {
    switch (source) {
      case 'VirtualMachine': return 'VM';
      case 'VirtualMachineInstance': return 'VMI';
      case 'Pod': return 'Pod';
      case 'DataVolume': return 'DV';
      default: return source;
    }
  };

  return (
    <div className="card">
      <div className="card-header flex items-center justify-between">
        <h3 className="font-display text-lg font-semibold">Events</h3>
        <span className="text-xs text-surface-500">{events.length} events</span>
      </div>
      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-primary-400" />
        </div>
      ) : events.length === 0 ? (
        <div className="px-6 py-12 text-center text-surface-500">
          No events found for this VM.
        </div>
      ) : (
        <div ref={parentRef} className="overflow-auto" style={{ maxHeight: '500px' }}>
          <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
            {virtualizer.getVirtualItems().map(virtualRow => {
              const event = events[virtualRow.index]!;
              return (
                <div
                  key={virtualRow.index}
                  style={{
                    position: 'absolute',
                    top: `${virtualRow.start}px`,
                    left: 0,
                    right: 0,
                  }}
                  className="flex items-start gap-4 px-6 py-4 border-b border-surface-800"
                >
                  <div className={`mt-1 ${event.type === 'Warning' ? 'text-amber-400' : 'text-surface-500'}`}>
                    {event.type === 'Warning' ? (
                      <AlertTriangle className="h-4 w-4" />
                    ) : (
                      <Clock className="h-4 w-4" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`text-sm font-medium ${
                        event.type === 'Warning' ? 'text-amber-400' : 'text-surface-200'
                      }`}>
                        {event.reason}
                      </span>
                      <span className="text-xs px-1.5 py-0.5 rounded bg-surface-700 text-surface-400">
                        {sourceLabel(event.source)}
                      </span>
                      {event.count > 1 && (
                        <span className="text-xs text-surface-500">x{event.count}</span>
                      )}
                      <span className="text-xs text-surface-500 ml-auto">{formatTime(event.last_timestamp)}</span>
                    </div>
                    <p className="text-sm text-surface-400 mt-1 break-words">{event.message}</p>
                    {event.source === 'Pod' && (
                      <p className="text-xs text-surface-600 mt-0.5 font-mono truncate">{event.source_name}</p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
