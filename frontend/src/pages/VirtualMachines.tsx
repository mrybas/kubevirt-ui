/**
 * Virtual Machines page - VM list and management
 */

import { useState, useEffect, useRef } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Plus,
  Play,
  Square,
  RotateCw,
  Trash2,
  Cpu,
  MemoryStick,
  Server,
  Grid3X3,
  List,
  HardDrive,
  Loader2,
  Terminal,
} from 'lucide-react';
import { useVMs, useStartVM, useStopVM, useRestartVM, useDeleteVM } from '@/hooks/useVMs';
import { RefreshCw, Folder } from 'lucide-react';
import { useNamespaces } from '@/hooks/useNamespaces';
import { useAppStore } from '@/store';
import { useFoldersFlat } from '@/hooks/useFolders';
import type { VM } from '@/types/vm';
import type { Folder as FolderType } from '@/types/folder';
import { StatusBadge } from '@/components/common/StatusBadge';
import { CopyableValue } from '@/components/common/CopyableValue';
import { CreateVMWizard } from '@/components/vm/CreateVMWizard';
import { ConfirmDeleteModal } from '@/components/common/ConfirmDeleteModal';
import { CustomSelect } from '@/components/common/CustomSelect';
import { FolderBreadcrumb } from '@/components/folders/FolderBreadcrumb';
import { usePagination } from '@/hooks/usePagination';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';

type ViewMode = 'table' | 'grid';

const FOLDER_LABEL = 'kubevirt-ui.io/folder';
const ENV_LABEL = 'kubevirt-ui.io/environment';

// The env namespace ({folder}-{environment}) a VM *belongs* to by label.
// Tenant worker VMs physically live in `tenant-<name>` but carry the folder +
// environment of their tenant, so this maps them back to the env namespace the
// global selector / folder filter reasons about.
function vmEnvNamespace(vm: VM): string | null {
  const f = vm.labels?.[FOLDER_LABEL];
  const e = vm.labels?.[ENV_LABEL];
  return f && e ? `${f}-${e}` : null;
}

// Collect all namespace names in a folder subtree (including sub-folders)
function collectFolderNamespaces(folderName: string, allFolders: FolderType[]): Set<string> {
  const ns = new Set<string>();
  const add = (name: string) => {
    const f = allFolders.find((x) => x.name === name);
    if (!f) return;
    f.environments.forEach((e) => ns.add(e.name));
    f.children.forEach((c) => add(c.name));
  };
  add(folderName);
  return ns;
}

