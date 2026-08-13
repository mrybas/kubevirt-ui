/**
 * Folder Detail Page — tabbed view: overview, members, children, envs, images, VMs
 */

import { useState } from 'react';
import { useParams, useNavigate, NavLink } from 'react-router-dom';
import {
  Folder,
  FolderOpen,
  Users,
  Server,
  HardDrive,
  Layers,
  Plus,
  Trash2,
  X,
  RefreshCw,
  Gauge,
  ChevronRight,
  ArrowLeft,
  Shield,
  Eye,
  Pencil,
  Move,
  KeyRound,
} from 'lucide-react';
import clsx from 'clsx';
import {
  useFolder,
  useFoldersFlat,
  useCreateFolder,
  useDeleteFolder,
  useUpdateFolder,
  useMoveFolder,
  useAddFolderEnvironment,
  useFolderQuotaHeadroom,
  useRemoveFolderEnvironment,
  useFolderAccess,
  useAddFolderAccess,
  useRemoveFolderAccess,
} from '../hooks/useFolders';
import { useTeams } from '../hooks/useProjects';
import { FolderBreadcrumb } from '../components/folders/FolderBreadcrumb';
import { FolderAccessTab } from '../components/folders/FolderAccessTab';
import { CustomSelect } from '../components/common/CustomSelect';
import { ConfirmDeleteModal } from '../components/common/ConfirmDeleteModal';
import type { FolderEnvironment, FolderRole } from '../types/folder';
import type { ByteUnit } from '../utils/quantity';
import {
  BYTE_UNITS, formatBytes, parseQuantity, sliderStep, trimNumber,
} from '../utils/quantity';
import { FOLDER_ROLE_LABELS, FOLDER_ROLE_DESCRIPTIONS } from '../types/folder';

type Tab = 'overview' | 'children' | 'environments' | 'members' | 'access' | 'images' | 'vms';

const TABS: { id: Tab; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'overview', label: 'Overview', icon: Folder },
  { id: 'children', label: 'Sub-folders', icon: FolderOpen },
  { id: 'environments', label: 'Environments', icon: Layers },
  { id: 'members', label: 'Members', icon: Users },
  { id: 'access', label: 'Access', icon: KeyRound },
  { id: 'vms', label: 'VMs', icon: Server },
  { id: 'images', label: 'Images', icon: HardDrive },
];

