/**
 * Folders List Page — root-level folder management
 */

import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Plus,
  Folder,
  RefreshCw,
  Server,
  Users,
  Trash2,
  X,
  Gauge,
  Eye,
} from 'lucide-react';
import {
  useFoldersTree,
  useCreateFolder,
  useDeleteFolder,
} from '../hooks/useFolders';
import type { Folder as FolderType, FolderQuota } from '../types/folder';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

export default function Folders() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [showCreateModal, setShowCreateModal] = useState(false);

  // Auto-open create modal from URL param (?create=true)
  useEffect(() => {
    if (searchParams.get('create') === 'true') {
      setShowCreateModal(true);
      searchParams.delete('create');
      setSearchParams(searchParams, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const [showDeleteModal, setShowDeleteModal] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const { data: treeData, isLoading, refetch } = useFoldersTree();
  const deleteFolder = useDeleteFolder();

  function flattenTree(folders: FolderType[]): FolderType[] {
    return folders.flatMap((f) => [f, ...flattenTree(f.children)]);
  }

  const allFolders = flattenTree(treeData?.items ?? []);
  const rootFolders = treeData?.items ?? [];

  const filtered = searchQuery
    ? allFolders.filter(
        (f) =>
          f.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
          f.display_name.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : rootFolders;

  const handleDelete = async (name: string) => {
    await deleteFolder.mutateAsync(name);
    setShowDeleteModal(null);
  };

  const columns: Column<FolderType>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (f) => (
        <div>
          {f.path.length > 0 && (
            <p className="text-xs text-surface-500 mb-0.5">{f.path.join(' › ')}</p>
          )}
          <span className="font-medium text-surface-100">{f.display_name}</span>
          <p className="text-xs text-surface-500 font-mono">{f.name}</p>
        </div>
      ),
    },
    {
      key: 'children',
      header: 'Sub-folders',
      hideOnMobile: true,
      accessor: (f) => f.children.length > 0 ? (
        <span className="flex items-center gap-1 text-xs">
          <Folder className="w-3.5 h-3.5 text-surface-500" />
          {f.children.length}
        </span>
      ) : <span className="text-surface-500">—</span>,
    },
    {
      key: 'vms',
      header: 'VMs',
      accessor: (f) => (
        <span className="flex items-center gap-1 text-xs">
          <Server className="w-3.5 h-3.5 text-surface-500" />
          {f.total_vms}
        </span>
      ),
    },
    {
      key: 'members',
      header: 'Members',
      hideOnMobile: true,
      accessor: (f) => {
        const count = f.teams.length + f.users.length;
        return count > 0 ? (
          <span className="flex items-center gap-1 text-xs">
            <Users className="w-3.5 h-3.5 text-surface-500" />
            {count}
          </span>
        ) : <span className="text-surface-500">—</span>;
      },
    },
    {
      key: 'quota',
      header: 'Quota',
      hideOnMobile: true,
      accessor: (f) => f.quota ? (
        <span className="flex items-center gap-1 text-xs text-surface-400">
          <Gauge className="w-3.5 h-3.5" />
          Set
        </span>
      ) : <span className="text-surface-500">—</span>,
    },
  ];

  const getActions = (f: FolderType): MenuItem[] => [
    { label: 'View Details', icon: <Eye className="h-4 w-4" />, onClick: () => navigate(`/folders/${f.name}`) },
    { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setShowDeleteModal(f.name), variant: 'danger' },
  ];

  return (
    <div className="space-y-6">
      <ActionBar
        title="Folders"
        subtitle="Organize VMs, environments, and access with hierarchical folders"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        <button onClick={() => setShowCreateModal(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create Folder
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filtered}
        loading={isLoading}
        keyExtractor={(f) => f.name}
        actions={getActions}
        onRowClick={(f) => navigate(`/folders/${f.name}`)}
        searchable
        searchPlaceholder="Search folders..."
        onSearch={setSearchQuery}
        emptyState={{
          icon: <Folder className="h-16 w-16" />,
          title: 'No folders yet',
          description: 'Create a folder to organize your VMs and environments.',
          action: (
            <button onClick={() => setShowCreateModal(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create your first folder
            </button>
          ),
        }}
      />

      {showCreateModal && (
        <CreateFolderModal
          parentOptions={allFolders}
          onClose={() => setShowCreateModal(false)}
        />
      )}

      {showDeleteModal && (
        <DeleteFolderModal
          folderName={showDeleteModal}
          onConfirm={() => handleDelete(showDeleteModal)}
          onCancel={() => setShowDeleteModal(null)}
          isDeleting={deleteFolder.isPending}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CreateFolderModal
// ---------------------------------------------------------------------------

function CreateFolderModal({
  parentOptions,
  initialParentId,
  onClose,
}: {
  parentOptions: FolderType[];
  initialParentId?: string;
  onClose: () => void;
}) {
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [parentId, setParentId] = useState(initialParentId ?? '');
  const [environments, setEnvironments] = useState<string[]>(['dev']);
  const [newEnv, setNewEnv] = useState('');
  const [enableQuota, setEnableQuota] = useState(false);
  const [quotaCpu, setQuotaCpu] = useState('');
  const [quotaMemory, setQuotaMemory] = useState('');
  const [quotaStorage, setQuotaStorage] = useState('');

  const createFolder = useCreateFolder();

  const handleDisplayNameChange = (value: string) => {
    setDisplayName(value);
    if (!name || name === toKebabCase(displayName)) {
      setName(toKebabCase(value));
    }
  };

  const addEnv = () => {
    const trimmed = newEnv.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    if (trimmed && !environments.includes(trimmed)) {
      setEnvironments([...environments, trimmed]);
      setNewEnv('');
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    let quota: FolderQuota | undefined;
    if (enableQuota && (quotaCpu || quotaMemory || quotaStorage)) {
      quota = {
        cpu: quotaCpu || undefined,
        memory: quotaMemory || undefined,
        storage: quotaStorage || undefined,
      };
    }
    await createFolder.mutateAsync({
      name,
      display_name: displayName,
      description: description || undefined,
      parent_id: parentId || undefined,
      environments: environments.filter(Boolean),
      quota,
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-lg mx-4 shadow-2xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b border-surface-700 sticky top-0 bg-surface-800 z-10">
          <h2 className="text-lg font-semibold text-surface-100">Create Folder</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {/* Display Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Folder Name *
            </label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => handleDisplayNameChange(e.target.value)}
              placeholder="My Team"
              required
              className="input w-full"
            />
          </div>

          {/* Slug */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Identifier *
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
              placeholder="my-team"
              required
              pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
              className="input w-full font-mono text-sm"
            />
            <p className="text-xs text-surface-500 mt-1">
              Used as prefix for environment namespaces, e.g. {name || 'my-team'}-dev
            </p>
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What is this folder for?"
              rows={2}
              className="input w-full resize-none"
            />
          </div>

          {/* Parent Folder */}
          {parentOptions.length > 0 && (
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                Parent Folder
              </label>
              <select
                value={parentId}
                onChange={(e) => setParentId(e.target.value)}
                className="input w-full"
              >
                <option value="">— Root level —</option>
                {parentOptions.map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.path.length > 0 ? `${f.path.join(' › ')} › ` : ''}{f.display_name}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Environments */}
          <div className="pt-3 border-t border-surface-700">
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Initial Environments
            </label>
            <div className="space-y-2 mb-2">
              {environments.map((env, idx) => (
                <div key={idx} className="flex items-center gap-2">
                  <div className="flex-1 px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-200 flex items-center justify-between">
                    <span>{env}</span>
                    <span className="text-xs text-surface-500 font-mono">
                      {name || 'folder'}-{env}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setEnvironments(environments.filter((_, i) => i !== idx))}
                    className="p-1.5 text-surface-500 hover:text-red-400 transition-colors"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={newEnv}
                onChange={(e) => setNewEnv(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                placeholder="dev, staging, prod..."
                className="flex-1 px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500"
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addEnv(); } }}
              />
              <button
                type="button"
                onClick={addEnv}
                disabled={!newEnv.trim()}
                className="p-1.5 bg-surface-700 hover:bg-surface-600 disabled:opacity-30 text-surface-300 rounded-lg transition-colors"
              >
                <Plus className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* Quota */}
          <div className="pt-3 border-t border-surface-700">
            <label className="flex items-center gap-2 cursor-pointer mb-3">
              <input
                type="checkbox"
                checked={enableQuota}
                onChange={(e) => setEnableQuota(e.target.checked)}
                className="rounded border-surface-600 text-primary-600 focus:ring-primary-500"
              />
              <span className="text-sm font-medium text-surface-300">Set quota</span>
              <span className="text-xs text-surface-500">(optional soft limit)</span>
            </label>

            {enableQuota && (
              <div className="grid grid-cols-3 gap-3">
                {[
                  { label: 'CPU (cores)', value: quotaCpu, set: setQuotaCpu, placeholder: 'e.g. 16' },
                  { label: 'Memory', value: quotaMemory, set: setQuotaMemory, placeholder: 'e.g. 32Gi' },
                  { label: 'Storage', value: quotaStorage, set: setQuotaStorage, placeholder: 'e.g. 200Gi' },
                ].map(({ label, value, set, placeholder }) => (
                  <div key={label}>
                    <label className="block text-xs text-surface-400 mb-1">{label}</label>
                    <input
                      type="text"
                      value={value}
                      onChange={(e) => set(e.target.value)}
                      placeholder={placeholder}
                      className="input w-full text-sm"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Submit */}
          <div className="flex justify-end gap-3 pt-3">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button
              type="submit"
              disabled={createFolder.isPending || !name || !displayName}
              className="btn-primary"
            >
              {createFolder.isPending ? 'Creating...' : 'Create Folder'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeleteFolderModal
// ---------------------------------------------------------------------------

function DeleteFolderModal({
  folderName,
  onConfirm,
  onCancel,
  isDeleting,
}: {
  folderName: string;
  onConfirm: () => void;
  onCancel: () => void;
  isDeleting: boolean;
}) {
  const [confirmName, setConfirmName] = useState('');

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl">
        <div className="p-5">
          <div className="w-12 h-12 bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
            <Trash2 className="w-6 h-6 text-red-400" />
          </div>
          <h2 className="text-lg font-semibold text-surface-100 text-center mb-2">Delete Folder</h2>
          <p className="text-sm text-surface-400 text-center mb-4">
            This will delete <strong>{folderName}</strong> and all its sub-folders, environments, and resources.
            This action cannot be undone.
          </p>
          <div className="mb-4">
            <label className="block text-sm text-surface-400 mb-1">
              Type <strong>{folderName}</strong> to confirm:
            </label>
            <input
              type="text"
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              placeholder={folderName}
              className="input w-full focus:border-red-500"
            />
          </div>
          <div className="flex gap-3">
            <button onClick={onCancel} className="flex-1 btn-secondary">
              Cancel
            </button>
            <button
              onClick={onConfirm}
              disabled={confirmName !== folderName || isDeleting}
              className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
            >
              {isDeleting ? 'Deleting...' : 'Delete Folder'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toKebabCase(str: string): string {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

// Export CreateFolderModal for use in FolderDetail
export { CreateFolderModal };
