/**
 * Tenants Management Page
 *
 * List tenants + Create Tenant wizard (multi-step)
 */

import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Plus,
  RefreshCw,
  Box,
  CheckCircle,
  XCircle,
  Clock,
  Loader2,
  X,
  ChevronRight,
  ChevronLeft,
  ChevronDown,
  Server,
  Cpu,
  Eye,
  Trash2,
  HardDrive,
  Info,
} from 'lucide-react';
import clsx from 'clsx';
import { useTenants, useCreateTenant, useDeleteTenant, useAddonCatalog, useDiscovery } from '../hooks/useTenants';
import { useStorageClasses } from '../hooks/useStorage';
import { useFoldersFlat } from '../hooks/useFolders';
import { useVpcs } from '../hooks/useVpcs';
import { useAuthStore } from '../store/auth';
import { useAppStore } from '../store';
import type { Tenant, TenantCreateRequest, TenantAddon, AddonComponent } from '../types/tenant';
import { WizardStepIndicator } from '../components/common/WizardStepIndicator';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

function TenantStatusBadge({ status }: { status: string }) {
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
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${color}`}>
      <Icon className={`h-3.5 w-3.5 ${status === 'Provisioning' || status === 'Deleting' ? 'animate-spin' : ''}`} />
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Create Tenant Wizard
// ---------------------------------------------------------------------------

// Real tags confirmed on quay.io/capk/* (see T10)
const K8S_VERSIONS = ['v1.34.1', 'v1.33.5', 'v1.32.1', 'v1.31.5', 'v1.30.1', 'v1.29.5'];

const CAPK_URL_RE = /^quay\.io\/capk\/ubuntu-\d{4}-container-disk:/;

function isCapkUrl(url: string): boolean {
  return CAPK_URL_RE.test(url);
}

/** Derive the canonical CAPK worker image URL from a Kubernetes version string. */
function deriveCapkUrl(version: string): string {
  const match = version.match(/^v1\.(\d+)\./);
  if (!match) return '';
  const minor = parseInt(match[1]!, 10);
  const ubuntuTag = minor <= 30 ? '2204' : '2404';
  return `quay.io/capk/ubuntu-${ubuntuTag}-container-disk:${version}`;
}

interface WizardState {
  name: string;
  display_name: string;
  kubernetes_version: string;
  control_plane_replicas: number;
  worker_type: 'vm' | 'bare_metal';
  worker_count: number;
  worker_vcpu: number;
  worker_memory: string;
  worker_disk: string;
  worker_image_url: string;
  worker_image_pull_secrets: string[];
  worker_network_binding: 'bridge' | 'masquerade'; // T11
  pod_cidr: string;
  service_cidr: string;
  enable_oidc: boolean;
  admin_group: string;
  viewer_group: string;
  dns_servers: string[];
  dns_mode: 'append' | 'override';
  dns_include_public_fallback: boolean;
  // T8 — folder / environment
  folder: string;
  environment: string;
  // Network: VPC selected by user (empty = default cluster network)
  vpc_name: string;
  // T5 — persistent storage (kubevirt-csi)
  enable_storage: boolean;
  storage_class: string;       // empty = cluster default
  storage_quota_gi: number;
  storage_pvc_count: number;
  selectedAddons: Record<string, boolean>;
  addonParams: Record<string, Record<string, string>>;
}

const defaultWizard: WizardState = {
  name: '',
  display_name: '',
  kubernetes_version: 'v1.32.1', // matches latest confirmed CAPK tag (T10)
  control_plane_replicas: 2,
  worker_type: 'vm',
  worker_count: 2,
  worker_vcpu: 2,
  worker_memory: '2Gi',
  worker_disk: '20Gi',
  worker_image_url: deriveCapkUrl('v1.32.1'), // auto-derived on load
  worker_image_pull_secrets: [],
  worker_network_binding: 'bridge',
  pod_cidr: '10.244.0.0/16',
  service_cidr: '10.96.0.0/12',
  enable_oidc: false,
  admin_group: '',
  viewer_group: '',
  dns_servers: [],
  dns_mode: 'append',
  dns_include_public_fallback: true,
  folder: '',
  environment: '',
  vpc_name: '',
  // T5 — storage (off by default until kubevirt-csi is bootstrapped)
  // Defaults match backend model (storage_quota_gi: 100, storage_pvc_count: 20)
  enable_storage: false,
  storage_class: '',
  storage_quota_gi: 100,
  storage_pvc_count: 20,
  selectedAddons: {},
  addonParams: {},
};

// Real CAPK tags — confirmed available on quay.io/capk (T10)
const capkPresets = [
  { name: 'Ubuntu 22.04 / k8s 1.29.5', url: 'quay.io/capk/ubuntu-2204-container-disk:v1.29.5' },
  { name: 'Ubuntu 22.04 / k8s 1.30.1', url: 'quay.io/capk/ubuntu-2204-container-disk:v1.30.1' },
  { name: 'Ubuntu 24.04 / k8s 1.31.5', url: 'quay.io/capk/ubuntu-2404-container-disk:v1.31.5' },
  { name: 'Ubuntu 24.04 / k8s 1.32.1', url: 'quay.io/capk/ubuntu-2404-container-disk:v1.32.1' },
  { name: 'Ubuntu 24.04 / k8s 1.33.5', url: 'quay.io/capk/ubuntu-2404-container-disk:v1.33.5' },
  { name: 'Ubuntu 24.04 / k8s 1.34.1', url: 'quay.io/capk/ubuntu-2404-container-disk:v1.34.1' },
];

/** DNS-1123 label: lowercase alphanumeric + hyphen, ≤63 chars, start/end alphanumeric */
const isDns1123Label = (s: string): boolean =>
  /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(s);

function DnsServerList({ servers, onChange }: { servers: string[]; onChange: (s: string[]) => void }) {
  const [draft, setDraft] = useState('');
  const ipRe = /^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$/;

  const addServer = () => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    onChange([...servers, trimmed]);
    setDraft('');
  };

  return (
    <div className="space-y-1.5">
      {servers.length === 0 && (
        <p className="text-xs text-surface-500 italic">No additional servers.</p>
      )}
      {servers.map((s, i) => (
        <div key={i} className="flex items-center gap-2">
          <code className={clsx(
            'flex-1 text-xs px-2 py-1 rounded bg-surface-900 font-mono',
            ipRe.test(s) ? 'text-surface-300' : 'text-amber-400',
          )}>
            {s}
          </code>
          <button
            onClick={() => onChange(servers.filter((_, idx) => idx !== i))}
            className="p-1.5 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors"
            title="Remove"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
      <div className="flex gap-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addServer(); } }}
          placeholder="e.g. 192.168.10.1"
          className="flex-1 px-2 py-1 bg-surface-800 border border-surface-700 rounded text-xs text-surface-100 focus:outline-none focus:border-primary-500 font-mono"
        />
        <button
          onClick={addServer}
          disabled={!draft.trim()}
          className="px-3 py-1 text-xs bg-surface-700 hover:bg-surface-600 disabled:opacity-40 disabled:cursor-not-allowed text-surface-100 rounded transition-colors"
        >
          Add
        </button>
      </div>
    </div>
  );
}


function CreateTenantWizard({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<WizardState>(defaultWizard);
  const [secretInput, setSecretInput] = useState('');
  const [imageAutoFilled, setImageAutoFilled] = useState(true); // default worker_image_url is auto-derived
  const [showNetworkBinding, setShowNetworkBinding] = useState(false);
  const [showCidrSection, setShowCidrSection] = useState(false);
  const { data: catalog } = useAddonCatalog();
  const { data: discovery } = useDiscovery();
  const { data: foldersData } = useFoldersFlat();
  const { data: storageClassesData } = useStorageClasses();
  // VPCs scoped to the chosen folder/env (re-fetches when they change).
  // Tenants in the same folder/env that pick the same VPC share one trust
  // domain (the VPC), per the shared-VPC tenant model.
  const { data: vpcsData } = useVpcs(
    form.folder && form.environment
      ? { folder: form.folder, environment: form.environment }
      : undefined
  );
  const user = useAuthStore(s => s.user);
  const createTenant = useCreateTenant();

  // Storage classes for T5 picker
  const storageClasses = storageClassesData?.items ?? [];

  // Ceph discovery marks one class as the sensible default for the CSI
  // passthrough (RBD + Immediate — see _suggest_ceph_sc on the backend).
  const suggestedStorageClass =
    discovery?.storage
      .find(s => s.type === 'ceph')
      ?.storage_classes.find(sc => sc.suggested)?.name ?? '';

  // Folder list — admins see all, non-admins see folders where they're listed
  const allFolders = foldersData?.items ?? [];
  const visibleFolders = user?.is_admin
    ? allFolders
    : allFolders.filter(f => f.users.includes(user?.username ?? ''));

  // Environments from selected folder
  const selectedFolder = allFolders.find(f => f.name === form.folder);
  const folderEnvironments = selectedFolder?.environments ?? [];

  // Autoderive worker_image_url when kubernetes_version changes (T10)
  useEffect(() => {
    const derived = deriveCapkUrl(form.kubernetes_version);
    if (derived && (form.worker_image_url === '' || isCapkUrl(form.worker_image_url))) {
      setForm(prev => ({ ...prev, worker_image_url: derived }));
      setImageAutoFilled(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.kubernetes_version]);

  // Preselect the discovered storage class — once, and only while the field
  // is still untouched, so a deliberate "(cluster default)" choice sticks.
  const storageClassPreselected = useRef(false);
  useEffect(() => {
    if (!suggestedStorageClass || storageClassPreselected.current) return;
    storageClassPreselected.current = true;
    setForm(prev => (prev.storage_class ? prev : { ...prev, storage_class: suggestedStorageClass }));
  }, [suggestedStorageClass]);

  // Reset environment + vpc when folder changes
  useEffect(() => {
    setForm(prev => ({ ...prev, environment: '', vpc_name: '' }));
  }, [form.folder]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reset vpc when environment changes
  useEffect(() => {
    setForm(prev => ({ ...prev, vpc_name: '' }));
  }, [form.environment]); // eslint-disable-line react-hooks/exhaustive-deps

  // Initialize addon defaults from catalog once it loads
  useEffect(() => {
    if (catalog?.components) {
      const selected: Record<string, boolean> = {};
      const params: Record<string, Record<string, string>> = {};
      for (const c of ((catalog.components ?? []) as AddonComponent[]).filter(c => c.category !== 'core')) {
        selected[c.id] = c.required || c.default;
        if (c.parameters.length > 0) {
          const ap: Record<string, string> = {};
          for (const p of c.parameters) {
            ap[p.id] = p.default;
          }
          params[c.id] = ap;
        }
      }
      setForm(prev => ({ ...prev, selectedAddons: selected, addonParams: params }));
    }
  }, [catalog]);

  const steps = ['Basics', 'Workers', 'Addons', 'Network', 'Review'];

  const handleSubmit = async () => {
    const addons: TenantAddon[] = [];
    for (const [id, enabled] of Object.entries(form.selectedAddons)) {
      if (enabled) {
        const params = { ...(form.addonParams[id] || {}) };
        // Inject auto-discovered values for params that weren't manually set
        const component = catalog?.components?.find((c: AddonComponent) => c.id === id);
        if (component && discovery) {
          for (const p of component.parameters) {
            if (p.auto_discover && !params[p.id]) {
              if (p.id === 'LINSTOR_API_URL' && discovery.storage.length > 0) {
                params[p.id] = discovery.storage[0]!.api_url;
              } else if (p.id === 'STORAGE_POOL' && discovery.storage.length > 0) {
                const pools = discovery.storage.flatMap(s => s.pools);
                if (pools.length > 0) params[p.id] = pools[0]!.name;
              } else if (p.id === 'VM_REMOTE_WRITE_URL' && discovery.monitoring.length > 0) {
                params[p.id] = discovery.monitoring[0]!.write_url;
              } else if (p.id === 'LOKI_PUSH_URL' && discovery.logging.length > 0) {
                params[p.id] = discovery.logging[0]!.push_url;
              } else if (p.id === 'INFRA_STORAGE_CLASS_NAME') {
                // Only reached when the CSI addon was ticked by hand — the
                // backend fills these itself for the auto-added one.
                const sc = form.storage_class || suggestedStorageClass;
                if (sc) params[p.id] = sc;
              } else if (p.id === 'INFRA_CLUSTER_NAMESPACE') {
                params[p.id] = `tenant-${form.name}`;
              }
            }
          }
        }
        addons.push({ addon_id: id, parameters: params });
      }
    }

    const request: TenantCreateRequest = {
      name: form.name,
      display_name: form.display_name,
      kubernetes_version: form.kubernetes_version,
      control_plane_replicas: form.control_plane_replicas,
      worker_type: form.worker_type,
      worker_count: form.worker_count,
      worker_vcpu: form.worker_vcpu,
      worker_memory: form.worker_memory,
      worker_disk: form.worker_disk,
      pod_cidr: form.pod_cidr,
      service_cidr: form.service_cidr,
      enable_oidc: form.enable_oidc,
      // Group bindings only ride along when OIDC is on — without it the
      // tenant apiserver has no notion of "groups" claim, the strings
      // would never bind to anything.
      ...(form.enable_oidc ? {
        admin_group: form.admin_group,
        viewer_group: form.viewer_group,
      } : { admin_group: '', viewer_group: '' }),
      // Worker DNS (cloud-init resolv.conf)
      dns_servers: form.dns_servers.filter(s => s.trim()),
      dns_mode: form.dns_mode,
      dns_include_public_fallback: form.dns_include_public_fallback,
      // T8: folder / environment
      ...(form.folder ? { folder: form.folder } : {}),
      ...(form.environment ? { environment: form.environment } : {}),
      // Network: explicit VPC choice (empty = default cluster network)
      ...(form.vpc_name ? { vpc_name: form.vpc_name } : {}),
      ...(form.worker_image_url ? {
        worker_image_url: form.worker_image_url,
        worker_image_source_type: 'registry' as const,
      } : {}),
      ...(form.worker_image_pull_secrets.length > 0 ? {
        worker_image_pull_secrets: form.worker_image_pull_secrets,
      } : {}),
      // T11: network binding (omit if default bridge)
      ...(form.worker_network_binding !== 'bridge' ? {
        worker_network_binding: form.worker_network_binding,
      } : {}),
      // T5: persistent storage (omit entirely when disabled)
      ...(form.enable_storage ? {
        enable_storage: true,
        storage_class: form.storage_class || undefined,
        storage_quota_gi: form.storage_quota_gi,
        storage_pvc_count: form.storage_pvc_count,
      } : {}),
      addons,
    };

    await createTenant.mutateAsync(request);
    onCreated();
  };

  const canNext = () => {
    if (step === 0) {
      if (!form.name || !form.display_name) return false;
      if (!form.folder) return false; // T8: folder required
      if (!form.environment) return false; // T8: environment required
      return true;
    }
    if (step === 1) {
      if (form.worker_count <= 0) return false;
      return true;
    }
    return true;
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface-900 border border-surface-700 rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-surface-700">
          <h2 className="text-xl font-semibold text-surface-100">Create Tenant</h2>
          <button onClick={onClose} className="text-surface-400 hover:text-surface-200">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Steps indicator */}
        <div className="px-6 pt-4">
          <WizardStepIndicator
            steps={steps}
            currentStep={step}
            onStepClick={(i) => i < step && setStep(i)}
          />
        </div>

        {/* Content */}
        <div className="p-6 space-y-4">
          {step === 0 && (
            <>
              <div>
                <label className="block text-sm text-surface-300 mb-1">Tenant Name</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={e => setForm({ ...form, name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') })}
                  placeholder="my-tenant"
                  className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500"
                />
                <p className="text-xs text-surface-500 mt-1">Lowercase letters, numbers, hyphens only</p>
              </div>
              <div>
                <label className="block text-sm text-surface-300 mb-1">Display Name</label>
                <input
                  type="text"
                  value={form.display_name}
                  onChange={e => setForm({ ...form, display_name: e.target.value })}
                  placeholder="My Tenant Cluster"
                  className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500"
                />
              </div>
              <div>
                <label className="block text-sm text-surface-300 mb-1">Kubernetes Version</label>
                <select
                  value={form.kubernetes_version}
                  onChange={e => setForm({ ...form, kubernetes_version: e.target.value })}
                  className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                >
                  {K8S_VERSIONS.map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-sm text-surface-300 mb-1">Control Plane Replicas</label>
                <div className="flex gap-2">
                  {[1, 2, 3].map(n => (
                    <button
                      key={n}
                      onClick={() => setForm({ ...form, control_plane_replicas: n })}
                      className={`px-4 py-2 rounded-lg border text-sm font-medium transition-colors ${
                        form.control_plane_replicas === n
                          ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                          : 'border-surface-700 bg-surface-800 text-surface-300 hover:border-surface-600'
                      }`}
                    >
                      {n}
                    </button>
                  ))}
                </div>
              </div>

              {/* T8: Folder + Environment */}
              <div className="pt-2 border-t border-surface-700/50 space-y-4">
                <p className="text-xs font-semibold text-surface-400 uppercase tracking-wider">Folder &amp; Environment</p>
                <div>
                  <label className="block text-sm text-surface-300 mb-1">
                    Folder <span className="text-red-400">*</span>
                  </label>
                  <select
                    value={form.folder}
                    onChange={e => setForm({ ...form, folder: e.target.value })}
                    className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                  >
                    <option value="">Select folder…</option>
                    {visibleFolders.map(f => (
                      <option key={f.name} value={f.name}>
                        {f.display_name || f.name}
                        {f.path.length > 0 ? ` (${f.path.join(' / ')})` : ''}
                      </option>
                    ))}
                  </select>
                  {visibleFolders.length === 0 && (
                    <p className="text-xs text-amber-400 mt-1">No folders available. Ask an admin to create one.</p>
                  )}
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-1">
                    Environment <span className="text-red-400">*</span>
                  </label>
                  <select
                    value={form.environment}
                    onChange={e => setForm({ ...form, environment: e.target.value })}
                    disabled={!form.folder}
                    className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <option value="">{form.folder ? 'Select environment…' : 'Select a folder first'}</option>
                    {folderEnvironments.map(env => (
                      <option key={env.environment} value={env.environment}>
                        {env.environment}
                        {env.name !== env.environment ? ` (${env.name})` : ''}
                      </option>
                    ))}
                  </select>
                  {form.folder && folderEnvironments.length === 0 && (
                    <p className="text-xs text-amber-400 mt-1">This folder has no environments. Add one in Folders first.</p>
                  )}
                </div>
              </div>

              {/* OIDC / RBAC */}
              <div className="mt-4 p-4 rounded-lg border border-surface-700 bg-surface-800/40 space-y-4">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p className="text-sm font-semibold text-surface-100">
                      OIDC Authentication
                      <span className={clsx(
                        'ml-2 text-[10px] px-1.5 py-0.5 rounded',
                        form.enable_oidc
                          ? 'bg-emerald-500/10 text-emerald-400'
                          : 'bg-surface-700 text-surface-400',
                      )}>
                        {form.enable_oidc ? 'ENABLED' : 'DISABLED'}
                      </span>
                    </p>
                    <p className="text-xs text-surface-500 mt-1">
                      Configure tenant apiserver to trust this cluster's Dex.
                      Disable if Dex isn't reachable from the apiserver pod yet.
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={form.enable_oidc}
                    onClick={() => setForm({ ...form, enable_oidc: !form.enable_oidc })}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      form.enable_oidc ? 'bg-primary-600' : 'bg-surface-600'
                    }`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                        form.enable_oidc ? 'translate-x-6' : 'translate-x-1'
                      }`}
                    />
                  </button>
                </div>

                {form.enable_oidc && (
                  <>
                    <p className="text-xs text-surface-500">
                      Map DEX groups to Kubernetes RBAC roles inside the tenant cluster.
                      Leave empty to skip RBAC setup.
                    </p>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-sm text-surface-300 mb-1">Admin Group (cluster-admin)</label>
                        <input
                          type="text"
                          value={form.admin_group}
                          placeholder="e.g. tenant-admins"
                          onChange={e => setForm({ ...form, admin_group: e.target.value })}
                          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 placeholder-surface-600"
                        />
                      </div>
                      <div>
                        <label className="block text-sm text-surface-300 mb-1">Viewer Group (view)</label>
                        <input
                          type="text"
                          value={form.viewer_group}
                          placeholder="e.g. tenant-viewers"
                          onChange={e => setForm({ ...form, viewer_group: e.target.value })}
                          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 placeholder-surface-600"
                        />
                      </div>
                    </div>
                  </>
                )}
              </div>

              {/* Advanced CIDR — collapsible */}
              <div className="border border-surface-700 rounded-lg overflow-hidden">
                <button
                  type="button"
                  onClick={() => setShowCidrSection(v => !v)}
                  className="w-full flex items-center justify-between px-4 py-3 bg-surface-800 hover:bg-surface-700/50 text-sm text-surface-300 transition-colors"
                >
                  <span className="font-medium">Advanced: Pod &amp; Service CIDR</span>
                  <ChevronDown className={`h-4 w-4 transition-transform ${showCidrSection ? 'rotate-180' : ''}`} />
                </button>
                {showCidrSection && (
                  <div className="p-4 bg-surface-800/50">
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-sm text-surface-300 mb-1">Pod CIDR</label>
                        <input
                          type="text"
                          value={form.pod_cidr}
                          onChange={e => setForm({ ...form, pod_cidr: e.target.value })}
                          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                        />
                      </div>
                      <div>
                        <label className="block text-sm text-surface-300 mb-1">Service CIDR</label>
                        <input
                          type="text"
                          value={form.service_cidr}
                          onChange={e => setForm({ ...form, service_cidr: e.target.value })}
                          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                        />
                      </div>
                    </div>
                    <p className="text-xs text-surface-500 mt-2">
                      Default values work for most clusters. Only change if you have overlapping CIDRs.
                    </p>
                  </div>
                )}
              </div>
            </>
          )}

          {step === 1 && (
            <>
              {/* T6: Storage banner — context-sensitive */}
              {form.enable_storage ? (
                <div className="flex gap-3 p-3 bg-emerald-900/10 border border-emerald-700/40 rounded-lg">
                  <CheckCircle className="h-4 w-4 text-emerald-400 shrink-0 mt-0.5" />
                  <p className="text-xs text-emerald-300/90 leading-relaxed">
                    <strong>Persistent storage via kubevirt-csi enabled.</strong>{' '}
                    Workers can request PVCs backed by the host cluster&apos;s{' '}
                    <span className="font-mono text-emerald-200">
                      {form.storage_class || '(cluster default)'}
                    </span>{' '}
                    storage class.
                  </p>
                </div>
              ) : (
                <div className="flex gap-3 p-3 bg-surface-800/60 border border-surface-700/50 rounded-lg">
                  <Info className="h-4 w-4 text-surface-400 shrink-0 mt-0.5" />
                  <p className="text-xs text-surface-400 leading-relaxed">
                    Without persistent storage, workers boot from the container disk only.
                    Toggle <strong className="text-surface-300">Enable persistent storage</strong> below for PVCs.
                  </p>
                </div>
              )}

              {/* Worker Type */}
              <div>
                <label className="block text-sm text-surface-300 mb-2">Worker Type</label>
                <div className="grid grid-cols-2 gap-3">
                  <button
                    type="button"
                    onClick={() => setForm({ ...form, worker_type: 'vm' })}
                    className={`flex items-center gap-3 p-3 rounded-lg border text-left transition-colors ${
                      form.worker_type === 'vm'
                        ? 'border-primary-500 bg-primary-500/10'
                        : 'border-surface-700 bg-surface-800 hover:border-surface-600'
                    }`}
                  >
                    <Server className={`h-5 w-5 shrink-0 ${form.worker_type === 'vm' ? 'text-primary-400' : 'text-surface-500'}`} />
                    <div>
                      <p className={`text-sm font-medium ${form.worker_type === 'vm' ? 'text-primary-300' : 'text-surface-300'}`}>Virtual Machine</p>
                      <p className="text-xs text-surface-500">KubeVirt VMs on host cluster</p>
                    </div>
                  </button>
                  <button
                    type="button"
                    disabled
                    className="flex items-center gap-3 p-3 rounded-lg border border-surface-700 bg-surface-800/50 text-left opacity-50 cursor-not-allowed"
                  >
                    <Cpu className="h-5 w-5 shrink-0 text-surface-600" />
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="text-sm font-medium text-surface-500">Bare Metal</p>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-700 text-surface-500">Coming soon</span>
                      </div>
                      <p className="text-xs text-surface-600">Physical servers</p>
                    </div>
                  </button>
                </div>
              </div>

              {/* Worker Resources */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm text-surface-300 mb-1">Worker Count</label>
                  <input
                    type="number"
                    value={form.worker_count}
                    onChange={e => setForm({ ...form, worker_count: parseInt(e.target.value) || 1 })}
                    min={1} max={20}
                    className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                  />
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-1">vCPU per Worker</label>
                  <input
                    type="number"
                    value={form.worker_vcpu}
                    onChange={e => setForm({ ...form, worker_vcpu: parseInt(e.target.value) || 1 })}
                    min={1} max={32}
                    className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                  />
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-1">Memory per Worker</label>
                  <input
                    type="text"
                    value={form.worker_memory}
                    onChange={e => setForm({ ...form, worker_memory: e.target.value })}
                    placeholder="8Gi"
                    className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                  />
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-1">Disk per Worker</label>
                  <input
                    type="text"
                    value={form.worker_disk}
                    onChange={e => setForm({ ...form, worker_disk: e.target.value })}
                    placeholder="50Gi"
                    className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                  />
                </div>
              </div>

              <div className="p-3 bg-surface-800 rounded-lg border border-surface-700">
                <p className="text-sm text-surface-300">
                  Total resources: <span className="text-primary-400 font-medium">{form.worker_count} VMs</span> ×{' '}
                  <span className="text-primary-400 font-medium">{form.worker_vcpu} vCPU</span>,{' '}
                  <span className="text-primary-400 font-medium">{form.worker_memory} RAM</span>
                </p>
              </div>

              {/* T10: Worker Container Image (CAPK OCI disk) */}
              <div className="space-y-2">
                <label className="block text-sm text-surface-300">
                  Worker Image URL
                  <span className="ml-1 text-surface-500 font-normal">(CAPK container disk)</span>
                </label>
                <input
                  type="text"
                  value={form.worker_image_url}
                  onChange={e => {
                    const val = e.target.value;
                    setForm({ ...form, worker_image_url: val });
                    // If user types a custom non-capk URL, clear the auto-fill flag
                    if (val !== '' && !isCapkUrl(val)) {
                      setImageAutoFilled(false);
                    }
                  }}
                  placeholder="quay.io/capk/ubuntu-2404-container-disk:v1.32.1"
                  className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                />
                {imageAutoFilled && form.worker_image_url && (
                  <p className="text-xs text-emerald-400 flex items-center gap-1">
                    <CheckCircle className="h-3 w-3" />
                    Auto-filled from Kubernetes version
                  </p>
                )}
                <div>
                  <p className="text-xs text-surface-500 mb-1.5">CAPK presets (quay.io/capk):</p>
                  <div className="flex flex-wrap gap-2">
                    {capkPresets.map((preset) => (
                      <button
                        key={preset.name}
                        type="button"
                        onClick={() => {
                          setForm({ ...form, worker_image_url: preset.url });
                          setImageAutoFilled(false);
                        }}
                        className={`px-3 py-1.5 text-xs rounded-md border transition-colors ${
                          form.worker_image_url === preset.url
                            ? 'border-primary-500 bg-primary-500/10 text-primary-300'
                            : 'border-surface-700 bg-surface-800 text-surface-400 hover:border-surface-600 hover:text-surface-300'
                        }`}
                      >
                        {preset.name}
                      </button>
                    ))}
                    {form.worker_image_url && !capkPresets.some(p => p.url === form.worker_image_url) && (
                      <span className="px-3 py-1.5 text-xs rounded-md border border-surface-600 bg-surface-800 text-surface-400 italic">
                        Custom URL
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* Image Pull Secrets */}
              <div className="space-y-2">
                <label className="block text-sm text-surface-300">Image Pull Secrets</label>
                {/* Existing secrets chips */}
                {form.worker_image_pull_secrets.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-1">
                    {form.worker_image_pull_secrets.map((secret) => (
                      <span
                        key={secret}
                        className="inline-flex items-center gap-1 px-2 py-1 bg-primary-500/10 border border-primary-500/30 text-primary-300 rounded-md text-xs font-mono"
                      >
                        {secret}
                        <button
                          type="button"
                          onClick={() =>
                            setForm({
                              ...form,
                              worker_image_pull_secrets: form.worker_image_pull_secrets.filter(s => s !== secret),
                            })
                          }
                          className="ml-0.5 text-primary-400 hover:text-primary-200 transition-colors"
                          aria-label={`Remove ${secret}`}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                {/* Input for adding a new secret */}
                <input
                  type="text"
                  value={secretInput}
                  onChange={e => {
                    // Strip commas live — comma triggers add
                    const val = e.target.value.replace(/,/g, '');
                    setSecretInput(val);
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter' || e.key === ',') {
                      e.preventDefault();
                      const trimmed = secretInput.trim();
                      if (trimmed && isDns1123Label(trimmed) && !form.worker_image_pull_secrets.includes(trimmed)) {
                        setForm({ ...form, worker_image_pull_secrets: [...form.worker_image_pull_secrets, trimmed] });
                      }
                      setSecretInput('');
                    } else if (e.key === 'Backspace' && secretInput === '' && form.worker_image_pull_secrets.length > 0) {
                      // Remove last chip on backspace when input is empty
                      setForm({
                        ...form,
                        worker_image_pull_secrets: form.worker_image_pull_secrets.slice(0, -1),
                      });
                    }
                  }}
                  placeholder={form.worker_image_pull_secrets.length === 0 ? 'my-registry-secret (Enter or comma to add)' : 'Add another…'}
                  className={`w-full px-3 py-2 bg-surface-800 border rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none text-sm font-mono ${
                    secretInput && !isDns1123Label(secretInput)
                      ? 'border-red-500/60 focus:border-red-500'
                      : 'border-surface-700 focus:border-primary-500'
                  }`}
                />
                {secretInput && !isDns1123Label(secretInput) && (
                  <p className="text-xs text-red-400">
                    Must be lowercase alphanumeric + hyphens, start/end with alphanumeric, ≤63 chars
                  </p>
                )}
                <p className="text-xs text-surface-500">
                  Secrets must already exist in the tenant namespace; admin creates them.
                </p>
              </div>

              {/* T11: Network binding (advanced) */}
              <div className="border border-surface-700 rounded-lg overflow-hidden">
                <button
                  type="button"
                  onClick={() => setShowNetworkBinding(v => !v)}
                  className="w-full flex items-center justify-between px-4 py-3 bg-surface-800 hover:bg-surface-700/50 text-sm text-surface-300 transition-colors"
                >
                  <span className="font-medium">Network binding (advanced)</span>
                  <ChevronDown className={`h-4 w-4 transition-transform ${showNetworkBinding ? 'rotate-180' : ''}`} />
                </button>
                {showNetworkBinding && (
                  <div className="p-4 bg-surface-800/50 space-y-3">
                    {(['bridge', 'masquerade'] as const).map(mode => (
                      <label key={mode} className="flex items-start gap-3 cursor-pointer group">
                        <input
                          type="radio"
                          name="worker_network_binding"
                          value={mode}
                          checked={form.worker_network_binding === mode}
                          onChange={() => setForm({ ...form, worker_network_binding: mode })}
                          className="mt-0.5 h-4 w-4 border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500"
                        />
                        <div>
                          <p className="text-sm font-medium text-surface-200 group-hover:text-surface-100">
                            {mode === 'bridge' ? 'Bridge' : 'Masquerade'}
                            {mode === 'bridge' && (
                              <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">default, recommended</span>
                            )}
                          </p>
                          <p className="text-xs text-surface-500 mt-0.5">
                            {mode === 'bridge'
                              ? 'Worker guests see the real pod CIDR IP. Required for multi-cast and consistent IP reporting.'
                              : 'QEMU SLIRP NAT inside guest. Worker guest sees 10.0.2.x; pod CIDR IP is masqueraded. Choose this only if you have a specific CNI compatibility requirement.'}
                          </p>
                        </div>
                      </label>
                    ))}
                  </div>
                )}
              </div>

              {/* T5: Persistent storage (kubevirt-csi) */}
              <div className="pt-2 border-t border-surface-700/50 space-y-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <HardDrive className="h-4 w-4 text-surface-400" />
                    <span className="text-sm font-medium text-surface-200">
                      Enable persistent storage (via kubevirt-csi)
                    </span>
                  </div>
                  {/* Toggle switch */}
                  <button
                    type="button"
                    role="switch"
                    aria-checked={form.enable_storage}
                    onClick={() => setForm({ ...form, enable_storage: !form.enable_storage })}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 focus:ring-offset-surface-900 ${
                      form.enable_storage ? 'bg-primary-600' : 'bg-surface-600'
                    }`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                        form.enable_storage ? 'translate-x-6' : 'translate-x-1'
                      }`}
                    />
                  </button>
                </div>

                {form.enable_storage && (
                  <div className="space-y-4 pl-6">
                    {/* StorageClass picker */}
                    <div>
                      <label className="block text-sm text-surface-300 mb-1">Storage Class</label>
                      <select
                        value={form.storage_class}
                        onChange={e => setForm({ ...form, storage_class: e.target.value })}
                        className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                      >
                        <option value="">(cluster default)</option>
                        {storageClasses.map(sc => (
                          <option key={sc.name} value={sc.name}>
                            {sc.name}
                            {sc.is_default ? ' (default)' : ''}
                            {sc.name === suggestedStorageClass ? ' — recommended' : ''}
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-surface-500 mt-1">
                        Storage class used for worker PVCs. Leave blank to use the cluster default.
                        {suggestedStorageClass && (
                          <>
                            {' '}Discovered <span className="text-surface-300">{suggestedStorageClass}</span>{' '}
                            (Ceph RBD) — recommended, since the CSI passthrough hotplugs the volume
                            onto a VM.
                          </>
                        )}
                      </p>
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                      {/* Total storage quota */}
                      <div>
                        <label className="block text-sm text-surface-300 mb-1">
                          Total Storage Quota (Gi)
                        </label>
                        <input
                          type="number"
                          value={form.storage_quota_gi}
                          onChange={e =>
                            setForm({ ...form, storage_quota_gi: parseInt(e.target.value) || 1 })
                          }
                          min={1}
                          max={10000}
                          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                        />
                        <p className="text-xs text-surface-500 mt-1">Max total GiB across all PVCs (up to 10 000)</p>
                      </div>

                      {/* Max PVC count */}
                      <div>
                        <label className="block text-sm text-surface-300 mb-1">
                          Max PVC Count
                        </label>
                        <input
                          type="number"
                          value={form.storage_pvc_count}
                          onChange={e =>
                            setForm({ ...form, storage_pvc_count: parseInt(e.target.value) || 1 })
                          }
                          min={1}
                          max={200}
                          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500"
                        />
                        <p className="text-xs text-surface-500 mt-1">Max PVCs tenant can create (up to 200)</p>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}

          {step === 2 && (
            <>
              {/* Discovery status */}
              {discovery && (
                <div className="p-3 bg-surface-800/50 rounded-lg border border-surface-700 mb-4 space-y-1">
                  <p className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-1">Auto-detected infrastructure</p>
                  {discovery.storage.map(s => {
                    // Ceph has no REST endpoint to interrogate — its
                    // StorageClasses are the discovery result.
                    const label = s.type === 'ceph'
                      ? `Ceph — ${s.storage_classes.length} storage class${s.storage_classes.length !== 1 ? 'es' : ''} `
                        + `(${s.storage_classes.map(sc => sc.name).join(', ')})`
                      : `Linstor — ${s.pools.length} pool${s.pools.length !== 1 ? 's' : ''} `
                        + `(${s.pools.map(p => `${p.name}: ${p.free_gb}GB free`).join(', ')})`;
                    return (
                      <p key={s.type} className="text-xs text-emerald-400 flex items-center gap-1.5">
                        <CheckCircle className="h-3 w-3" />
                        {label}
                      </p>
                    );
                  })}
                  {discovery.monitoring.map(m => (
                    <p key={m.write_url} className="text-xs text-emerald-400 flex items-center gap-1.5">
                      <CheckCircle className="h-3 w-3" />
                      VictoriaMetrics — {m.write_url.split('.svc')[0]?.split('//')[1] ?? m.write_url}
                    </p>
                  ))}
                  {discovery.logging.map(l => (
                    <p key={l.push_url} className="text-xs text-emerald-400 flex items-center gap-1.5">
                      <CheckCircle className="h-3 w-3" />
                      Loki — {l.push_url.split('.svc')[0]?.split('//')[1] ?? l.push_url}
                    </p>
                  ))}
                  {!discovery.storage.length && !discovery.monitoring.length && !discovery.logging.length && (
                    <p className="text-xs text-surface-500">No infrastructure detected on host cluster</p>
                  )}
                </div>
              )}

              {!catalog?.components?.length ? (
                <p className="text-surface-400 text-sm">
                  No addon catalog found. Create ConfigMap <code className="text-primary-400">tenant-addon-catalog</code> in{' '}
                  <code className="text-primary-400">kubevirt-ui-system</code> namespace.
                </p>
              ) : (
                <div className="space-y-3">
                  {catalog.components.filter((c: AddonComponent) => c.category !== 'core').map((c: AddonComponent) => (
                    <div key={c.id} className="p-4 bg-surface-800 rounded-lg border border-surface-700">
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-3">
                          <input
                            type="checkbox"
                            checked={form.selectedAddons[c.id] || false}
                            disabled={c.required}
                            onChange={e => setForm({
                              ...form,
                              selectedAddons: { ...form.selectedAddons, [c.id]: e.target.checked },
                            })}
                            className="h-4 w-4 rounded border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500"
                          />
                          <div>
                            <span className="text-sm font-medium text-surface-200">{c.name}</span>
                            {c.required && (
                              <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-primary-500/10 text-primary-400">required</span>
                            )}
                          </div>
                        </div>
                        <span className="text-xs text-surface-500">{c.category}</span>
                      </div>
                      {c.description && (
                        <p className="text-xs text-surface-400 mb-2 ml-7">{c.description}</p>
                      )}
                      {form.selectedAddons[c.id] && c.parameters.length > 0 && (
                        <div className="ml-7 mt-2 space-y-2">
                          {c.parameters.map(p => {
                            // Auto-discovered parameters: show discovery-based select or auto-filled label
                            if (p.auto_discover && discovery) {
                              // Storage pool select from Linstor discovery
                              if (p.id === 'STORAGE_POOL' && discovery.storage.length > 0) {
                                const pools = discovery.storage.flatMap(s => s.pools);
                                return (
                                  <div key={p.id} className="flex items-center gap-2">
                                    <label className="text-xs text-surface-400 w-40 shrink-0">{p.name || p.id}</label>
                                    <select
                                      value={form.addonParams[c.id]?.[p.id] || pools[0]?.name || p.default}
                                      onChange={e => setForm({
                                        ...form,
                                        addonParams: {
                                          ...form.addonParams,
                                          [c.id]: { ...form.addonParams[c.id], [p.id]: e.target.value },
                                        },
                                      })}
                                      className="flex-1 px-2 py-1 bg-surface-700 border border-surface-600 rounded text-xs text-surface-200 focus:outline-none focus:border-primary-500"
                                    >
                                      {pools.map(pool => (
                                        <option key={pool.name} value={pool.name}>
                                          {pool.name} — {pool.driver} ({pool.free_gb}GB free, {pool.node_count} nodes)
                                        </option>
                                      ))}
                                    </select>
                                  </div>
                                );
                              }
                              // Auto-filled URL params (LINSTOR_API_URL, VM_REMOTE_WRITE_URL, etc.)
                              const autoValue =
                                (p.id === 'LINSTOR_API_URL' && discovery.storage.length > 0) ? discovery.storage[0]!.api_url :
                                (p.id === 'VM_REMOTE_WRITE_URL' && discovery.monitoring.length > 0) ? discovery.monitoring[0]!.write_url :
                                (p.id === 'LOKI_PUSH_URL' && discovery.logging.length > 0) ? discovery.logging[0]!.push_url :
                                null;
                              if (autoValue) {
                                return (
                                  <div key={p.id} className="flex items-center gap-2">
                                    <label className="text-xs text-surface-400 w-40 shrink-0">{p.name || p.id}</label>
                                    <span className="flex-1 px-2 py-1 bg-surface-700/50 border border-emerald-500/20 rounded text-xs text-emerald-400 font-mono truncate">
                                      {autoValue}
                                    </span>
                                    <span className="text-[10px] text-emerald-500">auto</span>
                                  </div>
                                );
                              }
                            }
                            // Default: manual input or select
                            return (
                              <div key={p.id} className="flex items-center gap-2">
                                <label className="text-xs text-surface-400 w-40 shrink-0">{p.name || p.id}</label>
                                {p.type === 'select' ? (
                                  <select
                                    value={form.addonParams[c.id]?.[p.id] || p.default}
                                    onChange={e => setForm({
                                      ...form,
                                      addonParams: {
                                        ...form.addonParams,
                                        [c.id]: { ...form.addonParams[c.id], [p.id]: e.target.value },
                                      },
                                    })}
                                    className="flex-1 px-2 py-1 bg-surface-700 border border-surface-600 rounded text-xs text-surface-200 focus:outline-none focus:border-primary-500"
                                  >
                                    {p.options.map(o => <option key={o} value={o}>{o}</option>)}
                                  </select>
                                ) : (
                                  <input
                                    type="text"
                                    value={form.addonParams[c.id]?.[p.id] || p.default}
                                    onChange={e => setForm({
                                      ...form,
                                      addonParams: {
                                        ...form.addonParams,
                                        [c.id]: { ...form.addonParams[c.id], [p.id]: e.target.value },
                                      },
                                    })}
                                    className="flex-1 px-2 py-1 bg-surface-700 border border-surface-600 rounded text-xs text-surface-200 focus:outline-none focus:border-primary-500"
                                  />
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {step === 3 && (
            <>
              <div className="space-y-4">
                <div>
                  <h3 className="text-sm font-semibold text-surface-200 mb-1">Network</h3>
                  <p className="text-xs text-surface-500 mb-4">
                    Where the tenant's worker VMs run. A VPC isolates them on a
                    dedicated kube-ovn overlay; tenants sharing one VPC form one
                    trust domain (mutually reachable).
                  </p>
                </div>

                {!form.folder || !form.environment ? (
                  <div className="flex items-start gap-3 p-4 bg-surface-800/60 border border-surface-700/50 rounded-lg">
                    <Info className="h-4 w-4 text-surface-400 shrink-0 mt-0.5" />
                    <p className="text-xs text-surface-400">
                      Select folder and environment first (Basics step) to see available VPCs.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {/* Default cluster network */}
                    <label className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                      form.vpc_name === ''
                        ? 'border-primary-500 bg-primary-500/10'
                        : 'border-surface-700 bg-surface-800 hover:border-surface-600'
                    }`}>
                      <input
                        type="radio"
                        name="vpc_name"
                        value=""
                        checked={form.vpc_name === ''}
                        onChange={() => setForm({ ...form, vpc_name: '' })}
                        className="mt-0.5 h-4 w-4 border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500"
                      />
                      <div>
                        <p className={`text-sm font-medium ${form.vpc_name === '' ? 'text-primary-300' : 'text-surface-200'}`}>
                          Default cluster network
                          <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-surface-700 text-surface-400">no VPC isolation</span>
                        </p>
                        <p className="text-xs text-surface-500 mt-0.5">
                          Workers run on the cluster overlay; control plane reachable via its ClusterIP.
                        </p>
                      </div>
                    </label>

                    {(vpcsData?.items ?? []).length === 0 ? (
                      <div className="flex items-start gap-3 p-3 bg-surface-800/50 border border-surface-700/50 rounded-lg">
                        <Info className="h-4 w-4 text-surface-500 shrink-0 mt-0.5" />
                        <p className="text-xs text-surface-500">
                          No VPCs scoped to <span className="font-mono text-surface-400">{form.folder}/{form.environment}</span>.
                          Ask an admin to label a VPC with this folder/environment.
                        </p>
                      </div>
                    ) : (
                      (vpcsData?.items ?? []).map(vpc => (
                        <label
                          key={vpc.name}
                          className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                            form.vpc_name === vpc.name
                              ? 'border-primary-500 bg-primary-500/10'
                              : 'border-surface-700 bg-surface-800 hover:border-surface-600'
                          }`}
                        >
                          <input
                            type="radio"
                            name="vpc_name"
                            value={vpc.name}
                            checked={form.vpc_name === vpc.name}
                            onChange={() => setForm({ ...form, vpc_name: vpc.name })}
                            className="mt-0.5 h-4 w-4 border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500"
                          />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <p className={`text-sm font-medium font-mono ${form.vpc_name === vpc.name ? 'text-primary-300' : 'text-surface-200'}`}>
                                {vpc.name}
                              </p>
                              {vpc.folder && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary-500/10 text-primary-400">
                                  {vpc.folder}{vpc.environment ? `/${vpc.environment}` : ''}
                                </span>
                              )}
                              <span className={`text-[10px] px-1.5 py-0.5 rounded ${vpc.ready ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
                                {vpc.ready ? 'Ready' : 'Pending'}
                              </span>
                            </div>
                            {(vpc.subnets ?? []).length > 0 && (
                              <p className="text-xs text-surface-500 mt-0.5">
                                {vpc.subnets.map(s => s.cidr_block).join(', ')}
                              </p>
                            )}
                          </div>
                        </label>
                      ))
                    )}
                  </div>
                )}

                {/* Worker DNS configuration */}
                <div className="pt-3 border-t border-surface-700/50 space-y-3">
                  <div>
                    <h3 className="text-sm font-semibold text-surface-200">Worker DNS</h3>
                    <p className="text-xs text-surface-500 mt-0.5">
                      For the worker VM's <strong>guest OS</strong> — image pulls,
                      package installs, cloud-init. <strong>Not</strong> for in-cluster
                      service resolution (that's handled by tenant CoreDNS via
                      kubelet <code>--cluster-dns</code>).
                    </p>
                  </div>

                  <div className="p-2.5 rounded-md bg-surface-800/50 border border-surface-700/50">
                    <p className="text-[11px] uppercase tracking-wider text-surface-500 mb-1">Default primary</p>
                    <code className="text-xs text-primary-300 font-mono">1.1.1.1</code>
                    <span className="ml-2 text-[10px] text-surface-500">Cloudflare public DNS</span>
                  </div>

                  <div>
                    <label className="text-xs text-surface-400 mb-1 block">Additional DNS servers</label>
                    <DnsServerList
                      servers={form.dns_servers}
                      onChange={(servers) => setForm({ ...form, dns_servers: servers })}
                    />
                  </div>

                  <div className="space-y-2">
                    <label className="text-xs text-surface-400 block">Mode</label>
                    <div className="space-y-1.5">
                      <label className="flex items-start gap-2 cursor-pointer">
                        <input
                          type="radio"
                          name="dns_mode"
                          value="append"
                          checked={form.dns_mode === 'append'}
                          onChange={() => setForm({ ...form, dns_mode: 'append' })}
                          className="mt-0.5 h-3.5 w-3.5"
                        />
                        <span className="text-xs">
                          <span className="text-surface-200">Append</span>
                          <span className="text-surface-500"> — primary + your servers (+ public fallback if enabled)</span>
                        </span>
                      </label>
                      <label className="flex items-start gap-2 cursor-pointer">
                        <input
                          type="radio"
                          name="dns_mode"
                          value="override"
                          checked={form.dns_mode === 'override'}
                          onChange={() => setForm({ ...form, dns_mode: 'override' })}
                          className="mt-0.5 h-3.5 w-3.5"
                        />
                        <span className="text-xs">
                          <span className="text-surface-200">Override</span>
                          <span className="text-surface-500"> — only your servers (primary used as safety net if empty)</span>
                        </span>
                      </label>
                    </div>
                  </div>

                  <label className={clsx(
                    'flex items-start gap-2 cursor-pointer',
                    form.dns_mode === 'override' && 'opacity-50',
                  )}>
                    <input
                      type="checkbox"
                      checked={form.dns_include_public_fallback}
                      onChange={(e) => setForm({ ...form, dns_include_public_fallback: e.target.checked })}
                      disabled={form.dns_mode === 'override'}
                      className="mt-0.5 h-3.5 w-3.5"
                    />
                    <span className="text-xs">
                      <span className="text-surface-200">Include public fallback DNS</span>
                      <span className="text-surface-500">
                        {' '}— appends <code>8.8.8.8</code> (Google) to the end so the worker
                        still resolves names if Cloudflare is unreachable. Disable for
                        air-gapped clusters or to enforce corporate-only DNS.
                      </span>
                    </span>
                  </label>
                </div>
              </div>
            </>
          )}

          {step === 4 && (
            <>
              <div className="space-y-4">
                <div>
                  <h3 className="text-sm font-semibold text-surface-200 mb-3">Review & Create</h3>
                  <p className="text-xs text-surface-500 mb-4">
                    Review the configuration before creating the tenant cluster.
                  </p>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 bg-surface-800 rounded-lg border border-surface-700">
                    <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-3">Basics</h4>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                      <span className="text-surface-500">Name</span>
                      <span className="text-surface-200 font-medium">{form.name}</span>
                      <span className="text-surface-500">Display Name</span>
                      <span className="text-surface-200">{form.display_name}</span>
                      <span className="text-surface-500">K8s Version</span>
                      <span className="text-surface-200">{form.kubernetes_version}</span>
                      <span className="text-surface-500">Control Plane</span>
                      <span className="text-surface-200">{form.control_plane_replicas} replicas</span>
                      {form.folder && (
                        <>
                          <span className="text-surface-500">Folder</span>
                          <span className="text-surface-200">{form.folder}</span>
                        </>
                      )}
                      {form.environment && (
                        <>
                          <span className="text-surface-500">Environment</span>
                          <span className="text-surface-200">{form.environment}</span>
                        </>
                      )}
                    </div>
                  </div>

                  <div className="p-4 bg-surface-800 rounded-lg border border-surface-700">
                    <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-3">Workers</h4>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                      <span className="text-surface-500">Type</span>
                      <span className="text-surface-200">{form.worker_type === 'vm' ? 'Virtual Machine' : 'Bare Metal'}</span>
                      <span className="text-surface-500">Count</span>
                      <span className="text-surface-200">{form.worker_count}</span>
                      <span className="text-surface-500">vCPU</span>
                      <span className="text-surface-200">{form.worker_vcpu}</span>
                      <span className="text-surface-500">Memory</span>
                      <span className="text-surface-200">{form.worker_memory}</span>
                      <span className="text-surface-500">Disk</span>
                      <span className="text-surface-200">{form.worker_disk}</span>
                      {form.worker_image_url && (
                        <>
                          <span className="text-surface-500">Image</span>
                          <span className="text-surface-200 font-mono truncate" title={form.worker_image_url}>
                            {form.worker_image_url}
                          </span>
                        </>
                      )}
                      {form.worker_image_pull_secrets.length > 0 && (
                        <>
                          <span className="text-surface-500">Pull Secrets</span>
                          <span className="text-surface-200">{form.worker_image_pull_secrets.join(', ')}</span>
                        </>
                      )}
                      {/* T5: Storage */}
                      <span className="text-surface-500">Storage</span>
                      {form.enable_storage ? (
                        <span className="text-emerald-400 flex items-center gap-1">
                          <CheckCircle className="h-3 w-3" /> kubevirt-csi
                        </span>
                      ) : (
                        <span className="text-surface-400">Disabled</span>
                      )}
                      {form.enable_storage && (
                        <>
                          <span className="text-surface-500">Storage Class</span>
                          <span className="text-surface-200 font-mono">
                            {form.storage_class || '(cluster default)'}
                          </span>
                          <span className="text-surface-500">Quota</span>
                          <span className="text-surface-200">{form.storage_quota_gi} Gi</span>
                          <span className="text-surface-500">Max PVCs</span>
                          <span className="text-surface-200">{form.storage_pvc_count}</span>
                        </>
                      )}
                    </div>
                  </div>

                  <div className="p-4 bg-surface-800 rounded-lg border border-surface-700">
                    <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-3">Network</h4>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                      <span className="text-surface-500">Pod CIDR</span>
                      <span className="text-surface-200 font-mono">{form.pod_cidr}</span>
                      <span className="text-surface-500">Service CIDR</span>
                      <span className="text-surface-200 font-mono">{form.service_cidr}</span>
                      <span className="text-surface-500">VPC</span>
                      <span className="text-surface-200">
                        {form.vpc_name ? (
                          <span className="font-mono text-primary-300">{form.vpc_name}</span>
                        ) : (
                          <span className="text-surface-400">Default cluster network</span>
                        )}
                      </span>
                      {form.worker_network_binding === 'masquerade' && (
                        <>
                          <span className="text-surface-500">Net Binding</span>
                          <span className="text-surface-200">Masquerade</span>
                        </>
                      )}
                      <span className="text-surface-500">OIDC</span>
                      <span className={form.enable_oidc ? 'text-emerald-400' : 'text-surface-400'}>
                        {form.enable_oidc ? 'Enabled' : 'Disabled'}
                      </span>
                      {form.enable_oidc && form.admin_group && (
                        <>
                          <span className="text-surface-500">Admin Group</span>
                          <span className="text-surface-200">{form.admin_group}</span>
                        </>
                      )}
                      {form.enable_oidc && form.viewer_group && (
                        <>
                          <span className="text-surface-500">Viewer Group</span>
                          <span className="text-surface-200">{form.viewer_group}</span>
                        </>
                      )}
                    </div>
                  </div>

                  <div className="p-4 bg-surface-800 rounded-lg border border-surface-700">
                    <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-3">
                      Addons ({Object.values(form.selectedAddons).filter(Boolean).length})
                    </h4>
                    <div className="space-y-1">
                      {Object.entries(form.selectedAddons).filter(([, v]) => v).length === 0 ? (
                        <span className="text-xs text-surface-500">No addons selected</span>
                      ) : (
                        Object.entries(form.selectedAddons)
                          .filter(([, v]) => v)
                          .map(([id]) => (
                            <div key={id} className="flex items-center gap-1.5 text-xs text-surface-300">
                              <CheckCircle className="h-3 w-3 text-emerald-400 shrink-0" />
                              {id}
                            </div>
                          ))
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between p-6 border-t border-surface-700">
          <button
            onClick={() => step > 0 ? setStep(step - 1) : onClose()}
            className="flex items-center gap-1 px-4 py-2 text-sm text-surface-300 hover:text-surface-100 transition-colors"
          >
            <ChevronLeft className="h-4 w-4" />
            {step > 0 ? 'Back' : 'Cancel'}
          </button>

          {step < steps.length - 1 ? (
            <button
              onClick={() => setStep(step + 1)}
              disabled={!canNext()}
              className="flex items-center gap-1 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm transition-colors"
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          ) : (
            <button
              onClick={handleSubmit}
              disabled={createTenant.isPending}
              className="flex items-center gap-2 px-5 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {createTenant.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Create Tenant
            </button>
          )}
        </div>

        {createTenant.isError && (
          <div className="px-6 pb-4">
            <p className="text-sm text-red-400">
              {(createTenant.error as Error)?.message || 'Failed to create tenant'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Delete Confirmation
// ---------------------------------------------------------------------------

function DeleteModal({ name, onConfirm, onCancel, isPending }: {
  name: string; onConfirm: () => void; onCancel: () => void; isPending: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface-900 border border-surface-700 rounded-xl shadow-2xl w-full max-w-md p-6">
        <h3 className="text-lg font-semibold text-surface-100 mb-2">Delete Tenant</h3>
        <p className="text-sm text-surface-400 mb-4">
          Are you sure you want to delete tenant <span className="text-red-400 font-medium">{name}</span>?
          This will destroy the control plane, all worker VMs, and all data inside the tenant cluster.
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={onCancel} className="px-4 py-2 text-sm text-surface-300 hover:text-surface-100">
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isPending}
            className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium"
          >
            {isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function Tenants() {
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const { data: tenantsData, isLoading, refetch } = useTenants();
  const deleteTenantMutation = useDeleteTenant();
  const { selectedNamespace } = useAppStore();

  const allTenants = tenantsData?.items ?? [];
  const filteredTenants = allTenants.filter((t: Tenant) => {
    // Global namespace scope: a tenant belongs to the env namespace
    // `{folder}-{environment}`; show only those matching the selected one.
    if (selectedNamespace && `${t.folder ?? ''}-${t.environment ?? ''}` !== selectedNamespace) {
      return false;
    }
    if (!searchQuery) return true;
    const q = searchQuery.toLowerCase();
    return t.name.toLowerCase().includes(q) || t.display_name.toLowerCase().includes(q);
  });

  const handleDelete = async (name: string) => {
    await deleteTenantMutation.mutateAsync(name);
    setDeleteTarget(null);
  };

  const columns: Column<Tenant>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (t) => (
        <div>
          <span className="font-medium text-surface-100">{t.display_name}</span>
          <p className="text-xs text-surface-500">{t.name}</p>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (t) => <TenantStatusBadge status={t.status} />,
    },
    {
      key: 'version',
      header: 'K8s Version',
      hideOnMobile: true,
      accessor: (t) => <span className="text-xs font-mono text-surface-400">{t.kubernetes_version}</span>,
    },
    {
      key: 'workers',
      header: 'Workers',
      hideOnMobile: true,
      accessor: (t) => (
        <span className="flex items-center gap-1 text-sm">
          <Cpu className="h-3.5 w-3.5 text-surface-500" />
          {t.workers_ready}/{t.worker_count}
        </span>
      ),
    },
    {
      key: 'addons',
      header: 'Addons',
      hideOnMobile: true,
      accessor: (t) => (
        <div className="flex flex-wrap gap-1">
          {t.addons.map(a => (
            <span
              key={a.addon_id}
              className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium ${
                a.ready ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'
              }`}
            >
              {a.ready ? <CheckCircle className="h-2.5 w-2.5" /> : <Clock className="h-2.5 w-2.5" />}
              {a.addon_id}
            </span>
          ))}
        </div>
      ),
    },
  ];

  const getActions = (t: Tenant): MenuItem[] => [
    { label: 'View Details', icon: <Eye className="h-4 w-4" />, onClick: () => navigate(`/tenants/${t.name}`) },
    { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setDeleteTarget(t.name), variant: 'danger' },
  ];

  return (
    <div className="space-y-6">
      <ActionBar
        title="Tenants"
        subtitle="Manage virtual Kubernetes clusters"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        <button onClick={() => setShowCreate(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create Tenant
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filteredTenants}
        loading={isLoading}
        keyExtractor={(t) => t.name}
        actions={getActions}
        onRowClick={(t) => navigate(`/tenants/${t.name}`)}
        searchable
        searchPlaceholder="Search tenants..."
        onSearch={setSearchQuery}
        emptyState={{
          icon: <Box className="h-16 w-16" />,
          title: 'No tenants found',
          description: 'Create your first virtual Kubernetes cluster.',
          action: (
            <button onClick={() => setShowCreate(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create Tenant
            </button>
          ),
        }}
      />

      {showCreate && (
        <CreateTenantWizard
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); refetch(); }}
        />
      )}

      {deleteTarget && (
        <DeleteModal
          name={deleteTarget}
          onConfirm={() => handleDelete(deleteTarget)}
          onCancel={() => setDeleteTarget(null)}
          isPending={deleteTenantMutation.isPending}
        />
      )}
    </div>
  );
}
