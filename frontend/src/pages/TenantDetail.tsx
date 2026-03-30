/**
 * Tenant Detail Page
 *
 * Shows tenant status, workers, kubeconfig, addons, conditions.
 */

import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  CheckCircle,
  XCircle,
  Clock,
  Loader2,
  Download,
  Copy,
  Trash2,
  Server,
  Cpu,
  RefreshCw,
  Settings,
  Power,
  PowerOff,
  Info,
  Image,
  AlertTriangle,
} from 'lucide-react';
import {
  useTenant,
  useDeleteTenant,
  useScaleTenant,
  useTenantKubeconfig,
  useAddonCatalog,
  useEnableAddon,
  useDisableAddon,
  useTenantImages,
  useDeleteTenantImage,
} from '../hooks/useTenants';
import { ConfirmDeleteModal } from '../components/common/ConfirmDeleteModal';
import type { TenantAddonStatus, TenantCondition } from '../types/tenant';
import type { GoldenImage } from '../types/template';

function StatusBadge({ status }: { status: string }) {
  const config: Record<string, { icon: typeof CheckCircle; color: string }> = {
    Ready: { icon: CheckCircle, color: 'text-emerald-400 bg-emerald-500/10' },
    Provisioning: { icon: Clock, color: 'text-amber-400 bg-amber-500/10' },
    Failed: { icon: XCircle, color: 'text-red-400 bg-red-500/10' },
    Deleting: { icon: Loader2, color: 'text-surface-400 bg-surface-500/10' },
  };
  const entry = config[status] ?? config.Provisioning;
  const Icon = entry!.icon;
  const color = entry!.color;

  return (
    <span className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium ${color}`}>
      <Icon className={`h-4 w-4 ${status === 'Provisioning' || status === 'Deleting' ? 'animate-spin' : ''}`} />
      {status}
    </span>
  );
}

function ConditionRow({ condition }: { condition: TenantCondition }) {
  const isTrue = condition.status === 'True';
  return (
    <tr className="border-t border-surface-800">
      <td className="py-2 pr-4 text-sm text-surface-300">{condition.type}</td>
      <td className="py-2 pr-4">
        <span className={`text-xs font-medium ${isTrue ? 'text-emerald-400' : 'text-amber-400'}`}>
          {condition.status}
        </span>
      </td>
      <td className="py-2 pr-4 text-xs text-surface-400">{condition.reason}</td>
      <td className="py-2 text-xs text-surface-500">{condition.message}</td>
    </tr>
  );
}

type ActiveTab = 'overview' | 'images';

export default function TenantDetail() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deleteImageName, setDeleteImageName] = useState<string | null>(null);
  const [scaleCount, setScaleCount] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const [kubeconfigType, setKubeconfigType] = useState<'admin' | 'oidc'>('oidc');
  const [activeTab, setActiveTab] = useState<ActiveTab>('overview');

  const { data: tenant, isLoading, refetch } = useTenant(name);
  const { data: catalog } = useAddonCatalog();
  const { data: imagesData, isLoading: imagesLoading, refetch: refetchImages } = useTenantImages(name);
  const deleteMutation = useDeleteTenant();
  const scaleMutation = useScaleTenant();
  const deleteImageMutation = useDeleteTenantImage(name || '');
  const { refetch: fetchAdminKc, isFetching: adminKcLoading } = useTenantKubeconfig(name, 'admin');
  const { refetch: fetchOidcKc, isFetching: oidcKcLoading } = useTenantKubeconfig(name, 'oidc');
  const enableAddon = useEnableAddon(name || '');
  const disableAddon = useDisableAddon(name || '');

  const fetchKubeconfig = kubeconfigType === 'admin' ? fetchAdminKc : fetchOidcKc;
  const kubeconfigLoading = kubeconfigType === 'admin' ? adminKcLoading : oidcKcLoading;

  const handleDelete = async () => {
    if (!name) return;
    await deleteMutation.mutateAsync(name);
    navigate('/tenants');
  };

  const handleScale = async () => {
    if (!name || scaleCount === null) return;
    await scaleMutation.mutateAsync({ name, request: { worker_count: scaleCount } });
    setScaleCount(null);
  };

  const handleDownloadKubeconfig = async () => {
    const { data } = await fetchKubeconfig();
    if (data?.kubeconfig) {
      const blob = new Blob([data.kubeconfig], { type: 'text/yaml' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${name}-kubeconfig.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    }
  };

  const handleCopyKubeconfig = async () => {
    const { data } = await fetchKubeconfig();
    if (data?.kubeconfig) {
      await navigator.clipboard.writeText(data.kubeconfig);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleDeleteImage = (imageName: string) => {
    setDeleteImageName(imageName);
  };

  const handleDeleteImageConfirm = async () => {
    if (!deleteImageName) return;
    await deleteImageMutation.mutateAsync(deleteImageName);
    setDeleteImageName(null);
  };

  // Addons not yet enabled (from catalog)
  const enabledAddonIds = new Set(tenant?.addons.map(a => a.addon_id) || []);
  const availableAddons = catalog?.components.filter(c => !enabledAddonIds.has(c.id)) || [];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 text-primary-400 animate-spin" />
      </div>
    );
  }

  if (!tenant) {
    return (
      <div className="text-center py-16">
        <p className="text-surface-400">Tenant not found</p>
        <button onClick={() => navigate('/tenants')} className="text-primary-400 mt-2 text-sm">
          Back to Tenants
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/tenants')}
            className="text-surface-400 hover:text-surface-200 transition-colors"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div>
            <h1 className="text-2xl font-semibold text-surface-100">{tenant.display_name}</h1>
            <p className="text-sm text-surface-500">
              {tenant.name} · {tenant.namespace} · {tenant.kubernetes_version}
            </p>
          </div>
          <StatusBadge status={tenant.status} />
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          <button
            onClick={() => setShowDeleteModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-red-600/10 hover:bg-red-600/20 text-red-400 rounded-lg transition-colors text-sm"
          >
            <Trash2 className="h-4 w-4" />
            Delete
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-6 border-b border-surface-700">
        <button
          onClick={() => setActiveTab('overview')}
          className={`flex items-center gap-2 pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'overview'
              ? 'border-primary-400 text-primary-400'
              : 'border-transparent text-surface-400 hover:text-surface-200'
          }`}
        >
          <Server className="h-4 w-4" />
          Overview
        </button>
        <button
          onClick={() => { setActiveTab('images'); refetchImages(); }}
          className={`flex items-center gap-2 pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'images'
              ? 'border-amber-400 text-amber-400'
              : 'border-transparent text-surface-400 hover:text-surface-200'
          }`}
        >
          <Image className="h-4 w-4" />
          Images
          {imagesData && imagesData.total > 0 && (
            <span className={`px-1.5 py-0.5 rounded-full text-xs ${
              activeTab === 'images' ? 'bg-amber-500/20 text-amber-400' : 'bg-surface-700 text-surface-400'
            }`}>{imagesData.total}</span>
          )}
        </button>
      </div>

      {activeTab === 'images' && (
        <TenantImagesTab
          images={imagesData?.items || []}
          isLoading={imagesLoading}
          onDelete={handleDeleteImage}
          isDeleting={deleteImageMutation.isPending}
        />
      )}

      {activeTab === 'overview' && <>

      {/* Info cards */}
      <div className="grid grid-cols-4 gap-4">
        <div className="card">
          <div className="card-body text-center">
            <p className="text-xs text-surface-500 uppercase tracking-wider mb-1">Control Plane</p>
            <p className="text-lg font-semibold text-surface-100">
              {tenant.control_plane_ready ? (
                <span className="text-emerald-400">Ready</span>
              ) : (
                <span className="text-amber-400">Pending</span>
              )}
            </p>
            <p className="text-xs text-surface-500">{tenant.control_plane_replicas} replicas</p>
          </div>
        </div>
        <div className="card">
          <div className="card-body text-center">
            <p className="text-xs text-surface-500 uppercase tracking-wider mb-1">Workers</p>
            <p className="text-lg font-semibold text-surface-100">
              <span className={tenant.workers_ready === tenant.worker_count ? 'text-emerald-400' : 'text-amber-400'}>
                {tenant.workers_ready}
              </span>
              <span className="text-surface-500"> / {tenant.worker_count}</span>
            </p>
            <p className="text-xs text-surface-500">{tenant.worker_vcpu} vCPU, {tenant.worker_memory}</p>
          </div>
        </div>
        <div className="card">
          <div className="card-body text-center">
            <p className="text-xs text-surface-500 uppercase tracking-wider mb-1">Endpoint</p>
            <p className="text-xs font-mono text-primary-400 break-all">
              {tenant.endpoint?.replace('https://', '') || 'Pending...'}
            </p>
          </div>
        </div>
        <div className="card">
          <div className="card-body text-center">
            <p className="text-xs text-surface-500 uppercase tracking-wider mb-1">Created</p>
            <p className="text-sm text-surface-300">
              {tenant.created ? new Date(tenant.created).toLocaleDateString() : '-'}
            </p>
          </div>
        </div>
      </div>

      {/* CNI installation info */}
      {tenant.status === 'Ready' && tenant.workers_ready < tenant.worker_count && (() => {
        const cniAddon = tenant.addons.find(a => a.addon_id === 'calico' || a.addon_id === 'cilium');
        if (!cniAddon || !cniAddon.ready) {
          return (
            <div className="flex items-start gap-3 p-4 bg-sky-500/5 border border-sky-500/20 rounded-lg">
              <Info className="h-5 w-5 text-sky-400 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm text-sky-300 font-medium">
                  Installing CNI {cniAddon ? `(${cniAddon.addon_id})` : '(Calico)'}...
                </p>
                <p className="text-xs text-sky-400/70 mt-1">
                  Worker nodes will become Ready after CNI deployment completes.
                  {cniAddon?.message && ` Status: ${cniAddon.message}`}
                </p>
              </div>
            </div>
          );
        }
        return null;
      })()}

      {/* Workers + Scale */}
      <div className="card">
        <div className="card-body">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-surface-100 flex items-center gap-2">
              <Cpu className="h-5 w-5 text-primary-400" />
              Workers
            </h2>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1} max={20}
                placeholder={String(tenant.worker_count)}
                value={scaleCount ?? ''}
                onChange={e => setScaleCount(e.target.value ? parseInt(e.target.value) : null)}
                className="w-20 px-2 py-1 bg-surface-800 border border-surface-700 rounded text-sm text-surface-200 focus:outline-none focus:border-primary-500"
              />
              <button
                onClick={handleScale}
                disabled={scaleCount === null || scaleMutation.isPending}
                className="flex items-center gap-1 px-3 py-1 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 text-white rounded text-sm transition-colors"
              >
                {scaleMutation.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
                Scale
              </button>
            </div>
          </div>
          <p className="text-sm text-surface-400">
            {tenant.workers_ready} of {tenant.worker_count} workers ready ·{' '}
            {tenant.worker_vcpu} vCPU · {tenant.worker_memory} per worker
          </p>
        </div>
      </div>

      {/* Kubeconfig */}
      <div className="card">
        <div className="card-body">
          <h2 className="text-lg font-semibold text-surface-100 flex items-center gap-2 mb-4">
            <Settings className="h-5 w-5 text-primary-400" />
            Kubeconfig
          </h2>
          <div className="flex items-center gap-3 mb-4">
            <div className="flex bg-surface-800 rounded-lg p-0.5">
              <button
                onClick={() => setKubeconfigType('oidc')}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  kubeconfigType === 'oidc'
                    ? 'bg-primary-600 text-white'
                    : 'text-surface-400 hover:text-surface-200'
                }`}
              >
                OIDC (User)
              </button>
              <button
                onClick={() => setKubeconfigType('admin')}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  kubeconfigType === 'admin'
                    ? 'bg-primary-600 text-white'
                    : 'text-surface-400 hover:text-surface-200'
                }`}
              >
                Admin (Certificate)
              </button>
            </div>
          </div>
          <p className="text-xs text-surface-500 mb-3">
            {kubeconfigType === 'oidc'
              ? 'Uses your current OIDC token from DEX. Access governed by RBAC in tenant cluster.'
              : 'Certificate-based admin access. Full cluster-admin privileges.'}
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownloadKubeconfig}
              disabled={kubeconfigLoading}
              className="flex items-center gap-2 px-4 py-2 bg-surface-800 hover:bg-surface-700 border border-surface-700 rounded-lg text-sm text-surface-200 transition-colors"
            >
              {kubeconfigLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
              Download
            </button>
            <button
              onClick={handleCopyKubeconfig}
              disabled={kubeconfigLoading}
              className="flex items-center gap-2 px-4 py-2 bg-surface-800 hover:bg-surface-700 border border-surface-700 rounded-lg text-sm text-surface-200 transition-colors"
            >
              <Copy className="h-4 w-4" />
              {copied ? 'Copied!' : 'Copy to clipboard'}
            </button>
          </div>
        </div>
      </div>

      {/* Addons */}
      <div className="card">
        <div className="card-body">
          <h2 className="text-lg font-semibold text-surface-100 flex items-center gap-2 mb-4">
            <Server className="h-5 w-5 text-primary-400" />
            Addons
          </h2>
          <div className="space-y-2">
            {tenant.addons.map((addon: TenantAddonStatus) => {
              const catalogEntry = catalog?.components.find(c => c.id === addon.addon_id);
              return (
                <div key={addon.addon_id} className="flex items-center justify-between p-3 bg-surface-800 rounded-lg">
                  <div className="flex items-center gap-3">
                    {addon.ready ? (
                      <CheckCircle className="h-4 w-4 text-emerald-400" />
                    ) : (
                      <Clock className="h-4 w-4 text-amber-400 animate-pulse" />
                    )}
                    <div>
                      <span className="text-sm font-medium text-surface-200">
                        {catalogEntry?.name || addon.addon_id}
                      </span>
                      {addon.message && (
                        <p className="text-xs text-surface-500">{addon.message}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`text-xs ${addon.ready ? 'text-emerald-400' : 'text-amber-400'}`}>
                      {addon.ready ? 'Reconciled' : 'Reconciling...'}
                    </span>
                    {!(catalogEntry?.required) && (
                      <button
                        onClick={() => disableAddon.mutate(addon.addon_id)}
                        disabled={disableAddon.isPending}
                        className="text-surface-500 hover:text-red-400 transition-colors p-1"
                        title="Disable addon"
                      >
                        <PowerOff className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}

            {/* Available (not yet enabled) */}
            {availableAddons.length > 0 && (
              <div className="mt-3 pt-3 border-t border-surface-700">
                <p className="text-xs text-surface-500 mb-2">Available addons:</p>
                {availableAddons.map(c => (
                  <div key={c.id} className="flex items-center justify-between p-2 rounded hover:bg-surface-800/50">
                    <span className="text-sm text-surface-400">{c.name}</span>
                    <button
                      onClick={() => enableAddon.mutate({ addon_id: c.id, parameters: {} })}
                      disabled={enableAddon.isPending}
                      className="flex items-center gap-1 px-2 py-1 text-xs text-primary-400 hover:bg-primary-500/10 rounded transition-colors"
                    >
                      <Power className="h-3 w-3" />
                      Enable
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Conditions */}
      {tenant.conditions.length > 0 && (
        <div className="card">
          <div className="card-body">
            <h2 className="text-lg font-semibold text-surface-100 mb-4">Conditions</h2>
            <table className="w-full">
              <thead>
                <tr className="text-left text-xs text-surface-500 uppercase tracking-wider">
                  <th className="pb-2 pr-4">Type</th>
                  <th className="pb-2 pr-4">Status</th>
                  <th className="pb-2 pr-4">Reason</th>
                  <th className="pb-2">Message</th>
                </tr>
              </thead>
              <tbody>
                {tenant.conditions.map((c: TenantCondition) => (
                  <ConditionRow key={c.type} condition={c} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Delete Modal */}
      {showDeleteModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-surface-900 border border-surface-700 rounded-xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-lg font-semibold text-surface-100 mb-2">Delete Tenant</h3>
            <p className="text-sm text-surface-400 mb-4">
              Are you sure you want to delete tenant <span className="text-red-400 font-medium">{tenant.name}</span>?
              This will destroy the control plane, all worker VMs, and all data.
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowDeleteModal(false)} className="px-4 py-2 text-sm text-surface-300 hover:text-surface-100">
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={deleteMutation.isPending}
                className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium"
              >
                {deleteMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
      </>}

      <ConfirmDeleteModal
        isOpen={!!deleteImageName}
        onClose={() => setDeleteImageName(null)}
        onConfirm={handleDeleteImageConfirm}
        resourceName={deleteImageName ?? ''}
        resourceType="Image"
        isDeleting={deleteImageMutation.isPending}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Images Tab
// ---------------------------------------------------------------------------

function TenantImagesTab({
  images,
  isLoading,
  onDelete,
  isDeleting,
}: {
  images: GoldenImage[];
  isLoading: boolean;
  onDelete: (name: string) => void;
  isDeleting: boolean;
}) {
  const getStatusColor = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'ready' || s === 'succeeded') return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
    if (s === 'error' || s === 'failed') return 'text-red-400 bg-red-500/10 border-red-500/30';
    return 'text-amber-400 bg-amber-500/10 border-amber-500/30';
  };

  const getStatusIcon = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'ready' || s === 'succeeded') return <CheckCircle className="h-3.5 w-3.5" />;
    if (s === 'error' || s === 'failed') return <AlertTriangle className="h-3.5 w-3.5" />;
    return <Clock className="h-3.5 w-3.5 animate-pulse" />;
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-48">
        <Loader2 className="h-8 w-8 text-primary-400 animate-spin" />
      </div>
    );
  }

  if (images.length === 0) {
    return (
      <div className="card">
        <div className="card-body text-center py-16">
          <Image className="h-12 w-12 mx-auto text-surface-600 mb-4" />
          <h3 className="text-base font-semibold text-surface-100 mb-1">No images yet</h3>
          <p className="text-sm text-surface-400">
            Images are created automatically when a tenant is provisioned.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="card overflow-hidden">
      <table className="w-full">
        <thead className="bg-surface-800/50">
          <tr>
            <th className="table-header">Name</th>
            <th className="table-header">Status</th>
            <th className="table-header">Size</th>
            <th className="table-header">OS</th>
            <th className="table-header">Source</th>
            <th className="table-header">Created</th>
            <th className="table-header text-right">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-800">
          {images.map((img) => (
            <tr key={img.name} className="hover:bg-surface-800/30">
              <td className="table-cell">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-amber-500/10">
                    <Image className="h-4 w-4 text-amber-400" />
                  </div>
                  <div>
                    <p className="font-medium text-surface-100">{img.display_name || img.name}</p>
                    <p className="text-xs text-surface-500 font-mono">{img.name}</p>
                  </div>
                </div>
              </td>
              <td className="table-cell">
                <span
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${getStatusColor(img.status)}`}
                  title={img.error_message || undefined}
                >
                  {getStatusIcon(img.status)}
                  {img.status}
                </span>
              </td>
              <td className="table-cell">
                <span className="text-surface-300 text-sm">{img.size || '-'}</span>
              </td>
              <td className="table-cell">
                <span className="text-surface-400 text-sm">{img.os_type || '-'}</span>
              </td>
              <td className="table-cell max-w-xs">
                {img.source_url ? (
                  <span className="text-surface-400 text-xs font-mono truncate block max-w-[240px]" title={img.source_url}>
                    {img.source_url}
                  </span>
                ) : (
                  <span className="text-surface-600 text-sm">-</span>
                )}
              </td>
              <td className="table-cell">
                <span className="text-surface-400 text-xs">
                  {img.created ? new Date(img.created).toLocaleDateString() : '-'}
                </span>
              </td>
              <td className="table-cell text-right">
                <button
                  onClick={() => onDelete(img.name)}
                  disabled={isDeleting}
                  className="p-1.5 rounded-lg text-surface-400 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-50"
                  title="Delete image"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
