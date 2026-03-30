/**
 * Unified Storage page - Images and Data disks management
 * 
 * Disk Types:
 * - Image: Base OS images (can be booted)
 * - Data: Empty/blank disks for data storage
 * 
 * Persistent flag:
 * - false: Disk is cloned for each VM (template)
 * - true: Disk is used directly by one VM
 */

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Plus,
  Loader2,
  HardDrive,
  X,
  Trash2,
  Clock,
  CheckCircle2,
  CircleDot,
  Image,
  Database,
  Lock,
  Search,
  Folder,
  Layers,
  Monitor,
  RefreshCw,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '../store';
import { useStorageClasses } from '../hooks/useStorage';
import { CustomSelect } from '../components/common/CustomSelect';
import { useGoldenImages, useCreateGoldenImage, useDeleteGoldenImage } from '../hooks/useTemplates';
import { useNamespaces } from '../hooks/useNamespaces';
import { useFoldersFlat } from '../hooks/useFolders';
import { FolderBreadcrumb } from '../components/folders/FolderBreadcrumb';
import { LoadingSkeleton } from '../components/common/LoadingSkeleton';
import { usePagination } from '../hooks/usePagination';
import { Pagination } from '../components/common/Pagination';
import { ConfirmDeleteModal } from '../components/common/ConfirmDeleteModal';
import { notify } from '../store/notifications';
import type { GoldenImage, GoldenImageCreate, ImageScope } from '../types/template';

type DiskType = 'image' | 'data';
type ActiveTab = 'images' | 'data';

interface Disk extends GoldenImage {
  disk_type: DiskType;
  persistent: boolean;
}

