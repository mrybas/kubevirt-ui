import { useState } from 'react';
import { Trash2, X } from 'lucide-react';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  resourceName: string;
  resourceType: string;
  requireTyping?: boolean;
  isDeleting?: boolean;
}

export function ConfirmDeleteModal({
  isOpen,
  onClose,
  onConfirm,
  resourceName,
  resourceType,
  requireTyping = false,
  isDeleting = false,
}: Props) {
  const [typedName, setTypedName] = useState('');

  if (!isOpen) return null;

  const canConfirm = !requireTyping || typedName === resourceName;

  const handleClose = () => {
    setTypedName('');
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-900 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">Delete {resourceType}</h2>
          <button onClick={handleClose} className="p-1 text-surface-400 hover:text-surface-200 rounded transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-red-500/10 rounded-full flex items-center justify-center shrink-0">
              <Trash2 className="w-5 h-5 text-red-400" />
            </div>
            <p className="text-sm text-surface-300">
              Are you sure you want to delete{' '}
              <span className="font-semibold text-surface-100">{resourceName}</span>?
              This action cannot be undone.
            </p>
          </div>

          {requireTyping && (
            <div>
              <label className="block text-sm text-surface-400 mb-1">
                Type <strong className="text-surface-200">{resourceName}</strong> to confirm:
              </label>
              <input
                type="text"
                value={typedName}
                onChange={(e) => setTypedName(e.target.value)}
                placeholder={resourceName}
                autoFocus
                className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-red-500 focus:ring-1 focus:ring-red-500/50"
              />
            </div>
          )}
        </div>

        <div className="flex gap-3 px-5 pb-5">
          <button
            onClick={handleClose}
            disabled={isDeleting}
            className="flex-1 px-4 py-2 bg-surface-800 hover:bg-surface-700 text-surface-200 rounded-lg transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!canConfirm || isDeleting}
            className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
          >
            {isDeleting ? 'Deleting...' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
  );
}
