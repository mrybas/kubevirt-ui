import { useState } from 'react';
import { ArrowRightLeft, X, Loader2, Server, AlertTriangle, CheckCircle, XCircle } from 'lucide-react';
import { useNodes } from '@/hooks/useNamespaces';

export function MigrateVMModal({
  vmName,
  currentNode,
  onClose,
  onMigrate,
  isLoading,
  error,
}: {
  vmName: string;
  currentNode?: string | null;
  onClose: () => void;
  onMigrate: (targetNode: string) => void;
  isLoading: boolean;
  error?: string;
}) {
  const { data: nodesData, isLoading: nodesLoading } = useNodes();
  const [selectedNode, setSelectedNode] = useState<string>('');

  const availableNodes = (nodesData?.items || []).filter(
    (node) => node.name !== currentNode && node.status === 'Ready'
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">
            <ArrowRightLeft className="w-5 h-5 inline mr-2 text-primary-400" />
            Live Migrate
          </h2>
          <button onClick={onClose} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="p-6 space-y-4">
          <p className="text-surface-300 text-sm">
            Migrate <span className="font-medium text-surface-100">{vmName}</span> to another node.
            The VM will stay running during migration.
          </p>

          {currentNode && (
            <div className="flex items-center gap-2 px-3 py-2 bg-surface-900/50 rounded-lg border border-surface-700">
              <Server className="h-4 w-4 text-surface-400" />
              <span className="text-sm text-surface-400">Current node:</span>
              <span className="text-sm font-medium text-surface-200">{currentNode}</span>
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">Target node</label>
            {nodesLoading ? (
              <div className="flex items-center gap-2 text-surface-400 text-sm py-3">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading nodes...
              </div>
            ) : availableNodes.length === 0 ? (
              <div className="flex items-center gap-2 text-amber-400 text-sm py-3">
                <AlertTriangle className="h-4 w-4" />
                No other ready nodes available for migration.
              </div>
            ) : (
              <div className="space-y-2">
                {availableNodes.map((node) => (
                  <button
                    key={node.name}
                    type="button"
                    onClick={() => setSelectedNode(node.name)}
                    className={`w-full flex items-center gap-3 px-4 py-3 rounded-lg border text-left transition-all ${
                      selectedNode === node.name
                        ? 'border-primary-500/50 bg-primary-500/10 text-primary-300'
                        : 'border-surface-600 bg-surface-900/30 text-surface-300 hover:border-surface-500 hover:bg-surface-800'
                    }`}
                  >
                    <Server className={`h-5 w-5 ${selectedNode === node.name ? 'text-primary-400' : 'text-surface-500'}`} />
                    <div className="flex-1 min-w-0">
                      <p className="font-medium truncate">{node.name}</p>
                      <p className="text-xs text-surface-500">
                        {node.roles?.join(', ') || 'worker'} • {node.cpu || '?'} CPU • {node.memory || '?'} RAM
                      </p>
                    </div>
                    {selectedNode === node.name && (
                      <CheckCircle className="h-5 w-5 text-primary-400 flex-shrink-0" />
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          {error && (
            <div className="flex items-center gap-2 text-red-400 text-sm bg-red-500/10 px-3 py-2 rounded-lg">
              <XCircle className="h-4 w-4 flex-shrink-0" />
              {error}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-3 px-6 py-4 border-t border-surface-700">
          <button onClick={onClose} className="btn-secondary" disabled={isLoading}>
            Cancel
          </button>
          <button
            onClick={() => selectedNode && onMigrate(selectedNode)}
            className="btn-primary"
            disabled={!selectedNode || isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Migrating...
              </>
            ) : (
              <>
                <ArrowRightLeft className="h-4 w-4" />
                Migrate
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
