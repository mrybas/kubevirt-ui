/**
 * Virtual Machines page - VM list and management
 */

import { useState, useEffect } from 'react';
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
  const [searchQuery, setSearchQuery] = useState('');
  const [filterFolder, setFilterFolder] = useState('');
  const [showCreateWizard, setShowCreateWizard] = useState(false);
  const [deleteModalVM, setDeleteModalVM] = useState<VM | null>(null);
  const [showBulkDeleteModal, setShowBulkDeleteModal] = useState(false);
  const [pendingActions, setPendingActions] = useState<Set<string>>(new Set());
  const { page, perPage, setPage, setPerPage } = usePagination(50);

  // Reset to page 1 when search or folder filter changes
  useEffect(() => { setPage(1); }, [searchQuery, filterFolder]); // eslint-disable-line react-hooks/exhaustive-deps

  // VM hooks - if no namespace selected, fetch all VMs from all projects
  const { data: vmData, isLoading, error, refetch: refetchVMs } = useVMs(selectedNamespace || undefined, page, perPage);
  const { data: namespacesData } = useNamespaces();
  const { data: foldersData } = useFoldersFlat();
  const startVM = useStartVM();
  const stopVM = useStopVM();
  const restartVM = useRestartVM();
  const deleteVM = useDeleteVM();

  const projects = namespacesData?.items || [];
  const allFolders = foldersData?.items ?? [];
  const total = vmData?.total ?? 0;
  const activeFolder = allFolders.find((f) => f.name === filterFolder) ?? null;

  // Namespaces belonging to the selected folder tree
  const folderNamespaces = filterFolder
    ? collectFolderNamespaces(filterFolder, allFolders)
    : null;

  const vmKey = (vm: { namespace: string; name: string }) => `${vm.namespace}/${vm.name}`;
  const addPending = (key: string) => setPendingActions(prev => new Set(prev).add(key));
  const removePending = (key: string) => setPendingActions(prev => { const s = new Set(prev); s.delete(key); return s; });

  // Filter VMs by search and folder
  const filteredVMs = (vmData?.items || []).filter(vm => {
    const q = searchQuery.toLowerCase();
    const matchesSearch = (
      vm.name.toLowerCase().includes(q) ||
      vm.namespace.toLowerCase().includes(q) ||
      (vm.project || '').toLowerCase().includes(q) ||
      (vm.environment || '').toLowerCase().includes(q) ||
      (vm.owner || '').toLowerCase().includes(q)
    );
    const matchesFolder = !folderNamespaces || folderNamespaces.has(vm.namespace);
    return matchesSearch && matchesFolder;
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
          <Link to={`/vms/${vm.namespace}/${vm.name}`} className="font-medium font-mono text-surface-100 hover:text-primary-400" onClick={e => e.stopPropagation()}>
            {vm.name}
          </Link>
          <p className="text-xs text-surface-500 font-mono">{vm.namespace}</p>
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
              placeholder="All folders"
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
          <button className="btn-primary" onClick={() => setShowCreateWizard(true)}>
            <Plus className="h-4 w-4" />
            Create VM
          </button>
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
          searchPlaceholder="Search by name, project, owner..."
          onSearch={setSearchQuery}
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
            action: (
              <button className="btn-primary" onClick={() => setShowCreateWizard(true)}>
                <Plus className="h-4 w-4" />
                Create VM
              </button>
            ),
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
                      <h4 className="font-semibold text-surface-100">{vm.name}</h4>
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