export function VirtualMachines() {
  const navigate = useNavigate();
  const { selectedNamespace } = useAppStore();
  const [viewMode, setViewMode] = useState<ViewMode>('table');
  const [filterFolder, setFilterFolder] = useState('');
  const [showCreateWizard, setShowCreateWizard] = useState(false);
  const [deleteModalVM, setDeleteModalVM] = useState<VM | null>(null);
  const [showBulkDeleteModal, setShowBulkDeleteModal] = useState(false);
  const [pendingActions, setPendingActions] = useState<Set<string>>(new Set());
  const { page, perPage, setPage, setPerPage } = usePagination(50);

  // Debounce search query 300ms before sending to backend
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleSearch = (q: string) => {
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => {
      setDebouncedSearch(q);
      setPage(1);
    }, 300);
  };

  // Reset to page 1 when folder filter changes
  useEffect(() => { setPage(1); }, [filterFolder]); // eslint-disable-line react-hooks/exhaustive-deps

  // The global namespace scope wins over the per-page folder filter — picking a
  // namespace clears any folder selection so the two can't conflict into an
  // empty list. While a namespace is selected the folder dropdown is disabled.
  useEffect(() => {
    if (selectedNamespace && filterFolder) setFilterFolder('');
  }, [selectedNamespace]); // eslint-disable-line react-hooks/exhaustive-deps

  // Always fetch across all accessible namespaces; the global namespace scope
  // is applied client-side (by label) below so tenant worker VMs — which live
  // in `tenant-<name>`, not in the env namespace — still surface under their
  // env. A server-side single-namespace fetch would miss them.
  const { data: vmData, isLoading, error, refetch: refetchVMs } = useVMs(undefined, page, perPage, debouncedSearch || undefined);
  const { data: namespacesData } = useNamespaces();
  const { data: foldersData } = useFoldersFlat();
  // Whether to offer creating a VM at all. The backend decides it — per
  // folder, with the predicates that enforce it — and the page reads the
  // answer. A viewer used to be shown two Create VM buttons, both of which
  // answer 403 (UAT run 4, B5).
  const mayCreate = (foldersData?.items ?? []).some(f => f.can_create);
  const startVM = useStartVM();
  const stopVM = useStopVM();
  const restartVM = useRestartVM();
  const deleteVM = useDeleteVM();

  const projects = namespacesData?.items || [];
  const allFolders = foldersData?.items ?? [];
  const total = vmData?.total ?? 0;
  const activeFolder = allFolders.find((f) => f.name === filterFolder) ?? null;

  // Namespaces belonging to the selected folder tree. Ignored while a global
  // namespace scope is active (the namespace scope wins).
  const folderNamespaces = !selectedNamespace && filterFolder
    ? collectFolderNamespaces(filterFolder, allFolders)
    : null;

  const vmKey = (vm: { namespace: string; name: string }) => `${vm.namespace}/${vm.name}`;
  const addPending = (key: string) => setPendingActions(prev => new Set(prev).add(key));
  const removePending = (key: string) => setPendingActions(prev => { const s = new Set(prev); s.delete(key); return s; });

  // Scope client-side; search is handled server-side via debouncedSearch.
  // A VM matches a namespace either by living in it or by belonging to its env
  // (worker VMs). The global namespace scope wins over the folder filter.
  const filteredVMs = (vmData?.items || []).filter(vm => {
    if (selectedNamespace) {
      return vm.namespace === selectedNamespace || vmEnvNamespace(vm) === selectedNamespace;
    }
    if (folderNamespaces) {
      const envNs = vmEnvNamespace(vm);
      return folderNamespaces.has(vm.namespace) || (!!envNs && folderNamespaces.has(envNs));
    }
    return true;
  });

  const handleStart = (vm: VM) => {
    const k = vmKey(vm);
    addPending(k);
    startVM.mutate({ namespace: vm.namespace, name: vm.name }, { onSettled: () => removePending(k) });
  };

  const handleStop = (vm: VM) => {
    const k = vmKey(vm);
    addPending(k);
    stopVM.mutate({ namespace: vm.namespace, name: vm.name }, { onSettled: () => removePending(k) });
  };

  const handleRestart = (vm: VM) => {
    const k = vmKey(vm);
    addPending(k);
    restartVM.mutate({ namespace: vm.namespace, name: vm.name }, { onSettled: () => removePending(k) });
  };

  const handleDelete = (vm: VM) => {
    setDeleteModalVM(vm);
  };

  const handleDeleteConfirm = () => {
    if (!deleteModalVM) return;
    const k = vmKey(deleteModalVM);
    addPending(k);
    deleteVM.mutate(
      { namespace: deleteModalVM.namespace, name: deleteModalVM.name },
      { onSettled: () => removePending(k) }
    );
    setDeleteModalVM(null);
  };

  const handleBulkDeleteConfirm = () => {
    bulkSelectedVMs.forEach(vm => {
      deleteVM.mutate({ namespace: vm.namespace, name: vm.name });
    });
    setBulkSelectedVMs([]);
    setShowBulkDeleteModal(false);
  };

  const [bulkSelectedVMs, setBulkSelectedVMs] = useState<VM[]>([]);

  const vmColumns: Column<VM>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (vm) => (
        <div>
          <Link to={`/vms/${vm.namespace}/${vm.name}`} className="font-medium text-surface-100 hover:text-primary-400" onClick={e => e.stopPropagation()}>
            {vm.display_name || vm.name}
          </Link>
          <p className="text-xs text-surface-500 font-mono">{vm.name} · {vm.namespace}</p>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (vm) => pendingActions.has(vmKey(vm)) ? (
        <div className="flex items-center gap-2 text-primary-400">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-xs">Processing...</span>
        </div>
      ) : <StatusBadge status={vm.status} />,
    },
    {
      key: 'node',
      header: 'Node',
      sortable: true,
      hideOnMobile: true,
      accessor: (vm) => <span>{vm.node || '-'}</span>,
    },
    {
      key: 'resources',
      header: 'CPU / Memory',
      hideOnMobile: true,
      accessor: (vm) => (
        <div className="flex items-center gap-3 text-sm">
          <span className="flex items-center gap-1"><Cpu className="h-3 w-3 text-surface-500" />{vm.cpu_cores || '-'}</span>
          <span className="flex items-center gap-1"><MemoryStick className="h-3 w-3 text-surface-500" />{vm.memory || '-'}</span>
        </div>
      ),
    },
    {
      key: 'ip',
      header: 'IP Address',
      hideOnMobile: true,
      accessor: (vm) => <CopyableValue value={vm.ip_address} className="text-sm text-surface-300" />,
    },
    {
      key: 'created',
      header: 'Age',
      sortable: true,
      hideOnMobile: true,
      accessor: (vm) => <span>{vm.created ? new Date(vm.created).toLocaleDateString() : '-'}</span>,
    },
  ];

  const getVMActions = (vm: VM): MenuItem[] => {
    if (pendingActions.has(vmKey(vm))) return [];
    const items: MenuItem[] = [];
    if (vm.status === 'Stopped') {
      items.push({ label: 'Start', icon: <Play className="h-4 w-4" />, onClick: () => handleStart(vm) });
    } else {
      items.push({ label: 'Stop', icon: <Square className="h-4 w-4" />, onClick: () => handleStop(vm) });
    }
    items.push({ label: 'Restart', icon: <RotateCw className="h-4 w-4" />, onClick: () => handleRestart(vm) });
    items.push({ label: 'Console', icon: <Terminal className="h-4 w-4" />, onClick: () => navigate(`/vms/${vm.namespace}/${vm.name}`) });
    items.push({ label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => handleDelete(vm), variant: 'danger' });
    return items;
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-surface-100">Virtual Machines</h1>
          <p className="text-surface-400 mt-1">
            {filteredVMs.length} virtual machine{filteredVMs.length !== 1 ? 's' : ''}
            {activeFolder && (
              <span className="ml-2 inline-flex items-center gap-1 text-primary-400">
                <Folder className="h-3.5 w-3.5" />
                {activeFolder.display_name}
              </span>
            )}
            {!activeFolder && selectedNamespace && ` in ${selectedNamespace}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {allFolders.length > 0 && (
            <CustomSelect
              value={filterFolder}
              onChange={setFilterFolder}
              disabled={!!selectedNamespace}
              placeholder={selectedNamespace ? 'Namespace scope' : 'All folders'}
              options={[
                { value: '', label: 'All folders' },
                ...allFolders.map((f) => ({
                  value: f.name,
                  label: f.path.length > 0
                    ? `${f.path.join(' › ')} › ${f.display_name}`
                    : f.display_name,
                })),
              ]}
            />
          )}
          {/* View mode toggle */}
          <div className="flex border border-surface-700 rounded-lg overflow-hidden">
            <button
              onClick={() => setViewMode('table')}
              className={`p-2 ${viewMode === 'table' ? 'bg-surface-700 text-surface-100' : 'text-surface-400 hover:bg-surface-800'}`}
            >
              <List className="h-4 w-4" />
            </button>
            <button
              onClick={() => setViewMode('grid')}
              className={`p-2 ${viewMode === 'grid' ? 'bg-surface-700 text-surface-100' : 'text-surface-400 hover:bg-surface-800'}`}
            >
              <Grid3X3 className="h-4 w-4" />
            </button>
          </div>
          <button onClick={() => refetchVMs()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          {mayCreate && (
            <button className="btn-primary" onClick={() => setShowCreateWizard(true)}>
              <Plus className="h-4 w-4" />
              Create VM
            </button>
          )}
        </div>
      </div>

      {/* Active folder breadcrumb */}
      {activeFolder && (
        <div className="flex items-center gap-2 text-sm text-surface-400">
          <span>Folder:</span>
          <FolderBreadcrumb folder={activeFolder} allFolders={allFolders} />
          <button
            onClick={() => setFilterFolder('')}
            className="ml-1 text-surface-500 hover:text-surface-300 text-xs"
          >
            × Clear
          </button>
        </div>
      )}

      {/* Content */}
      {error ? (
        <div className="card">
          <div className="card-body text-center py-12">
            <p className="text-red-400">Failed to load VMs</p>
          </div>
        </div>
      ) : viewMode === 'table' ? (
        <DataTable
          columns={vmColumns}
          data={filteredVMs}
          loading={isLoading}
          keyExtractor={(vm) => vmKey(vm)}
          actions={getVMActions}
          onRowClick={(vm) => navigate(`/vms/${vm.namespace}/${vm.name}`)}
          selectable
          onSelectionChange={setBulkSelectedVMs}
          bulkActions={[
            { label: 'Start', icon: <Play className="h-4 w-4" />, onClick: (items) => (items as VM[]).forEach((vm) => startVM.mutate({ namespace: vm.namespace, name: vm.name })) },
            { label: 'Stop', icon: <Square className="h-4 w-4" />, onClick: (items) => (items as VM[]).forEach((vm) => stopVM.mutate({ namespace: vm.namespace, name: vm.name })) },
            { label: 'Restart', icon: <RotateCw className="h-4 w-4" />, onClick: (items) => (items as VM[]).forEach((vm) => restartVM.mutate({ namespace: vm.namespace, name: vm.name })) },
            { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: (items) => { setBulkSelectedVMs(items as VM[]); setShowBulkDeleteModal(true); }, variant: 'danger' },
          ]}
          searchable
          searchPlaceholder="Search VMs by name or IP..."
          onSearch={handleSearch}
          pagination={{
            page,
            pageSize: perPage,
            total,
            onPageChange: setPage,
            onPageSizeChange: setPerPage,
          }}
          emptyState={{
            icon: <Server className="h-16 w-16" />,
            title: 'No virtual machines',
            description: 'Create your first virtual machine to get started.',
            action: mayCreate ? (
              <button className="btn-primary" onClick={() => setShowCreateWizard(true)}>
                <Plus className="h-4 w-4" />
                Create VM
              </button>
            ) : undefined,
          }}
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredVMs.map((vm) => (
            <Link
              key={vmKey(vm)}
              to={`/vms/${vm.namespace}/${vm.name}`}
              className="card hover:border-surface-600 transition-colors"
            >
              <div className="card-body">
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-primary-500/10 text-primary-400">
                      <Server className="h-5 w-5" />
                    </div>
                    <div>
                      <h4 className="font-semibold text-surface-100">{vm.display_name || vm.name}</h4>
                      <p className="text-xs text-surface-500 font-mono">{vm.name}</p>
                      <p className="text-xs text-surface-500">{vm.namespace}</p>
                    </div>
                  </div>
                  <StatusBadge status={vm.status} />
                </div>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div className="flex items-center gap-2">
                    <Cpu className="h-4 w-4 text-surface-500" />
                    <span className="text-surface-300">{vm.cpu_cores || '-'} vCPU</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <MemoryStick className="h-4 w-4 text-surface-500" />
                    <span className="text-surface-300">{vm.memory || '-'}</span>
                  </div>
                  <div className="flex items-center gap-2 col-span-2">
                    <HardDrive className="h-4 w-4 text-surface-500" />
                    <CopyableValue value={vm.ip_address} className="text-sm text-surface-300" />
                  </div>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      {/* Create VM Wizard */}
      {showCreateWizard && (
        <CreateVMWizard
          projects={projects.map(p => ({ name: p.name, display_name: (p as any).display_name || p.name }))}
          defaultProject={selectedNamespace}
          defaultFolderName={filterFolder || undefined}
          onClose={() => setShowCreateWizard(false)}
          onSuccess={() => {
            setShowCreateWizard(false);
            refetchVMs();
          }}
        />
      )}

      <ConfirmDeleteModal
        isOpen={!!deleteModalVM}
        onClose={() => setDeleteModalVM(null)}
        onConfirm={handleDeleteConfirm}
        resourceName={deleteModalVM?.name ?? ''}
        resourceType="Virtual Machine"
        isDeleting={deleteVM.isPending}
      />

      <ConfirmDeleteModal
        isOpen={showBulkDeleteModal}
        onClose={() => setShowBulkDeleteModal(false)}
        onConfirm={handleBulkDeleteConfirm}
        resourceName={`${bulkSelectedVMs.length} virtual machine${bulkSelectedVMs.length !== 1 ? 's' : ''}`}
        resourceType="Virtual Machines"
        isDeleting={deleteVM.isPending}
      />
    </div>
  );
}
