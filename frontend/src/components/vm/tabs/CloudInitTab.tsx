import { useEffect, useState } from 'react';
import { AlertCircle, FileText, Lock, RefreshCw } from 'lucide-react';
import clsx from 'clsx';
import { useVMCloudInit, useUpdateVMCloudInit } from '@/hooks/useVMs';
import { notify } from '@/store/notifications';

interface ApiErrorLike extends Error {
  status?: number;
}

export function CloudInitTab({ namespace, name }: { namespace: string; name: string }) {
  const { data, isLoading, error } = useVMCloudInit(namespace, name);
  const update = useUpdateVMCloudInit();

  const [draft, setDraft] = useState('');
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    if (data) setDraft(data.user_data ?? '');
  }, [data]);

  const handleSave = async () => {
    try {
      await update.mutateAsync({ namespace, name, user_data: draft });
      notify.success('Cloud-init userData saved — restart VM to take effect');
      setIsEditing(false);
    } catch (e) {
      notify.error('Failed to save', e instanceof Error ? e.message : String(e));
    }
  };

  const handleCancel = () => {
    setDraft(data?.user_data ?? '');
    setIsEditing(false);
  };

  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  const errStatus = (error as ApiErrorLike | undefined)?.status;
  if (errStatus === 404) {
    return (
      <div className="card">
        <div className="card-body flex flex-col items-center justify-center py-12 text-surface-400">
          <FileText className="w-10 h-10 mb-3 opacity-50" />
          <p className="text-sm mb-1">No cloud-init volume on this VM</p>
          <p className="text-xs text-surface-500 text-center max-w-md">
            Cloud-init userData can be edited only for VMs created with an inline
            <code className="mx-1">cloudInitNoCloud</code>
            or
            <code className="mx-1">cloudInitConfigDrive</code>
            volume.
          </p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card">
        <div className="card-body flex flex-col items-center justify-center py-12 text-surface-400">
          <AlertCircle className="w-10 h-10 mb-3 text-red-400 opacity-70" />
          <p className="text-sm">Failed to load cloud-init</p>
          <p className="text-xs text-red-400 mt-1">{(error as Error).message}</p>
        </div>
      </div>
    );
  }

  if (!data) return null;

  if (!data.editable) {
    return (
      <div className="card">
        <div className="card-body space-y-2">
          <div className="flex items-center gap-2 text-surface-100">
            <Lock className="w-4 h-4" />
            <h3 className="font-medium">Cloud-init not editable</h3>
          </div>
          <p className="text-sm text-surface-400">{data.note}</p>
          <p className="text-xs text-surface-500">Volume: <code>{data.volume_name}</code> · Source: {data.source}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-body space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="font-medium text-surface-100">Cloud-init userData</h3>
            <p className="text-xs text-surface-500 mt-0.5">
              Volume <code className="text-surface-400">{data.volume_name}</code> · {data.note}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {!isEditing && (
              <button onClick={() => setIsEditing(true)} className="btn-secondary text-sm">
                Edit
              </button>
            )}
            {isEditing && (
              <>
                <button
                  onClick={handleCancel}
                  className="btn-secondary text-sm"
                  disabled={update.isPending}
                >
                  Cancel
                </button>
                <button
                  onClick={handleSave}
                  className="btn-primary text-sm"
                  disabled={update.isPending}
                >
                  {update.isPending ? 'Saving...' : 'Save'}
                </button>
              </>
            )}
          </div>
        </div>

        {isEditing && (
          <div className="flex items-start gap-2 p-2.5 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-amber-300">
            <RefreshCw className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
            <span>
              Cloud-init runs at first boot. Changes apply to the next freshly-provisioned
              VMI; an existing running VM will not re-run cloud-init on simple restart.
            </span>
          </div>
        )}

        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          readOnly={!isEditing}
          spellCheck={false}
          placeholder="#cloud-config&#10;..."
          className={clsx(
            'w-full h-[600px] font-mono text-xs p-3 rounded-lg border resize-y',
            isEditing
              ? 'bg-surface-900 border-surface-700 text-surface-100 focus:border-primary-500 focus:outline-none'
              : 'bg-surface-950 border-surface-800 text-surface-300 cursor-default',
          )}
        />
      </div>
    </div>
  );
}