export default function FolderDetail() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [showCreateChild, setShowCreateChild] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showMoveModal, setShowMoveModal] = useState(false);

  const { data: folder, isLoading, refetch } = useFolder(name);
  const { data: flatData } = useFoldersFlat();
  const deleteFolder = useDeleteFolder();
  // const updateFolder = useUpdateFolder();

  const allFolders = flatData?.items ?? [];

  const handleDelete = async () => {
    if (!folder) return;
    await deleteFolder.mutateAsync(folder.name);
    navigate('/folders');
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-8 h-8 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!folder) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-surface-400">
        <Folder className="w-12 h-12 mb-4 opacity-50" />
        <p>Folder not found</p>
        <button onClick={() => navigate('/folders')} className="mt-4 btn-secondary text-sm">
          Back to Folders
        </button>
      </div>
    );
  }

  const memberCount = folder.teams.length + folder.users.length;

  return (
    <div className="space-y-6">
      {/* Breadcrumb + Back */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/folders')}
          className="p-1.5 text-surface-500 hover:text-surface-300 hover:bg-surface-800 rounded-lg transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <FolderBreadcrumb folder={folder} allFolders={allFolders} />
      </div>

      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 bg-primary-600/20 rounded-xl flex items-center justify-center">
            <FolderOpen className="w-6 h-6 text-primary-400" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-surface-100">{folder.display_name}</h1>
            <p className="text-sm text-surface-500 font-mono">{folder.name}</p>
            {folder.description && (
              <p className="text-sm text-surface-400 mt-1">{folder.description}</p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          <button onClick={() => setShowEditModal(true)} className="btn-secondary" title="Edit">
            <Pencil className="h-4 w-4" />
          </button>
          <button onClick={() => setShowMoveModal(true)} className="btn-secondary" title="Move">
            <Move className="h-4 w-4" />
          </button>
          <button
            onClick={() => setShowDeleteModal(true)}
            className="p-2 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
            title="Delete folder"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Stats row */}
      <div className="flex flex-wrap gap-4 text-sm text-surface-400">
        <span className="flex items-center gap-1.5">
          <FolderOpen className="w-4 h-4" />
          {folder.children.length} sub-folder{folder.children.length !== 1 ? 's' : ''}
        </span>
        <span className="flex items-center gap-1.5">
          <Layers className="w-4 h-4" />
          {folder.environments.length} environment{folder.environments.length !== 1 ? 's' : ''}
        </span>
        <span className="flex items-center gap-1.5">
          <Server className="w-4 h-4" />
          {folder.total_vms} VM{folder.total_vms !== 1 ? 's' : ''}
        </span>
        {folder.total_storage && (
          <span className="flex items-center gap-1.5">
            <HardDrive className="w-4 h-4" />
            {folder.total_storage}
          </span>
        )}
        <span className="flex items-center gap-1.5">
          <Users className="w-4 h-4" />
          {memberCount} member{memberCount !== 1 ? 's' : ''}
        </span>
        {folder.quota && (
          <span className="flex items-center gap-1.5">
            <Gauge className="w-4 h-4" />
            Quota configured
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="border-b border-surface-700">
        <div className="flex items-center gap-1">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={clsx(
                'flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors',
                activeTab === tab.id
                  ? 'border-primary-400 text-primary-400'
                  : 'border-transparent text-surface-400 hover:text-surface-200'
              )}
            >
              <tab.icon className="h-4 w-4" />
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' && <OverviewTab folder={folder} />}
      {activeTab === 'children' && (
        <ChildrenTab
          folder={folder}
          allFolders={allFolders}
          onCreateChild={() => setShowCreateChild(true)}
        />
      )}
      {activeTab === 'environments' && <EnvironmentsTab folder={folder} />}
      {activeTab === 'members' && <MembersTab folderName={folder.name} />}
      {activeTab === 'access' && <FolderAccessTab folder={folder} />}
      {activeTab === 'vms' && <VMsTab folder={folder} />}
      {activeTab === 'images' && <ImagesTab folder={folder} />}

      {/* Modals */}
      {showCreateChild && (
        <CreateChildModal
          parentFolder={folder}
          allFolders={allFolders}
          onClose={() => setShowCreateChild(false)}
        />
      )}
      {showDeleteModal && (
        <ConfirmDeleteFolderModal
          folderName={folder.name}
          onConfirm={handleDelete}
          onCancel={() => setShowDeleteModal(false)}
          isDeleting={deleteFolder.isPending}
        />
      )}
      {showEditModal && (
        <EditFolderModal
          folder={folder}
          onClose={() => setShowEditModal(false)}
        />
      )}
      {showMoveModal && (
        <MoveFolderModal
          folder={folder}
          allFolders={allFolders.filter((f) => f.name !== folder.name && !f.path.includes(folder.name))}
          onClose={() => setShowMoveModal(false)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OverviewTab
// ---------------------------------------------------------------------------

function OverviewTab({ folder }: { folder: ReturnType<typeof useFolder>['data'] & {} }) {
  if (!folder) return null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      {/* Metadata */}
      <div className="card">
        <div className="card-body space-y-3">
          <h3 className="font-medium text-surface-100 mb-3">Details</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-surface-400">Name</span>
              <span className="text-surface-200 font-mono">{folder.name}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-surface-400">Display Name</span>
              <span className="text-surface-200">{folder.display_name}</span>
            </div>
            {folder.parent_id && (
              <div className="flex justify-between">
                <span className="text-surface-400">Parent</span>
                <NavLink to={`/folders/${folder.parent_id}`} className="text-primary-400 hover:text-primary-300 flex items-center gap-1">
                  {folder.parent_id} <ChevronRight className="h-3 w-3" />
                </NavLink>
              </div>
            )}
            {folder.created_by && (
              <div className="flex justify-between">
                <span className="text-surface-400">Created by</span>
                <span className="text-surface-200">{folder.created_by}</span>
              </div>
            )}
            {folder.created_at && (
              <div className="flex justify-between">
                <span className="text-surface-400">Created at</span>
                <span className="text-surface-200">{new Date(folder.created_at).toLocaleDateString()}</span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Quota */}
      {folder.quota ? (
        <div className="card">
          <div className="card-body">
            <h3 className="font-medium text-surface-100 mb-3 flex items-center gap-2">
              <Gauge className="h-4 w-4 text-surface-400" />
              Quota
            </h3>
            <div className="space-y-2 text-sm">
              {folder.quota.cpu && (
                <div className="flex justify-between">
                  <span className="text-surface-400">CPU</span>
                  <span className="text-surface-200">{folder.quota.cpu} cores</span>
                </div>
              )}
              {folder.quota.memory && (
                <div className="flex justify-between">
                  <span className="text-surface-400">Memory</span>
                  <span className="text-surface-200">{folder.quota.memory}</span>
                </div>
              )}
              {folder.quota.storage && (
                <div className="flex justify-between">
                  <span className="text-surface-400">Storage</span>
                  <span className="text-surface-200">{folder.quota.storage}</span>
                </div>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="card">
          <div className="card-body flex items-center justify-center h-full text-surface-500 text-sm">
            No quota configured
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ChildrenTab
// ---------------------------------------------------------------------------

function ChildrenTab({
  folder,
  allFolders: _allFolders2,
  onCreateChild,
}: {
  folder: NonNullable<ReturnType<typeof useFolder>['data']>;
  allFolders: NonNullable<ReturnType<typeof useFoldersFlat>['data']>['items'];
  onCreateChild: () => void;
}) {
  const navigate = useNavigate();

  if (folder.children.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-surface-400">
        <FolderOpen className="w-10 h-10 mb-3 opacity-50" />
        <p className="mb-3">No sub-folders yet</p>
        <button onClick={onCreateChild} className="btn-primary text-sm">
          <Plus className="h-4 w-4" />
          Create Sub-folder
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button onClick={onCreateChild} className="btn-primary text-sm">
          <Plus className="h-4 w-4" />
          New Sub-folder
        </button>
      </div>
      {folder.children.map((child) => (
        <div
          key={child.name}
          className="flex items-center justify-between p-4 border border-surface-700 rounded-xl bg-surface-800/50 hover:bg-surface-800 transition-colors cursor-pointer"
          onClick={() => navigate(`/folders/${child.name}`)}
        >
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-primary-600/20 rounded-lg flex items-center justify-center">
              <Folder className="w-4 h-4 text-primary-400" />
            </div>
            <div>
              <p className="font-medium text-surface-100">{child.display_name}</p>
              <p className="text-xs text-surface-500 font-mono">{child.name}</p>
            </div>
          </div>
          <div className="flex items-center gap-4 text-xs text-surface-400">
            <span className="flex items-center gap-1"><Server className="w-3 h-3" />{child.total_vms} VMs</span>
            <ChevronRight className="h-4 w-4" />
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EnvironmentsTab
// ---------------------------------------------------------------------------

/** Quantities come back from the API as plain numbers. */
function fmtQuota(v: number | null): string {
  if (v === null) return '—';
  return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, '');
}

function fmtBytes(v: number | null): string {
  if (v === null) return '—';
  const gi = v / 2 ** 30;
  if (gi >= 1) return `${fmtQuota(gi)}Gi`;
  const mi = v / 2 ** 20;
  return `${fmtQuota(mi)}Mi`;
}


function EnvironmentsTab({ folder }: { folder: NonNullable<ReturnType<typeof useFolder>['data']> }) {
  const [showAdd, setShowAdd] = useState(false);
  const [deleteModalEnv, setDeleteModalEnv] = useState<FolderEnvironment | null>(null);
  const removeEnv = useRemoveFolderEnvironment(folder.name);

  const handleRemove = (env: FolderEnvironment) => {
    setDeleteModalEnv(env);
  };

  const handleRemoveConfirm = async () => {
    if (!deleteModalEnv) return;
    await removeEnv.mutateAsync(deleteModalEnv.environment);
    setDeleteModalEnv(null);
  };

  return (
    <div className="space-y-3">
      {folder.environments.length === 0 && !showAdd ? (
        <div className="flex flex-col items-center justify-center h-48 text-surface-400">
          <Layers className="w-10 h-10 mb-3 opacity-50" />
          <p className="mb-3">No environments yet</p>
          <button onClick={() => setShowAdd(true)} className="btn-primary text-sm">
            <Plus className="h-4 w-4" />
            Add Environment
          </button>
        </div>
      ) : (
        <>
          <div className="divide-y divide-surface-700 border border-surface-700 rounded-xl overflow-hidden">
            {folder.environments.map((env) => (
              <div
                key={env.name}
                className="flex items-center justify-between px-4 py-3 bg-surface-800/50 hover:bg-surface-800 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <div className="w-2 h-2 rounded-full bg-emerald-500" />
                  <div>
                    <span className="text-sm font-medium text-surface-200">{env.environment}</span>
                    <span className="text-xs text-surface-500 ml-2 font-mono">{env.name}</span>
                  </div>
                </div>
                <div className="flex items-center gap-4">
                  <div className="flex items-center gap-3 text-xs text-surface-400">
                    <span className="flex items-center gap-1">
                      <Server className="w-3 h-3" />
                      {env.vm_count}
                    </span>
                    {env.storage_used && (
                      <span className="flex items-center gap-1">
                        <HardDrive className="w-3 h-3" />
                        {env.storage_used}
                      </span>
                    )}
                  </div>
                  <button
                    onClick={() => handleRemove(env)}
                    disabled={removeEnv.isPending}
                    className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                    title="Remove environment"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>

          <button
            onClick={() => setShowAdd(true)}
            className="flex items-center gap-1.5 text-sm text-surface-500 hover:text-primary-400 transition-colors"
          >
            <Plus className="w-4 h-4" />
            Add Environment
          </button>

          {showAdd && (
            <AddEnvironmentModal folder={folder} onClose={() => setShowAdd(false)} />
          )}
        </>
      )}

      <ConfirmDeleteModal
        isOpen={!!deleteModalEnv}
        onClose={() => setDeleteModalEnv(null)}
        onConfirm={handleRemoveConfirm}
        resourceName={deleteModalEnv?.environment ?? ''}
        resourceType="Environment"
        isDeleting={removeEnv.isPending}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// MembersTab
// ---------------------------------------------------------------------------

function MembersTab({ folderName }: { folderName: string }) {
  const [showAddForm, setShowAddForm] = useState(false);
  const [accessType, setAccessType] = useState<'team' | 'user'>('team');
  const [selectedName, setSelectedName] = useState('');
  const [selectedRole, setSelectedRole] = useState<FolderRole>('editor');

  const { data: accessData, isLoading } = useFolderAccess(folderName);
  const { data: teamsData } = useTeams();
  const addAccess = useAddFolderAccess(folderName);
  const removeAccess = useRemoveFolderAccess(folderName);

  const handleAdd = async () => {
    if (!selectedName) return;
    await addAccess.mutateAsync({ type: accessType, name: selectedName, role: selectedRole });
    setShowAddForm(false);
    setSelectedName('');
  };

  return (
    <div className="space-y-4">
      <p className="text-xs text-surface-500">
        Access granted here propagates to all sub-folders and environments.
      </p>

      {!showAddForm ? (
        <button
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2 px-4 py-2 border border-dashed border-surface-600 hover:border-primary-500 text-surface-400 hover:text-primary-400 rounded-lg transition-colors w-full justify-center"
        >
          <Plus className="w-4 h-4" />
          Add Team or User
        </button>
      ) : (
        <div className="bg-surface-900/50 border border-surface-700 rounded-lg p-4 space-y-4">
          <div className="flex gap-4">
            {(['team', 'user'] as const).map((t) => (
              <label key={t} className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  value={t}
                  checked={accessType === t}
                  onChange={() => { setAccessType(t); setSelectedName(''); }}
                  className="text-primary-600"
                />
                <span className="text-surface-300 capitalize">{t}</span>
              </label>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-surface-400 mb-1">
                {accessType === 'team' ? 'Select Team' : 'User Email'}
              </label>
              {accessType === 'team' ? (
                <CustomSelect
                  value={selectedName}
                  onChange={setSelectedName}
                  placeholder="Select a team..."
                  options={[{ value: '', label: 'Select a team...' }, ...(teamsData?.items ?? []).map(t => ({ value: t.name, label: t.display_name }))]}
                />
              ) : (
                <input
                  type="email"
                  value={selectedName}
                  onChange={(e) => setSelectedName(e.target.value)}
                  placeholder="user@example.com"
                  className="input w-full"
                />
              )}
            </div>
            <div>
              <label className="block text-sm text-surface-400 mb-1">Role</label>
              <CustomSelect
                value={selectedRole}
                onChange={(v) => setSelectedRole(v as FolderRole)}
                options={Object.entries(FOLDER_ROLE_LABELS).map(([v, l]) => ({ value: v, label: l }))}
              />
            </div>
          </div>

          <p className="text-xs text-surface-500">{FOLDER_ROLE_DESCRIPTIONS[selectedRole]}</p>

          <div className="flex justify-end gap-2">
            <button onClick={() => { setShowAddForm(false); setSelectedName(''); }} className="btn-secondary text-sm">
              Cancel
            </button>
            <button
              onClick={handleAdd}
              disabled={!selectedName || addAccess.isPending}
              className="btn-primary text-sm"
            >
              {addAccess.isPending ? 'Adding...' : 'Add Access'}
            </button>
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="flex justify-center py-8">
          <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
        </div>
      ) : (accessData?.items ?? []).length === 0 ? (
        <div className="text-center py-8 text-surface-400">
          <Users className="w-8 h-8 mx-auto mb-2 opacity-50" />
          <p>No access configured</p>
        </div>
      ) : (
        <div className="space-y-2">
          {accessData?.items.map((entry) => (
            <div
              key={entry.id}
              className="flex items-center justify-between p-3 bg-surface-900/50 border border-surface-700/50 rounded-lg"
            >
              <div className="flex items-center gap-3">
                <div className={clsx(
                  'w-8 h-8 rounded-lg flex items-center justify-center',
                  entry.type === 'team' ? 'bg-purple-600/20 text-purple-400' : 'bg-emerald-600/20 text-emerald-400'
                )}>
                  {entry.type === 'team' ? (
                    <Users className="w-4 h-4" />
                  ) : (
                    <span className="text-sm font-medium">{entry.name?.[0]?.toUpperCase() || '?'}</span>
                  )}
                </div>
                <div>
                  <div className="text-sm font-medium text-surface-200">{entry.name}</div>
                  <div className="text-xs text-surface-500">
                    {entry.type === 'team' ? 'Team' : 'User'}
                    {entry.inherited && (
                      <span className="ml-2 text-amber-500">· inherited from {entry.folder}</span>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <RoleBadge role={entry.role as FolderRole} />
                {!entry.inherited && (
                  <button
                    onClick={() => removeAccess.mutateAsync(entry.id)}
                    disabled={removeAccess.isPending}
                    className="p-1.5 text-surface-400 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors"
                    title="Remove access"
                  >
                    <X className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Creating an environment, including taking the room from a sibling.
 *
 * A dialog rather than the inline row it replaced: the row had to carry a
 * name, three quota fields, the folder's remaining headroom, and — once you
 * ask for more than is free — a slider per sibling. That is a form, not a row.
 *
 * Memory and storage are entered as a number plus a unit, and the sliders
 * move in that unit. A slider over bytes is unusable: 16Gi is 17179869184, so
 * the thumb steps in amounts nobody asked for and the label reads as noise.
 *
 * Sub-folders donate on the same terms as environments — a child folder's
 * declared quota is reserved out of the parent whether or not anything runs
 * in it, so it is exactly as much a place to take room from.
 *
 * The reallocation travels with the create in one request. Shrinking a
 * sibling and then failing to create what the room was for would leave
 * someone smaller for nothing, so the server validates the whole plan first
 * and gives the room back if the create fails.
 */

type Dim = 'cpu' | 'memory' | 'storage';

interface Donor {
  key: string;
  label: string;
  kind: 'environment' | 'folder';
  /** Current quota per dimension, in cores/bytes; 0 when it holds none. */
  held: Record<Dim, number>;
}

export function AddEnvironmentModal({
  folder,
  onClose,
}: {
  folder: NonNullable<ReturnType<typeof useFolder>['data']>;
  onClose: () => void;
}) {
  const [name, setName] = useState('');
  const [cpu, setCpu] = useState('');
  const [memory, setMemory] = useState('');
  const [storage, setStorage] = useState('');
  const [memUnit, setMemUnit] = useState<ByteUnit>('Gi');
  const [storUnit, setStorUnit] = useState<ByteUnit>('Gi');
  // Per dimension, per donor, how much to take — in cores/bytes.
  const [takeFrom, setTakeFrom] = useState<Record<Dim, Record<string, number>>>({
    cpu: {}, memory: {}, storage: {},
  });
  const [error, setError] = useState<string | null>(null);

  const addEnv = useAddFolderEnvironment(folder.name);
  const { data: headroom } = useFolderQuotaHeadroom(folder.name);

  const unitOf = (dim: Dim): ByteUnit => (dim === 'memory' ? memUnit : storUnit);

  /** What the form asks for, in cores/bytes. */
  const wanted: Record<Dim, number> = {
    cpu: parseQuantity(cpu) ?? 0,
    memory: memory ? (Number(memory) || 0) * BYTE_UNITS[memUnit] : 0,
    storage: storage ? (Number(storage) || 0) * BYTE_UNITS[storUnit] : 0,
  };

  const donors: Donor[] = [
    ...(folder.environments ?? []).map(env => ({
      key: `env:${env.environment}`,
      label: env.environment,
      kind: 'environment' as const,
      held: {
        cpu: parseQuantity(env.quota_cpu) ?? 0,
        memory: parseQuantity(env.quota_memory) ?? 0,
        storage: parseQuantity(env.quota_storage) ?? 0,
      },
    })),
    ...(folder.children ?? []).map(child => ({
      key: `folder:${child.name}`,
      label: `${child.display_name || child.name} (sub-folder)`,
      kind: 'folder' as const,
      held: {
        cpu: parseQuantity(child.quota?.cpu) ?? 0,
        memory: parseQuantity(child.quota?.memory) ?? 0,
        storage: parseQuantity(child.quota?.storage) ?? 0,
      },
    })),
  ];

  const shortfallOf = (dim: Dim): number => {
    const free = headroom?.free[dim] ?? null;
    if (free === null) return 0;   // the folder does not cap this dimension
    const taken = Object.values(takeFrom[dim]).reduce((a, b) => a + b, 0);
    return Math.max(0, wanted[dim] - free - taken);
  };

  const shortfalls: Record<Dim, number> = {
    cpu: shortfallOf('cpu'), memory: shortfallOf('memory'), storage: shortfallOf('storage'),
  };
  const short = (['cpu', 'memory', 'storage'] as Dim[]).filter(d => shortfalls[d] > 0);
  const canSubmit = name.trim() !== '' && short.length === 0 && !addEnv.isPending;

  const setTake = (dim: Dim, key: string, amount: number) =>
    setTakeFrom(prev => ({ ...prev, [dim]: { ...prev[dim], [key]: amount } }));

  const handleSubmit = async () => {
    const env = name.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    if (!env) return;
    setError(null);

    // One entry per donor carrying only the dimensions actually taken from
    // it. The server merges into the donor's current quota, so a donor that
    // gives memory keeps the CPU nobody mentioned.
    const reallocate = donors
      .filter(d => (['cpu', 'memory', 'storage'] as Dim[]).some(dim => (takeFrom[dim][d.key] ?? 0) > 0))
      .map(d => {
        const left: Record<string, string> = {};
        if ((takeFrom.cpu[d.key] ?? 0) > 0) {
          left.cpu = trimNumber(d.held.cpu - takeFrom.cpu[d.key]);
        }
        if ((takeFrom.memory[d.key] ?? 0) > 0) {
          left.memory = formatBytes(d.held.memory - takeFrom.memory[d.key], memUnit);
        }
        if ((takeFrom.storage[d.key] ?? 0) > 0) {
          left.storage = formatBytes(d.held.storage - takeFrom.storage[d.key], storUnit);
        }
        return { source: d.key.split(':')[1], kind: d.kind, ...left };
      });

    try {
      await addEnv.mutateAsync({
        environment: env,
        quota_cpu: cpu || undefined,
        quota_memory: memory ? `${memory}${memUnit}` : undefined,
        quota_storage: storage ? `${storage}${storUnit}` : undefined,
        ...(reallocate.length ? { reallocate } : {}),
      });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create the environment');
    }
  };

  const DIM_LABEL: Record<Dim, string> = { cpu: 'CPU', memory: 'memory', storage: 'storage' };

  const renderShort = (dim: Dim) => {
    const eligible = donors.filter(d => d.held[dim] > 0);
    const amount = dim === 'cpu'
      ? `${trimNumber(shortfalls[dim])} CPU`
      : `${formatBytes(shortfalls[dim], unitOf(dim))} of ${DIM_LABEL[dim]}`;

    if (eligible.length === 0) {
      return (
        <p key={dim} className="text-xs text-amber-400">
          {amount} short, and nothing else in this folder holds a {DIM_LABEL[dim]}
          {' '}quota to take it from. Raise the folder ceiling instead.
        </p>
      );
    }

    return (
      <div key={dim} className="p-3 bg-amber-900/10 border border-amber-800/30 rounded-lg space-y-3">
        <p className="text-xs text-amber-300/90">{amount} short. Take it from:</p>
        {eligible.map(d => {
          const unit = unitOf(dim);
          const taken = takeFrom[dim][d.key] ?? 0;
          const step = dim === 'cpu' ? 1 : sliderStep(d.held[dim], unit);
          const show = (v: number) => (dim === 'cpu' ? trimNumber(v) : formatBytes(v, unit));
          return (
            <div key={d.key} className="flex items-center gap-3">
              <span className="text-xs text-surface-300 w-28 truncate" title={d.label}>
                {d.label}
              </span>
              <input
                type="range" min={0} max={d.held[dim]} step={step} value={taken}
                aria-label={`Take ${DIM_LABEL[dim]} from ${d.label}`}
                onChange={(e) => setTake(dim, d.key, Number(e.target.value))}
                className="flex-1"
              />
              <span className="text-xs text-surface-400 w-36 text-right">
                −{show(taken)} → leaves {show(d.held[dim] - taken)}
              </span>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-xl mx-4 shadow-2xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">Add Environment</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Name *</label>
            <input
              type="text" value={name} autoFocus
              onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
              placeholder="dev, staging, prod" className="input w-full"
            />
            <p className="text-xs text-surface-500 mt-1">
              Namespace: <span className="font-mono">{folder.name}-{name || '…'}</span>
            </p>
          </div>

          {headroom?.quota && (
            <div className="text-xs text-surface-400">
              Folder ceiling:{' '}
              {headroom.quota.cpu && (
                <span className="mr-3">
                  CPU {headroom.quota.cpu} · free{' '}
                  <span className="text-surface-200">{fmtQuota(headroom.free.cpu)}</span>
                </span>
              )}
              {headroom.quota.memory && (
                <span className="mr-3">
                  memory {headroom.quota.memory} · free{' '}
                  <span className="text-surface-200">{fmtBytes(headroom.free.memory)}</span>
                </span>
              )}
              {headroom.quota.storage && (
                <span>
                  storage {headroom.quota.storage} · free{' '}
                  <span className="text-surface-200">{fmtBytes(headroom.free.storage)}</span>
                </span>
              )}
            </div>
          )}

          <div className="grid grid-cols-3 gap-2">
            <div>
              <label className="block text-xs text-surface-400 mb-1">CPU</label>
              <input
                type="text" value={cpu} onChange={(e) => setCpu(e.target.value)}
                placeholder="e.g. 8" className="input w-full" aria-label="Environment CPU quota"
              />
            </div>
            <div>
              <label className="block text-xs text-surface-400 mb-1">Memory</label>
              <div className="flex gap-1">
                <input
                  type="number" min={0} value={memory}
                  onChange={(e) => setMemory(e.target.value)}
                  placeholder="16" className="input w-full"
                  aria-label="Environment memory quota"
                />
                <UnitToggle value={memUnit} onChange={setMemUnit} label="Memory unit" />
              </div>
            </div>
            <div>
              <label className="block text-xs text-surface-400 mb-1">Storage</label>
              <div className="flex gap-1">
                <input
                  type="number" min={0} value={storage}
                  onChange={(e) => setStorage(e.target.value)}
                  placeholder="100" className="input w-full"
                  aria-label="Environment storage quota"
                />
                <UnitToggle value={storUnit} onChange={setStorUnit} label="Storage unit" />
              </div>
            </div>
          </div>
          <p className="text-xs text-surface-500">
            Enforced by Kubernetes as a ResourceQuota on the namespace — it
            applies to kubectl as well, not only to this UI. Leave empty for no
            quota.
          </p>

          {short.map(renderShort)}

          {error && <p className="text-sm text-red-400">{error}</p>}
        </div>

        <div className="flex justify-end gap-3 p-5 border-t border-surface-700">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button onClick={handleSubmit} disabled={!canSubmit} className="btn-primary">
            {addEnv.isPending ? 'Creating...' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Mi/Gi, as two buttons — a two-option select is a worse control than a pair. */
function UnitToggle({
  value, onChange, label,
}: {
  value: ByteUnit;
  onChange: (u: ByteUnit) => void;
  label: string;
}) {
  return (
    <div className="flex rounded-lg border border-surface-600 overflow-hidden" role="group" aria-label={label}>
      {(['Mi', 'Gi'] as ByteUnit[]).map(u => (
        <button
          key={u}
          type="button"
          onClick={() => onChange(u)}
          aria-pressed={value === u}
          className={clsx(
            'px-2 text-xs',
            value === u
              ? 'bg-primary-600 text-white'
              : 'bg-surface-900 text-surface-400 hover:text-surface-200',
          )}
        >
          {u}
        </button>
      ))}
    </div>
  );
}


function RoleBadge({ role }: { role: FolderRole }) {
  const styles: Record<FolderRole, string> = {
    admin: 'bg-red-900/30 text-red-400',
    editor: 'bg-amber-900/30 text-amber-400',
    viewer: 'bg-surface-700 text-surface-400',
  };
  const icons: Record<FolderRole, React.ReactNode> = {
    admin: <Shield className="w-3 h-3" />,
    editor: <Pencil className="w-3 h-3" />,
    viewer: <Eye className="w-3 h-3" />,
  };
  return (
    <span className={`flex items-center gap-1 px-2 py-1 text-xs rounded ${styles[role]}`}>
      {icons[role]}
      {FOLDER_ROLE_LABELS[role]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// VMsTab / ImagesTab — placeholder (links to filtered views)
// ---------------------------------------------------------------------------

function VMsTab({ folder }: { folder: NonNullable<ReturnType<typeof useFolder>['data']> }) {
  const navigate = useNavigate();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-surface-400">
          VMs across all environments in this folder and sub-folders.
        </p>
        <button
          onClick={() => navigate('/vms')}
          className="btn-secondary text-sm"
        >
          <Server className="h-4 w-4" />
          View All VMs
        </button>
      </div>

      <div className="card">
        <div className="card-body text-center py-12 text-surface-400">
          <Server className="w-10 h-10 mx-auto mb-3 opacity-50" />
          <p className="font-medium text-surface-300">{folder.total_vms} VMs</p>
          <p className="text-sm mt-1">
            Across {folder.environments.length} environment{folder.environments.length !== 1 ? 's' : ''}
          </p>
          <button
            onClick={() => navigate('/vms')}
            className="mt-4 btn-secondary text-sm"
          >
            Browse VMs
          </button>
        </div>
      </div>
    </div>
  );
}

function ImagesTab({ folder: _folder }: { folder: NonNullable<ReturnType<typeof useFolder>['data']> }) {
  const navigate = useNavigate();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-surface-400">
          Images available in this folder's environments.
        </p>
        <button
          onClick={() => navigate('/storage/images')}
          className="btn-secondary text-sm"
        >
          <HardDrive className="h-4 w-4" />
          Manage Images
        </button>
      </div>

      <div className="card">
        <div className="card-body text-center py-12 text-surface-400">
          <HardDrive className="w-10 h-10 mx-auto mb-3 opacity-50" />
          <p className="text-sm">
            Images are managed in the Storage section, filtered by folder.
          </p>
          <button
            onClick={() => navigate('/storage/images')}
            className="mt-4 btn-secondary text-sm"
          >
            Go to Storage
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------------

function CreateChildModal({
  parentFolder,
  allFolders: _allFolders,
  onClose,
}: {
  parentFolder: NonNullable<ReturnType<typeof useFolder>['data']>;
  allFolders: NonNullable<ReturnType<typeof useFoldersFlat>['data']>['items'];
  onClose: () => void;
}) {
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const createFolder = useCreateFolder();

  const handleDisplayNameChange = (value: string) => {
    setDisplayName(value);
    if (!name || name === toKebabCase(displayName)) setName(toKebabCase(value));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await createFolder.mutateAsync({
      name,
      display_name: displayName,
      description: description || undefined,
      parent_id: parentFolder.name,
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl">
        <div className="flex items-center justify-between p-5 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">Create Sub-folder</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          <p className="text-sm text-surface-400">
            Parent: <span className="text-surface-200 font-medium">{parentFolder.display_name}</span>
          </p>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Folder Name *</label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => handleDisplayNameChange(e.target.value)}
              placeholder="Sub-team"
              required
              className="input w-full"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Identifier *</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
              placeholder="sub-team"
              required
              pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
              className="input w-full font-mono text-sm"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="input w-full resize-none"
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button
              type="submit"
              disabled={createFolder.isPending || !name || !displayName}
              className="btn-primary"
            >
              {createFolder.isPending ? 'Creating...' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function EditFolderModal({
  folder,
  onClose,
}: {
  folder: NonNullable<ReturnType<typeof useFolder>['data']>;
  onClose: () => void;
}) {
  const [displayName, setDisplayName] = useState(folder.display_name);
  const [description, setDescription] = useState(folder.description ?? '');
  // Quota could be set when the folder was created and never afterwards: the
  // detail page shows a quota panel and the edit dialog had no way to reach
  // it, so "No quota configured" was a permanent state for every existing
  // folder. `PATCH /folders/{name}` has always accepted a quota.
  const [quotaCpu, setQuotaCpu] = useState(folder.quota?.cpu ?? '');
  const [quotaMemory, setQuotaMemory] = useState(folder.quota?.memory ?? '');
  const [quotaStorage, setQuotaStorage] = useState(folder.quota?.storage ?? '');
  const updateFolder = useUpdateFolder();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const quota = (quotaCpu || quotaMemory || quotaStorage)
      ? {
          cpu: quotaCpu || undefined,
          memory: quotaMemory || undefined,
          storage: quotaStorage || undefined,
        }
      : undefined;
    await updateFolder.mutateAsync({
      name: folder.name,
      request: { display_name: displayName, description, ...(quota ? { quota } : {}) },
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl">
        <div className="flex items-center justify-between p-5 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">Edit Folder</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Display Name *</label>
            <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required className="input w-full" />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Description</label>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} className="input w-full resize-none" />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Quota</label>
            <div className="grid grid-cols-3 gap-2">
              <input
                type="text" value={quotaCpu} onChange={(e) => setQuotaCpu(e.target.value)}
                placeholder="CPU e.g. 16" className="input w-full" aria-label="Quota CPU"
              />
              <input
                type="text" value={quotaMemory} onChange={(e) => setQuotaMemory(e.target.value)}
                placeholder="Memory e.g. 32Gi" className="input w-full" aria-label="Quota memory"
              />
              <input
                type="text" value={quotaStorage} onChange={(e) => setQuotaStorage(e.target.value)}
                placeholder="Storage e.g. 200Gi" className="input w-full" aria-label="Quota storage"
              />
            </div>
            <p className="text-xs text-surface-500 mt-1">
              A budget shown when creating VMs in this folder, not a limit the
              cluster enforces. Leave all three empty for no quota.
            </p>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={updateFolder.isPending || !displayName} className="btn-primary">
              {updateFolder.isPending ? 'Saving...' : 'Save'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function MoveFolderModal({
  folder,
  allFolders,
  onClose,
}: {
  folder: NonNullable<ReturnType<typeof useFolder>['data']>;
  allFolders: NonNullable<ReturnType<typeof useFoldersFlat>['data']>['items'];
  onClose: () => void;
}) {
  const [newParentId, setNewParentId] = useState(folder.parent_id ?? '');
  const moveFolder = useMoveFolder();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await moveFolder.mutateAsync({ name: folder.name, request: { new_parent_id: newParentId || null } });
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl">
        <div className="flex items-center justify-between p-5 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">Move Folder</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          <p className="text-sm text-surface-400">
            Moving <strong className="text-surface-200">{folder.display_name}</strong> to a new parent.
          </p>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">New Parent</label>
            <select value={newParentId} onChange={(e) => setNewParentId(e.target.value)} className="input w-full">
              <option value="">— Root level —</option>
              {allFolders.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.path.length > 0 ? `${f.path.join(' › ')} › ` : ''}{f.display_name}
                </option>
              ))}
            </select>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={moveFolder.isPending} className="btn-primary">
              {moveFolder.isPending ? 'Moving...' : 'Move'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ConfirmDeleteFolderModal({
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
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl p-5">
        <div className="w-12 h-12 bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <Trash2 className="w-6 h-6 text-red-400" />
        </div>
        <h2 className="text-lg font-semibold text-surface-100 text-center mb-2">Delete Folder</h2>
        <p className="text-sm text-surface-400 text-center mb-4">
          This will delete <strong>{folderName}</strong> and all its sub-folders, environments, and resources. Cannot be undone.
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
          <button onClick={onCancel} className="flex-1 btn-secondary">Cancel</button>
          <button
            onClick={onConfirm}
            disabled={confirmName !== folderName || isDeleting}
            className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
          >
            {isDeleting ? 'Deleting...' : 'Delete'}
          </button>
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
