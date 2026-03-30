import { useState } from 'react';
import { Camera, CheckCircle, XCircle, Loader2, RotateCcw, Check, X, Trash2 } from 'lucide-react';
import { useVMSnapshots, useCreateVMSnapshot, useDeleteVMSnapshot, useRestoreVMSnapshot } from '@/hooks/useVMs';

export function SnapshotsTab({ vm }: { vm: any }) {
  const { data: snapshots, isLoading } = useVMSnapshots(vm.namespace, vm.name);
  const createSnapshot = useCreateVMSnapshot();
  const deleteSnapshot = useDeleteVMSnapshot();
  const restoreSnapshot = useRestoreVMSnapshot();
  const [snapshotName, setSnapshotName] = useState('');
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [restoreConfirm, setRestoreConfirm] = useState<string | null>(null);

  const handleCreate = () => {
    if (!snapshotName.trim()) return;
    createSnapshot.mutate(
      { namespace: vm.namespace, vmName: vm.name, data: { snapshot_name: snapshotName.trim() } },
      { onSuccess: () => setSnapshotName('') }
    );
  };

  const handleDelete = (name: string) => {
    deleteSnapshot.mutate(
      { namespace: vm.namespace, vmName: vm.name, snapshotName: name },
      { onSuccess: () => setDeleteConfirm(null) }
    );
  };

  const handleRestore = (name: string) => {
    restoreSnapshot.mutate(
      { namespace: vm.namespace, vmName: vm.name, snapshotName: name },
      { onSuccess: () => setRestoreConfirm(null) }
    );
  };

  return (
    <div className="space-y-6">
      {/* Create snapshot */}
      <div className="card">
        <div className="card-header">
          <h3 className="font-display text-lg font-semibold">VM Snapshots</h3>
          <p className="text-surface-400 text-sm mt-1">
            Full VM snapshots including all disks and VM configuration
          </p>
        </div>
        <div className="p-4">
          <div className="flex gap-2">
            <input
              type="text"
              value={snapshotName}
              onChange={(e) => setSnapshotName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              placeholder="Snapshot name (e.g. before-upgrade)"
              className="input flex-1"
            />
            <button
              onClick={handleCreate}
              disabled={!snapshotName.trim() || createSnapshot.isPending}
              className="btn-primary"
            >
              {createSnapshot.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Camera className="h-4 w-4" />
              )}
              Create Snapshot
            </button>
          </div>
          {createSnapshot.error && (
            <p className="text-red-400 text-sm mt-2">{createSnapshot.error.message}</p>
          )}
        </div>
      </div>

      {/* Snapshots list */}
      <div className="card">
        <div className="card-header">
          <h3 className="font-display font-semibold">Snapshots</h3>
        </div>
        {isLoading ? (
          <div className="p-8 flex items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-surface-400" />
          </div>
        ) : !snapshots?.length ? (
          <div className="p-8 text-center text-surface-500">
            <Camera className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p>No VM snapshots yet</p>
            <p className="text-xs mt-1">Create a snapshot to save the current VM state</p>
          </div>
        ) : (
          <div className="divide-y divide-surface-700">
            {snapshots.map((snap) => (
              <div key={snap.name} className="px-4 py-3 flex items-center justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-surface-200 truncate">{snap.name}</span>
                    {snap.ready ? (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-emerald-500/10 text-emerald-400">
                        <CheckCircle className="h-3 w-3" />
                        Ready
                      </span>
                    ) : snap.error ? (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-red-500/10 text-red-400">
                        <XCircle className="h-3 w-3" />
                        Failed
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-amber-500/10 text-amber-400">
                        <Loader2 className="h-3 w-3 animate-spin" />
                        {snap.phase}
                      </span>
                    )}
                    {snap.indications?.includes('Online') && (
                      <span className="text-xs text-surface-500">online</span>
                    )}
                    {snap.indications?.includes('GuestAgent') && (
                      <span className="text-xs text-surface-500">quiesced</span>
                    )}
                  </div>
                  <div className="text-xs text-surface-500 mt-0.5">
                    {snap.creation_time ? new Date(snap.creation_time).toLocaleString() : ''}
                    {snap.error && <span className="text-red-400 ml-2">{snap.error}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  {/* Restore */}
                  {snap.ready && (
                    restoreConfirm === snap.name ? (
                      <div className="flex items-center gap-1 mr-2">
                        <span className="text-xs text-amber-400">Restore?</span>
                        <button
                          onClick={() => handleRestore(snap.name)}
                          className="btn-ghost p-1 text-amber-400 hover:text-amber-300"
                          disabled={restoreSnapshot.isPending}
                          title="Confirm restore"
                        >
                          {restoreSnapshot.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                        </button>
                        <button onClick={() => setRestoreConfirm(null)} className="btn-ghost p-1" title="Cancel">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setRestoreConfirm(snap.name)}
                        className="btn-ghost p-1 text-surface-500 hover:text-amber-400"
                        title="Restore VM to this snapshot"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </button>
                    )
                  )}
                  {/* Delete */}
                  {deleteConfirm === snap.name ? (
                    <div className="flex items-center gap-1">
                      <button onClick={() => handleDelete(snap.name)} className="btn-ghost p-1 text-red-400 hover:text-red-300" title="Confirm delete">
                        <Check className="h-3.5 w-3.5" />
                      </button>
                      <button onClick={() => setDeleteConfirm(null)} className="btn-ghost p-1" title="Cancel">
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <button onClick={() => setDeleteConfirm(snap.name)} className="btn-ghost p-1 text-surface-500 hover:text-red-400" title="Delete snapshot">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        {restoreSnapshot.error && (
          <div className="px-4 py-2 border-t border-surface-700">
            <p className="text-red-400 text-sm">{restoreSnapshot.error.message}</p>
          </div>
        )}
      </div>
    </div>
  );
}
