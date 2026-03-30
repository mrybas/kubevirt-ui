/**
 * Tenants Management Page
 *
 * List tenants + Create Tenant wizard (multi-step)
 */

import { useState, useEffect } from 'react';
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
  Server,
  Cpu,
  Eye,
  Trash2,
} from 'lucide-react';
import { useTenants, useCreateTenant, useDeleteTenant, useAddonCatalog, useDiscovery } from '../hooks/useTenants';
import { useSubnets } from '../hooks/useNetwork';
import { useEgressGateways } from '../hooks/useEgressGateways';
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

const K8S_VERSIONS = ['v1.32.0', 'v1.31.5', 'v1.31.0', 'v1.30.0'];

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
  pod_cidr: string;
  service_cidr: string;
  admin_group: string;
  viewer_group: string;
  network_isolation: boolean;
  egress_gateway: string; // gateway name or '' for none
  selectedAddons: Record<string, boolean>;
  addonParams: Record<string, Record<string, string>>;
}

const defaultWizard: WizardState = {
  name: '',
  display_name: '',
  kubernetes_version: 'v1.30.0',
  control_plane_replicas: 2,
  worker_type: 'vm',
  worker_count: 2,
  worker_vcpu: 2,
  worker_memory: '2Gi',
  worker_disk: '20Gi',
  pod_cidr: '10.244.0.0/16',
  service_cidr: '10.96.0.0/12',
  admin_group: '',
  viewer_group: '',
  network_isolation: false,
  egress_gateway: '',
  selectedAddons: {},
  addonParams: {},
};

function CreateTenantWizard({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<WizardState>(defaultWizard);
  const { data: catalog } = useAddonCatalog();
  const { data: discovery } = useDiscovery();
  const { data: subnets } = useSubnets();
  const { data: egressGatewaysData } = useEgressGateways();
  const createTenant = useCreateTenant();

  // Check if infrastructure subnet exists (required for VPC network isolation)
  const hasInfraSubnet = subnets?.some(s => s.purpose === 'infrastructure') ?? false;

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
      admin_group: form.admin_group,
      viewer_group: form.viewer_group,
      network_isolation: form.network_isolation || undefined,
      egress_gateway: form.egress_gateway || undefined,
      addons,
    };

    await createTenant.mutateAsync(request);
    onCreated();
  };

  const canNext = () => {
    if (step === 0) return form.name.length > 0 && form.display_name.length > 0;
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
            </>
          )}

          {step === 1 && (
            <>
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
            </>
          )}

          {step === 2 && (
            <>
              {/* Discovery status */}
              {discovery && (
                <div className="p-3 bg-surface-800/50 rounded-lg border border-surface-700 mb-4 space-y-1">
                  <p className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-1">Auto-detected infrastructure</p>
                  {discovery.storage.map(s => (
                    <p key={s.api_url} className="text-xs text-emerald-400 flex items-center gap-1.5">
                      <CheckCircle className="h-3 w-3" />
                      Linstor — {s.pools.length} pool{s.pools.length !== 1 ? 's' : ''}
                      ({s.pools.map(p => `${p.name}: ${p.free_gb}GB free`).join(', ')})
                    </p>
                  ))}
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

              {/* Network Isolation */}
              <div className={`mt-4 flex items-center justify-between p-4 bg-surface-800 rounded-lg border ${
                hasInfraSubnet ? 'border-surface-700' : 'border-surface-700/50 opacity-60'
              }`}>
                <div>
                  <h3 className="text-sm font-semibold text-surface-200">Network Isolation (VPC)</h3>
                  <p className="text-xs text-surface-500 mt-1">
                    {hasInfraSubnet
                      ? 'Create a dedicated VPC for this tenant. Worker VMs will be isolated in their own network.'
                      : 'Requires an infrastructure subnet for VPC NAT gateway. Create one in Networks first.'}
                  </p>
                </div>
                <button
                  type="button"
                  disabled={!hasInfraSubnet}
                  onClick={() => hasInfraSubnet && setForm({ ...form, network_isolation: !form.network_isolation })}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                    !hasInfraSubnet ? 'bg-surface-700 cursor-not-allowed' :
                    form.network_isolation ? 'bg-primary-500' : 'bg-surface-600'
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      form.network_isolation ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  />
                </button>
              </div>

              {/* Egress Gateway */}
              <div className="mt-4 p-4 bg-surface-800 rounded-lg border border-surface-700">
                <div className="flex items-start justify-between mb-2">
                  <div>
                    <h3 className="text-sm font-semibold text-surface-200">Egress Gateway</h3>
                    <p className="text-xs text-surface-500 mt-1">
                      Route internet traffic from this tenant's VPC through a shared egress gateway.
                      Leave as "None" for no internet access.
                    </p>
                  </div>
                </div>
                <select
                  value={form.egress_gateway}
                  onChange={e => setForm({ ...form, egress_gateway: e.target.value })}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                >
                  <option value="">None (no internet)</option>
                  {(egressGatewaysData?.items ?? []).map(gw => (
                    <option key={gw.name} value={gw.name}>
                      {gw.name} — {gw.ready ? 'Ready' : 'Not Ready'} ({gw.replicas} replicas)
                    </option>
                  ))}
                </select>
                {(egressGatewaysData?.items ?? []).length === 0 && (
                  <p className="text-xs text-surface-500 mt-1">
                    No egress gateways available. Create one in Network → Egress Gateways first.
                  </p>
                )}
              </div>

              {/* RBAC — DEX group mapping */}
              <div className="mt-4">
                <h3 className="text-sm font-semibold text-surface-200 mb-2">OIDC / RBAC Access</h3>
                <p className="text-xs text-surface-500 mb-3">
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
                    </div>
                  </div>

                  <div className="p-4 bg-surface-800 rounded-lg border border-surface-700">
                    <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-3">Network</h4>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                      <span className="text-surface-500">Pod CIDR</span>
                      <span className="text-surface-200 font-mono">{form.pod_cidr}</span>
                      <span className="text-surface-500">Service CIDR</span>
                      <span className="text-surface-200 font-mono">{form.service_cidr}</span>
                      <span className="text-surface-500">Isolation</span>
                      <span className="text-surface-200">{form.network_isolation ? 'VPC (isolated)' : 'Shared'}</span>
                      <span className="text-surface-500">Egress Gateway</span>
                      <span className="text-surface-200">{form.egress_gateway || 'None'}</span>
                      {form.admin_group && (
                        <>
                          <span className="text-surface-500">Admin Group</span>
                          <span className="text-surface-200">{form.admin_group}</span>
                        </>
                      )}
                      {form.viewer_group && (
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

  const allTenants = tenantsData?.items ?? [];
  const filteredTenants = searchQuery
    ? allTenants.filter(
        (t: Tenant) =>
          t.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
          t.display_name.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : allTenants;

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