export function Storage() {
  const { selectedNamespace } = useAppStore();
  const navigate = useNavigate();
  const [showImportImageModal, setShowImportImageModal] = useState(false);
  const [showNewDiskModal, setShowNewDiskModal] = useState(false);
  const [activeTab, setActiveTab] = useState<ActiveTab>('images');
  const [searchQuery, setSearchQuery] = useState('');
  const [filterProject, setFilterProject] = useState<string>('');
  const [filterEnv, setFilterEnv] = useState<string>('');
  const [filterFolder, setFilterFolder] = useState<string>('');
  const { page, perPage, setPage, setPerPage } = usePagination(50);
  const [deleteModalDisk, setDeleteModalDisk] = useState<Disk | null>(null);
  useEffect(() => { setPage(1); }, [searchQuery, filterProject, filterEnv, filterFolder, activeTab]); // eslint-disable-line react-hooks/exhaustive-deps

  const { data: imagesData, isLoading, refetch: refetchImages } = useGoldenImages(selectedNamespace || undefined);
  const { data: namespacesData } = useNamespaces();
  const { data: scData } = useStorageClasses();
  const { data: foldersData } = useFoldersFlat();
  const createMutation = useCreateGoldenImage();
  const deleteMutation = useDeleteGoldenImage();

  const projects = namespacesData?.items || [];
  const storageClasses = scData?.items || [];
  const allFolders = foldersData?.items ?? [];

  // Transform images data to include disk_type and persistent
  const disks: Disk[] = (imagesData?.items || []).map((img) => ({
    ...img,
    disk_type: (img as any).disk_type || 'image',
    persistent: (img as any).persistent || false,
  }));

  // Unique projects and environments for filtering
  const uniqueProjects = [...new Set(disks.map(d => d.project).filter(Boolean))] as string[];
  const uniqueEnvs = [...new Set(disks.map(d => d.environment).filter(Boolean))] as string[];

  // Split disks by type
  const imageDisks = disks.filter(d => d.disk_type === 'image');
  const dataDisks = disks.filter(d => d.disk_type === 'data');
  const currentDisks = activeTab === 'images' ? imageDisks : dataDisks;

  // Get namespaces that belong to the selected folder (including sub-folders)
  const folderNamespaces: Set<string> = filterFolder
    ? (() => {
        const ns = new Set<string>();
        const addFolder = (name: string) => {
          const f = allFolders.find((x) => x.name === name);
          if (!f) return;
          f.environments.forEach((e) => ns.add(e.name));
          f.children.forEach((c) => addFolder(c.name));
        };
        addFolder(filterFolder);
        return ns;
      })()
    : new Set<string>();

  // Filter disks
  const filteredDisks = currentDisks.filter((disk) => {
    const matchesSearch = disk.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (disk.display_name?.toLowerCase().includes(searchQuery.toLowerCase()));
    const matchesProject = !filterProject || disk.project === filterProject;
    const matchesEnv = !filterEnv || disk.environment === filterEnv;
    const matchesFolder = !filterFolder || folderNamespaces.has(disk.namespace);
    return matchesSearch && matchesProject && matchesEnv && matchesFolder;
  });

  const paginatedDisks = filteredDisks.slice((page - 1) * perPage, page * perPage);
  const totalPages = Math.max(1, Math.ceil(filteredDisks.length / perPage));

  // Map backend status to display status
  const mapStatus = (backendStatus: string, usedBy?: string[] | null): 'Ready' | 'InUse' | 'Pending' | 'Error' => {
    const status = backendStatus.toLowerCase();
    if (status === 'error' || status === 'failed') return 'Error';
    if (usedBy && usedBy.length > 0) return 'InUse';
    if (status === 'succeeded' || status === 'ready') return 'Ready';
    if (status.includes('progress') || status.includes('pending') || status.includes('scheduled') || status === 'waitforfirstconsumer') return 'Pending';
    return 'Ready';
  };

  const handleCreate = async (data: GoldenImageCreate & { disk_type: DiskType; persistent: boolean }, namespace: string) => {
    // Add labels for disk_type and persistent to the request
    const createData = {
      ...data,
      labels: {
        'kubevirt-ui.io/disk-type': data.disk_type,
        'kubevirt-ui.io/persistent': String(data.persistent),
      },
    };
    await createMutation.mutateAsync({ data: createData as any, namespace });
    setShowImportImageModal(false);
    setShowNewDiskModal(false);
  };

  const handleDelete = (disk: Disk) => {
    const status = mapStatus(disk.status, disk.used_by);
    if (status === 'InUse') {
      notify.error('Cannot Delete', 'Cannot delete disk that is in use by virtual machines.');
      return;
    }
    setDeleteModalDisk(disk);
  };

  const handleDeleteConfirm = async () => {
    if (!deleteModalDisk) return;
    await deleteMutation.mutateAsync({ name: deleteModalDisk.name, namespace: deleteModalDisk.namespace });
    setDeleteModalDisk(null);
  };

  const getStatusIcon = (status: 'Ready' | 'InUse' | 'Pending' | 'Error') => {
    switch (status) {
      case 'Ready':
        return <CheckCircle2 className="h-4 w-4 text-emerald-400" />;
      case 'InUse':
        return <CircleDot className="h-4 w-4 text-primary-400" />;
      case 'Pending':
        return <Clock className="h-4 w-4 text-amber-400" />;
      case 'Error':
        return <AlertTriangle className="h-4 w-4 text-red-400" />;
    }
  };

  const getStatusBadge = (status: 'Ready' | 'InUse' | 'Pending' | 'Error') => {
    const styles = {
      Ready: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
      InUse: 'bg-primary-500/10 text-primary-400 border-primary-500/30',
      Pending: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
      Error: 'bg-red-500/10 text-red-400 border-red-500/30',
    };
    return (
      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${styles[status]}`}>
        {getStatusIcon(status)}
        {status}
      </span>
    );
  };

  const getTypeIcon = (type: DiskType) => {
    return type === 'image' ? (
      <Image className="h-4 w-4 text-amber-400" />
    ) : (
      <Database className="h-4 w-4 text-blue-400" />
    );
  };

  // Stats
  const stats = {
    total: disks.length,
    images: disks.filter(d => d.disk_type === 'image').length,
    data: disks.filter(d => d.disk_type === 'data').length,
    persistent: disks.filter(d => d.persistent).length,
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-surface-100">Storage</h1>
          <p className="text-surface-400 mt-1">
            Manage images and data disks for your virtual machines
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => refetchImages()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          <button className="btn-secondary" onClick={() => setShowNewDiskModal(true)}>
            <Plus className="h-4 w-4" />
            Create Disk
          </button>
          <button className="btn-primary" onClick={() => setShowImportImageModal(true)}>
            <Plus className="h-4 w-4" />
            Import Image
          </button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="card">
          <div className="card-body flex items-center gap-4 py-4">
            <div className="rounded-xl p-3 bg-surface-700">
              <HardDrive className="h-5 w-5 text-surface-300" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{stats.total}</p>
              <p className="text-sm text-surface-400">Total Disks</p>
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-body flex items-center gap-4 py-4">
            <div className="rounded-xl p-3 bg-amber-500/10">
              <Image className="h-5 w-5 text-amber-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{stats.images}</p>
              <p className="text-sm text-surface-400">Images</p>
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-body flex items-center gap-4 py-4">
            <div className="rounded-xl p-3 bg-blue-500/10">
              <Database className="h-5 w-5 text-blue-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{stats.data}</p>
              <p className="text-sm text-surface-400">Data Disks</p>
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-body flex items-center gap-4 py-4">
            <div className="rounded-xl p-3 bg-purple-500/10">
              <Lock className="h-5 w-5 text-purple-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{stats.persistent}</p>
              <p className="text-sm text-surface-400">Persistent</p>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-6 border-b border-surface-700">
        <button
          onClick={() => setActiveTab('images')}
          className={clsx(
            'flex items-center gap-2 pb-3 px-1 text-sm font-medium border-b-2 transition-colors',
            activeTab === 'images'
              ? 'border-amber-400 text-amber-400'
              : 'border-transparent text-surface-400 hover:text-surface-200'
          )}
        >
          <Image className="h-4 w-4" />
          Images
          <span className={clsx(
            'px-1.5 py-0.5 rounded-full text-xs',
            activeTab === 'images' ? 'bg-amber-500/20 text-amber-400' : 'bg-surface-700 text-surface-400'
          )}>{imageDisks.length}</span>
        </button>
        <button
          onClick={() => setActiveTab('data')}
          className={clsx(
            'flex items-center gap-2 pb-3 px-1 text-sm font-medium border-b-2 transition-colors',
            activeTab === 'data'
              ? 'border-blue-400 text-blue-400'
              : 'border-transparent text-surface-400 hover:text-surface-200'
          )}
        >
          <Database className="h-4 w-4" />
          Data Disks
          <span className={clsx(
            'px-1.5 py-0.5 rounded-full text-xs',
            activeTab === 'data' ? 'bg-blue-500/20 text-blue-400' : 'bg-surface-700 text-surface-400'
          )}>{dataDisks.length}</span>
        </button>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-surface-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={activeTab === 'images' ? 'Search images...' : 'Search data disks...'}
            className="w-full pl-9 pr-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm"
          />
        </div>
        {allFolders.length > 0 && (
          <CustomSelect
            value={filterFolder}
            onChange={(v) => { setFilterFolder(v); setFilterProject(''); setFilterEnv(''); }}
            placeholder="All folders"
            options={[
              { value: '', label: 'All folders' },
              ...allFolders.map((f) => ({
                value: f.name,
                label: f.path.length > 0 ? `${f.path.join(' › ')} › ${f.display_name}` : f.display_name,
              })),
            ]}
          />
        )}
        {!filterFolder && uniqueProjects.length > 0 && (
          <CustomSelect
            value={filterProject}
            onChange={setFilterProject}
            placeholder="All projects"
            options={[{ value: '', label: 'All projects' }, ...uniqueProjects.map(p => ({ value: p, label: p }))]}
          />
        )}
        {!filterFolder && uniqueEnvs.length > 0 && (
          <CustomSelect
            value={filterEnv}
            onChange={setFilterEnv}
            placeholder="All environments"
            options={[{ value: '', label: 'All environments' }, ...uniqueEnvs.map(e => ({ value: e, label: e }))]}
          />
        )}
      </div>

      {/* Active folder filter breadcrumb */}
      {filterFolder && (() => {
        const f = allFolders.find((x) => x.name === filterFolder);
        if (!f) return null;
        return (
          <div className="flex items-center gap-2 text-sm text-surface-400">
            <span>Showing images in:</span>
            <FolderBreadcrumb folder={f} allFolders={allFolders} />
            <button
              onClick={() => setFilterFolder('')}
              className="text-surface-500 hover:text-surface-300 ml-1"
              title="Clear folder filter"
            >
              ×
            </button>
          </div>
        );
      })()}

      {/* Disks Table */}
      {isLoading ? (
        <LoadingSkeleton rows={6} columns={5} />
      ) : filteredDisks.length === 0 ? (
        <div className="card">
          <div className="card-body text-center py-16">
            <HardDrive className="h-16 w-16 mx-auto text-surface-600 mb-4" />
            <h3 className="text-lg font-semibold text-surface-100 mb-2">
              {disks.length === 0 ? 'No disks yet' : 'No disks match your filter'}
            </h3>
            <p className="text-surface-400 mb-6 max-w-md mx-auto">
              {disks.length === 0
                ? 'Import base OS images or create data disks for your virtual machines.'
                : 'Try adjusting your search or filter criteria.'}
            </p>
            {disks.length === 0 && (
              <div className="flex gap-3 justify-center">
                <button className="btn-secondary" onClick={() => setShowNewDiskModal(true)}>
                  <Plus className="h-4 w-4" />
                  Create Disk
                </button>
                <button className="btn-primary" onClick={() => setShowImportImageModal(true)}>
                  <Plus className="h-4 w-4" />
                  Import Image
                </button>
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full">
            <thead className="bg-surface-800/50">
              <tr>
                <th className="table-header">Name</th>
                <th className="table-header">Namespace</th>
                <th className="table-header">Size</th>
                <th className="table-header">Status</th>
                <th className="table-header">VMs</th>
                <th className="table-header">Scope</th>
                <th className="table-header text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {paginatedDisks.map((disk) => {
                const status = mapStatus(disk.status, disk.used_by);
                const vmCount = disk.used_by?.length || 0;
                return (
                  <tr
                    key={`${disk.namespace}-${disk.name}`}
                    className="hover:bg-surface-800/30 cursor-pointer"
                    onClick={() => navigate(`/storage/${disk.namespace}/${disk.name}`)}
                  >
                    <td className="table-cell">
                      <div className="flex items-center gap-3">
                        <div className={clsx(
                          'p-2 rounded-lg',
                          disk.disk_type === 'image' ? 'bg-amber-500/10' : 'bg-blue-500/10'
                        )}>
                          {getTypeIcon(disk.disk_type)}
                        </div>
                        <div>
                          <p className="font-medium text-surface-100">
                            {disk.display_name || disk.name}
                          </p>
                          <p className="text-xs text-surface-500">{disk.name}</p>
                        </div>
                      </div>
                    </td>
                    <td className="table-cell">
                      <span className="text-surface-400 text-xs font-mono">{disk.namespace}</span>
                    </td>
                    <td className="table-cell">
                      <span className="text-surface-300 text-sm">{disk.size || '-'}</span>
                    </td>
                    <td className="table-cell">
                      <div title={status === 'Error' && disk.error_message ? disk.error_message : undefined}>
                        {getStatusBadge(status)}
                      </div>
                    </td>
                    <td className="table-cell">
                      {vmCount > 0 ? (
                        <span className="inline-flex items-center gap-1.5 text-sm text-primary-400">
                          <Monitor className="h-3.5 w-3.5" />
                          {vmCount} VM{vmCount > 1 ? 's' : ''}
                        </span>
                      ) : (
                        <span className="text-surface-500 text-sm">-</span>
                      )}
                    </td>
                    <td className="table-cell">
                      {disk.scope === 'project' ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-cyan-500/10 text-cyan-400">
                          <Layers className="h-3 w-3" />
                          All envs
                        </span>
                      ) : (
                        <span className="text-surface-400 text-xs">{disk.environment || 'env'}</span>
                      )}
                    </td>
                    <td className="table-cell text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDelete(disk);
                        }}
                        disabled={status === 'InUse'}
                        className={clsx(
                          'p-1.5 rounded-lg',
                          status === 'InUse'
                            ? 'text-surface-600 cursor-not-allowed'
                            : 'text-surface-400 hover:text-red-400 hover:bg-red-500/10'
                        )}
                        title={status === 'InUse' ? 'Cannot delete - in use' : 'Delete'}
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <Pagination
            page={page}
            totalPages={totalPages}
            onPageChange={setPage}
            perPage={perPage}
            onPerPageChange={setPerPage}
            total={filteredDisks.length}
          />
        </div>
      )}

      {/* Import Image Modal */}
      {showImportImageModal && (
        <ImportImageModal
          projects={projects.map(p => ({ name: p.name, display_name: (p as any).display_name || p.name }))}
          storageClasses={storageClasses}
          defaultProject={selectedNamespace}
          onClose={() => setShowImportImageModal(false)}
          onSubmit={handleCreate}
          isLoading={createMutation.isPending}
        />
      )}

      {/* New Disk Modal */}
      {showNewDiskModal && (
        <NewDiskModal
          projects={projects.map(p => ({ name: p.name, display_name: (p as any).display_name || p.name }))}
          storageClasses={storageClasses}
          existingDisks={disks}
          defaultProject={selectedNamespace}
          onClose={() => setShowNewDiskModal(false)}
          onSubmit={handleCreate}
          isLoading={createMutation.isPending}
        />
      )}

      <ConfirmDeleteModal
        isOpen={!!deleteModalDisk}
        onClose={() => setDeleteModalDisk(null)}
        onConfirm={handleDeleteConfirm}
        resourceName={deleteModalDisk?.name ?? ''}
        resourceType={deleteModalDisk?.disk_type === 'image' ? 'Image' : 'Disk'}
        isDeleting={deleteMutation.isPending}
      />
    </div>
  );
}

// Import Image Modal
function ImportImageModal({
  projects,
  storageClasses,
  defaultProject,
  onClose,
  onSubmit,
  isLoading,
}: {
  projects: { name: string; display_name?: string }[];
  storageClasses: { name: string; is_default: boolean }[];
  defaultProject?: string;
  onClose: () => void;
  onSubmit: (data: GoldenImageCreate & { disk_type: DiskType; persistent: boolean }, namespace: string) => Promise<void>;
  isLoading: boolean;
}) {
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [description] = useState('');
  const [osType, setOsType] = useState('linux');
  const [size, setSize] = useState('10Gi');
  const [storageClass] = useState('');
  const [sourceType, setSourceType] = useState<'http' | 'registry'>('http');
  const [sourceUrl, setSourceUrl] = useState('');
  const [selectedProject, setSelectedProject] = useState(defaultProject || '');
  const [persistent, setPersistent] = useState(false);
  const [scope, setScope] = useState<ImageScope>('environment');

  const defaultSC = storageClasses.find((sc) => sc.is_default)?.name || '';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedProject) return;

    const data: GoldenImageCreate & { disk_type: DiskType; persistent: boolean } = {
      name: name || undefined,
      display_name: displayName || name || undefined,
      description: description || undefined,
      os_type: osType,
      size,
      storage_class: storageClass || defaultSC || undefined,
      disk_type: 'image',
      persistent,
      scope,
    };

    if (sourceType === 'http') {
      data.source_url = sourceUrl;
    } else {
      data.source_registry = sourceUrl;
    }

    await onSubmit(data, selectedProject);
  };

  const isValid = (name.length > 0 || displayName.length > 0) && sourceUrl.length > 0 && selectedProject.length > 0;

  const popularImages = [
    { name: 'Ubuntu 22.04', url: 'https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img' },
    { name: 'Debian 12', url: 'https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2' },
    { name: 'Alpine 3.19', url: 'https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/cloud/nocloud_alpine-3.19.1-x86_64-bios-cloudinit-r0.qcow2' },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700 sticky top-0 bg-surface-800">
          <h2 className="text-lg font-semibold text-surface-100">
            <Image className="w-5 h-5 inline mr-2 text-amber-400" />
            Import Image
          </h2>
          <button onClick={onClose} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg">
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {/* Project Selection */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">
              Project *
            </label>
            <CustomSelect
              value={selectedProject}
              onChange={setSelectedProject}
              placeholder="Select a project..."
              options={[{ value: '', label: 'Select a project...' }, ...projects.map(p => ({ value: p.name, label: p.display_name || p.name }))]}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Name *</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                placeholder="ubuntu-22-04"
                className="input w-full"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Display Name</label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Ubuntu 22.04 LTS"
                className="input w-full"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">OS Type</label>
              <CustomSelect
                value={osType}
                onChange={setOsType}
                options={[{ value: 'linux', label: 'Linux' }, { value: 'windows', label: 'Windows' }]}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Size</label>
              <input
                type="text"
                value={size}
                onChange={(e) => setSize(e.target.value)}
                placeholder="10Gi"
                className="input w-full"
                required
              />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">Source Type</label>
            <CustomSelect
              value={sourceType}
              onChange={(v) => setSourceType(v as 'http' | 'registry')}
              options={[{ value: 'http', label: 'HTTP/HTTPS URL' }, { value: 'registry', label: 'Container Registry' }]}
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">Image URL *</label>
            <input
              type="text"
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              placeholder={sourceType === 'http' ? 'https://cloud-images.ubuntu.com/...' : 'docker://quay.io/...'}
              className="input w-full"
              required
            />
          </div>

          {/* Popular Images */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">Popular Images</label>
            <div className="flex flex-wrap gap-2">
              {popularImages.map((img) => (
                <button
                  key={img.name}
                  type="button"
                  onClick={() => {
                    setSourceUrl(img.url);
                    setDisplayName(img.name);
                  }}
                  className="px-3 py-1.5 text-sm bg-surface-700 hover:bg-surface-600 text-surface-300 rounded-md transition-colors"
                >
                  {img.name}
                </button>
              ))}
            </div>
          </div>

          {/* Scope Toggle */}
          <div className="p-4 bg-surface-900/50 rounded-lg border border-surface-700 space-y-3">
            <div className="text-sm font-medium text-surface-300 mb-2">Availability</div>
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="radio"
                checked={scope === 'environment'}
                onChange={() => setScope('environment')}
                className="mt-0.5 text-primary-500 focus:ring-primary-500"
              />
              <div>
                <div className="flex items-center gap-2">
                  <Folder className="h-4 w-4 text-emerald-400" />
                  <span className="font-medium text-surface-200">This environment only</span>
                </div>
                <p className="text-sm text-surface-400 mt-0.5">
                  Image available only in the selected namespace.
                </p>
              </div>
            </label>
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="radio"
                checked={scope === 'project'}
                onChange={() => setScope('project')}
                className="mt-0.5 text-primary-500 focus:ring-primary-500"
              />
              <div>
                <div className="flex items-center gap-2">
                  <Layers className="h-4 w-4 text-cyan-400" />
                  <span className="font-medium text-surface-200">Entire project</span>
                </div>
                <p className="text-sm text-surface-400 mt-0.5">
                  Image available to all environments in this project.
                </p>
              </div>
            </label>
          </div>

          {/* Persistent Toggle */}
          <div className="p-4 bg-surface-900/50 rounded-lg border border-surface-700">
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={persistent}
                onChange={(e) => setPersistent(e.target.checked)}
                className="mt-1 rounded bg-surface-700 border-surface-600 text-primary-500 focus:ring-primary-500"
              />
              <div>
                <div className="flex items-center gap-2">
                  <Lock className="h-4 w-4 text-purple-400" />
                  <span className="font-medium text-surface-200">Persistent</span>
                </div>
                <p className="text-sm text-surface-400 mt-0.5">
                  Use directly without cloning. Only one VM can use this image.
                </p>
              </div>
            </label>
          </div>

          <div className="flex justify-end gap-3 pt-4 border-t border-surface-700">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={!isValid || isLoading} className="btn-primary">
              {isLoading && <Loader2 className="h-4 w-4 animate-spin" />}
              Import Image
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// New Disk Modal
function NewDiskModal({
  projects,
  storageClasses,
  existingDisks,
  defaultProject,
  onClose,
  onSubmit,
  isLoading,
}: {
  projects: { name: string; display_name?: string }[];
  storageClasses: { name: string; is_default: boolean }[];
  existingDisks: Disk[];
  defaultProject?: string;
  onClose: () => void;
  onSubmit: (data: GoldenImageCreate & { disk_type: DiskType; persistent: boolean }, namespace: string) => Promise<void>;
  isLoading: boolean;
}) {
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [size, setSize] = useState('50Gi');
  const [storageClass, setStorageClass] = useState('');
  const [selectedProject, setSelectedProject] = useState(defaultProject || '');
  const [persistent, setPersistent] = useState(true);
  const [sourceType, setSourceType] = useState<'blank' | 'clone'>('blank');
  const [cloneFrom, setCloneFrom] = useState('');
  const [scope, setScope] = useState<ImageScope>('environment');

  const defaultSC = storageClasses.find((sc) => sc.is_default)?.name || '';

  // Get disks from selected project for cloning
  const cloneableDisk = existingDisks.filter(d => d.namespace === selectedProject && !d.persistent);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedProject) return;

    const data: GoldenImageCreate & { disk_type: DiskType; persistent: boolean } = {
      name: name || undefined,
      display_name: displayName || name || undefined,
      size,
      storage_class: storageClass || defaultSC || undefined,
      disk_type: 'data',
      persistent,
      scope,
    };

    if (sourceType === 'clone' && cloneFrom) {
      // Clone from existing disk - use pvc source
      (data as any).source_pvc = cloneFrom;
      (data as any).source_pvc_namespace = selectedProject;
    }
    // If blank, no source needed - backend will create blank volume

    await onSubmit(data, selectedProject);
  };

  const isValid = name.length > 0 && selectedProject.length > 0 && (sourceType === 'blank' || cloneFrom.length > 0);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700 sticky top-0 bg-surface-800">
          <h2 className="text-lg font-semibold text-surface-100">
            <Database className="w-5 h-5 inline mr-2 text-blue-400" />
            New Data Disk
          </h2>
          <button onClick={onClose} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg">
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {/* Project Selection */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">
              Project *
            </label>
            <CustomSelect
              value={selectedProject}
              onChange={(v) => { setSelectedProject(v); setCloneFrom(''); }}
              placeholder="Select a project..."
              options={[{ value: '', label: 'Select a project...' }, ...projects.map(p => ({ value: p.name, label: p.display_name || p.name }))]}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Name *</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                placeholder="data-disk"
                className="input w-full"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Display Name</label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Database Storage"
                className="input w-full"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Size *</label>
              <input
                type="text"
                value={size}
                onChange={(e) => setSize(e.target.value)}
                placeholder="50Gi"
                className="input w-full"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Storage Class</label>
              <CustomSelect
                value={storageClass}
                onChange={setStorageClass}
                placeholder={`Default (${defaultSC || 'auto'})`}
                options={[{ value: '', label: `Default (${defaultSC || 'auto'})` }, ...storageClasses.map(sc => ({ value: sc.name, label: `${sc.name}${sc.is_default ? ' (default)' : ''}` }))]}
              />
            </div>
          </div>

          {/* Source Selection */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">Source</label>
            <div className="space-y-2">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  checked={sourceType === 'blank'}
                  onChange={() => setSourceType('blank')}
                  className="text-primary-500 focus:ring-primary-500"
                />
                <span className="text-surface-200">Empty (blank disk)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  checked={sourceType === 'clone'}
                  onChange={() => setSourceType('clone')}
                  className="text-primary-500 focus:ring-primary-500"
                />
                <span className="text-surface-200">Clone from existing disk</span>
              </label>
            </div>
          </div>

          {sourceType === 'clone' && (
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Clone From *</label>
              <CustomSelect
                value={cloneFrom}
                onChange={setCloneFrom}
                disabled={!selectedProject}
                placeholder={selectedProject ? 'Select a disk...' : 'Select project first'}
                options={[{ value: '', label: selectedProject ? 'Select a disk...' : 'Select project first' }, ...cloneableDisk.map(d => ({ value: d.name, label: `${d.display_name || d.name} (${d.size})` }))]}
              />
              {selectedProject && cloneableDisk.length === 0 && (
                <p className="text-xs text-amber-400 mt-1">No cloneable disks in this project</p>
              )}
            </div>
          )}

          {/* Scope Toggle */}
          <div className="p-4 bg-surface-900/50 rounded-lg border border-surface-700 space-y-3">
            <div className="text-sm font-medium text-surface-300 mb-2">Availability</div>
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="radio"
                checked={scope === 'environment'}
                onChange={() => setScope('environment')}
                className="mt-0.5 text-primary-500 focus:ring-primary-500"
              />
              <div>
                <div className="flex items-center gap-2">
                  <Folder className="h-4 w-4 text-emerald-400" />
                  <span className="font-medium text-surface-200">This environment only</span>
                </div>
                <p className="text-sm text-surface-400 mt-0.5">
                  Disk available only in the selected namespace.
                </p>
              </div>
            </label>
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="radio"
                checked={scope === 'project'}
                onChange={() => setScope('project')}
                className="mt-0.5 text-primary-500 focus:ring-primary-500"
              />
              <div>
                <div className="flex items-center gap-2">
                  <Layers className="h-4 w-4 text-cyan-400" />
                  <span className="font-medium text-surface-200">Entire project</span>
                </div>
                <p className="text-sm text-surface-400 mt-0.5">
                  Disk available to all environments in this project.
                </p>
              </div>
            </label>
          </div>

          {/* Persistent Toggle */}
          <div className="p-4 bg-surface-900/50 rounded-lg border border-surface-700">
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={persistent}
                onChange={(e) => setPersistent(e.target.checked)}
                className="mt-1 rounded bg-surface-700 border-surface-600 text-primary-500 focus:ring-primary-500"
              />
              <div>
                <div className="flex items-center gap-2">
                  <Lock className="h-4 w-4 text-purple-400" />
                  <span className="font-medium text-surface-200">Persistent</span>
                </div>
                <p className="text-sm text-surface-400 mt-0.5">
                  {persistent
                    ? 'Disk will be attached directly. Not deleted with VM.'
                    : 'Disk will be cloned for each VM. Clone is deleted with VM.'}
                </p>
              </div>
            </label>
          </div>

          <div className="flex justify-end gap-3 pt-4 border-t border-surface-700">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={!isValid || isLoading} className="btn-primary">
              {isLoading && <Loader2 className="h-4 w-4 animate-spin" />}
              Create Disk
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

