/**
 * Image Detail page — replaces the old DiskDetailModal.
 * Shows image info, scope toggle, and Used By VMs tab.
 */

import { useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import {
  ArrowLeft,
  Image,
  Database,
  Loader2,
  Lock,
  Folder,
  Layers,
  Trash2,
  ExternalLink,
  Monitor,
  CheckCircle2,
  CircleDot,
  Clock,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import { useGoldenImages, useUpdateGoldenImage, useDeleteGoldenImage } from '../hooks/useTemplates';
import { useAppStore } from '../store';
import { ConfirmDeleteModal } from '../components/common/ConfirmDeleteModal';
import { notify } from '../store/notifications';
import type { GoldenImage, ImageScope } from '../types/template';

type Tab = 'overview' | 'vms';

export function ImageDetail() {
  const { namespace, name } = useParams<{ namespace: string; name: string }>();
  const navigate = useNavigate();
  const { selectedNamespace } = useAppStore();
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [isDeleteModalOpen, setIsDeleteModalOpen] = useState(false);

  const { data: imagesData, isLoading } = useGoldenImages(selectedNamespace || undefined);
  const updateScopeMutation = useUpdateGoldenImage();
  const deleteMutation = useDeleteGoldenImage();

  // Find the specific image
  const image: GoldenImage | undefined = (imagesData?.items || []).find(
    (img) => img.namespace === namespace && img.name === name
  );

  const handleUpdateScope = (scope: ImageScope) => {
    if (!image || image.scope === scope) return;
    updateScopeMutation.mutate({
      name: image.name,
      namespace: image.namespace,
      data: { scope },
    });
  };

  const handleDelete = () => {
    if (!image) return;
    if (image.used_by && image.used_by.length > 0) {
      notify.error('Cannot Delete', 'Cannot delete image that is in use by virtual machines.');
      return;
    }
    setIsDeleteModalOpen(true);
  };

  const handleDeleteConfirm = async () => {
    if (!image) return;
    await deleteMutation.mutateAsync({ name: image.name, namespace: image.namespace });
    setIsDeleteModalOpen(false);
    navigate('/storage');
  };

  const getStatus = (img: GoldenImage): 'Ready' | 'InUse' | 'Pending' | 'Error' => {
    const s = img.status.toLowerCase();
    if (s === 'error' || s === 'failed') return 'Error';
    if (img.used_by && img.used_by.length > 0) return 'InUse';
    if (s === 'succeeded' || s === 'ready') return 'Ready';
    if (s.includes('progress') || s.includes('pending') || s.includes('scheduled') || s === 'waitforfirstconsumer') return 'Pending';
    return 'Ready';
  };

  const getStatusBadge = (status: 'Ready' | 'InUse' | 'Pending' | 'Error') => {
    const styles = {
      Ready: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
      InUse: 'bg-primary-500/10 text-primary-400 border-primary-500/30',
      Pending: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
      Error: 'bg-red-500/10 text-red-400 border-red-500/30',
    };
    const icons = {
      Ready: <CheckCircle2 className="h-4 w-4" />,
      InUse: <CircleDot className="h-4 w-4" />,
      Pending: <Clock className="h-4 w-4" />,
      Error: <AlertTriangle className="h-4 w-4" />,
    };
    return (
      <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-sm font-medium border ${styles[status]}`}>
        {icons[status]}
        {status}
      </span>
    );
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary-400" />
      </div>
    );
  }

  if (!image) {
    return (
      <div className="space-y-6">
        <button onClick={() => navigate('/storage')} className="flex items-center gap-2 text-surface-400 hover:text-surface-200">
          <ArrowLeft className="h-4 w-4" />
          Back to Storage
        </button>
        <div className="card">
          <div className="card-body text-center py-16">
            <p className="text-surface-400">Image not found: {namespace}/{name}</p>
          </div>
        </div>
      </div>
    );
  }

  const status = getStatus(image);
  const isImage = image.disk_type === 'image';
  const vmCount = image.used_by?.length || 0;

  return (
    <div className="space-y-6">
      {/* Back + Header */}
      <button onClick={() => navigate('/storage')} className="flex items-center gap-2 text-surface-400 hover:text-surface-200 transition-colors">
        <ArrowLeft className="h-4 w-4" />
        Back to Storage
      </button>

      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className={clsx(
            'p-3 rounded-xl',
            isImage ? 'bg-amber-500/10' : 'bg-blue-500/10'
          )}>
            {isImage ? (
              <Image className="h-8 w-8 text-amber-400" />
            ) : (
              <Database className="h-8 w-8 text-blue-400" />
            )}
          </div>
          <div>
            <h2 className="text-2xl font-bold text-surface-100">
              {image.display_name || image.name}
            </h2>
            <p className="text-surface-400 mt-0.5">{image.name}</p>
          </div>
          <div className="ml-4">{getStatusBadge(status)}</div>
        </div>
        <button
          onClick={handleDelete}
          disabled={status === 'InUse' || deleteMutation.isPending}
          className={clsx(
            'btn-secondary',
            status === 'InUse' ? 'opacity-50 cursor-not-allowed' : 'hover:text-red-400 hover:border-red-500/30'
          )}
        >
          {deleteMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
          Delete
        </button>
      </div>

      {/* Tabs */}
      <div className="border-b border-surface-700">
        <nav className="flex gap-6">
          <button
            onClick={() => setActiveTab('overview')}
            className={clsx(
              'pb-3 text-sm font-medium border-b-2 transition-colors',
              activeTab === 'overview'
                ? 'border-primary-400 text-primary-400'
                : 'border-transparent text-surface-400 hover:text-surface-200'
            )}
          >
            Overview
          </button>
          <button
            onClick={() => setActiveTab('vms')}
            className={clsx(
              'pb-3 text-sm font-medium border-b-2 transition-colors flex items-center gap-2',
              activeTab === 'vms'
                ? 'border-primary-400 text-primary-400'
                : 'border-transparent text-surface-400 hover:text-surface-200'
            )}
          >
            <Monitor className="h-4 w-4" />
            Used by VMs
            {vmCount > 0 && (
              <span className="px-1.5 py-0.5 rounded-full text-xs bg-primary-500/20 text-primary-400">
                {vmCount}
              </span>
            )}
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left: Properties */}
          <div className="lg:col-span-2 space-y-6">
            <div className="card">
              <div className="card-body space-y-4">
                <h3 className="text-sm font-semibold text-surface-300 uppercase tracking-wider">Properties</h3>
                <div className="grid grid-cols-2 gap-y-4 gap-x-8">
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Project</label>
                    <p className="text-surface-100 mt-1">{image.project || '-'}</p>
                  </div>
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Environment</label>
                    <p className="text-surface-100 mt-1">
                      {image.scope === 'project' ? (
                        <span className="inline-flex items-center gap-1 text-cyan-400">
                          <Layers className="h-3.5 w-3.5" />
                          All environments
                        </span>
                      ) : (
                        image.environment || '-'
                      )}
                    </p>
                  </div>
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Type</label>
                    <p className="text-surface-100 mt-1 capitalize">{image.disk_type}</p>
                  </div>
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Size</label>
                    <p className="text-surface-100 mt-1">{image.size || 'Unknown'}</p>
                  </div>
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Namespace</label>
                    <p className="text-surface-400 mt-1 text-sm">{image.namespace}</p>
                  </div>
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Persistent</label>
                    <p className="text-surface-100 mt-1 flex items-center gap-1">
                      {image.persistent ? (
                        <>
                          <Lock className="h-4 w-4 text-purple-400" />
                          Yes
                        </>
                      ) : (
                        'No (cloned per VM)'
                      )}
                    </p>
                  </div>
                  {image.os_type && (
                    <div>
                      <label className="text-xs text-surface-500 uppercase tracking-wider">OS Type</label>
                      <p className="text-surface-100 mt-1 capitalize">{image.os_type}</p>
                    </div>
                  )}
                  {image.created && (
                    <div>
                      <label className="text-xs text-surface-500 uppercase tracking-wider">Created</label>
                      <p className="text-surface-400 mt-1 text-sm">{new Date(image.created).toLocaleString()}</p>
                    </div>
                  )}
                  <div>
                    <label className="text-xs text-surface-500 uppercase tracking-wider">VMs using this image</label>
                    <p className="text-surface-100 mt-1">
                      {vmCount > 0 ? `${vmCount} VM${vmCount > 1 ? 's' : ''}` : '-'}
                    </p>
                  </div>
                </div>
                {image.source_url && (
                  <div className="pt-2 border-t border-surface-700">
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Source URL</label>
                    <p className="text-surface-300 mt-1 text-sm break-all">{image.source_url}</p>
                  </div>
                )}
                {image.description && (
                  <div className="pt-2 border-t border-surface-700">
                    <label className="text-xs text-surface-500 uppercase tracking-wider">Description</label>
                    <p className="text-surface-300 mt-1 text-sm">{image.description}</p>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Right: Scope Toggle */}
          <div className="space-y-6">
            <div className="card">
              <div className="card-body space-y-4">
                <h3 className="text-sm font-semibold text-surface-300 uppercase tracking-wider">Availability Scope</h3>
                <p className="text-xs text-surface-500">
                  Control where this image can be used for creating VMs.
                </p>
                <div className="space-y-2">
                  <button
                    type="button"
                    disabled={updateScopeMutation.isPending}
                    onClick={() => handleUpdateScope('environment')}
                    className={clsx(
                      'w-full flex items-center gap-3 px-4 py-3 rounded-lg border text-sm font-medium transition-all text-left',
                      image.scope === 'environment'
                        ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                        : 'border-surface-600 bg-surface-800 text-surface-400 hover:border-surface-500 hover:text-surface-300 cursor-pointer'
                    )}
                  >
                    <Folder className="h-5 w-5 flex-shrink-0" />
                    <div>
                      <div>This environment only</div>
                      <div className="text-xs opacity-70 mt-0.5">Available only in {image.namespace}</div>
                    </div>
                  </button>
                  <button
                    type="button"
                    disabled={updateScopeMutation.isPending}
                    onClick={() => handleUpdateScope('project')}
                    className={clsx(
                      'w-full flex items-center gap-3 px-4 py-3 rounded-lg border text-sm font-medium transition-all text-left',
                      image.scope === 'project'
                        ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-400'
                        : 'border-surface-600 bg-surface-800 text-surface-400 hover:border-surface-500 hover:text-surface-300 cursor-pointer'
                    )}
                  >
                    {updateScopeMutation.isPending ? (
                      <Loader2 className="h-5 w-5 flex-shrink-0 animate-spin" />
                    ) : (
                      <Layers className="h-5 w-5 flex-shrink-0" />
                    )}
                    <div>
                      <div>Entire project</div>
                      <div className="text-xs opacity-70 mt-0.5">All environments in {image.project || 'project'}</div>
                    </div>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      ) : (
        /* VMs Tab */
        <div className="card">
          {vmCount === 0 ? (
            <div className="card-body text-center py-12">
              <Monitor className="h-12 w-12 mx-auto text-surface-600 mb-3" />
              <p className="text-surface-400">No virtual machines are using this image.</p>
            </div>
          ) : (
            <table className="w-full">
              <thead className="bg-surface-800/50">
                <tr>
                  <th className="table-header">VM Name</th>
                  <th className="table-header">Namespace</th>
                  <th className="table-header text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-800">
                {(image.used_by || []).map((vmRef) => {
                  // vmRef format: "namespace/name"
                  const parts = vmRef.split('/');
                  const vmNs = parts.length > 1 ? parts[0] : namespace || '';
                  const vmName = parts.length > 1 ? parts[1] : vmRef;

                  return (
                    <tr key={vmRef} className="hover:bg-surface-800/30">
                      <td className="table-cell">
                        <div className="flex items-center gap-2">
                          <Monitor className="h-4 w-4 text-primary-400" />
                          <Link
                            to={`/vms/${vmNs}/${vmName}`}
                            className="text-primary-400 hover:text-primary-300 font-medium"
                          >
                            {vmName}
                          </Link>
                        </div>
                      </td>
                      <td className="table-cell text-surface-400">{vmNs}</td>
                      <td className="table-cell text-right">
                        <Link
                          to={`/vms/${vmNs}/${vmName}`}
                          className="inline-flex items-center gap-1.5 text-sm text-surface-400 hover:text-primary-400 transition-colors"
                        >
                          <ExternalLink className="h-3.5 w-3.5" />
                          Open
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      <ConfirmDeleteModal
        isOpen={isDeleteModalOpen}
        onClose={() => setIsDeleteModalOpen(false)}
        onConfirm={handleDeleteConfirm}
        resourceName={image?.display_name || image?.name || ''}
        resourceType="Image"
        isDeleting={deleteMutation.isPending}
      />
    </div>
  );
}
