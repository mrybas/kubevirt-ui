/**
 * CiliumPolicies page — manage CiliumNetworkPolicies with template wizard.
 */

import { useState } from 'react';
import {
  Plus,
  RefreshCw,
  Trash2,
  Shield,
  Eye,
  CheckCircle,
  Clock,
  ArrowLeft,
  ArrowRight,
  Check,
  X,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import { useCiliumPolicies, useCreateCiliumPolicy, useDeleteCiliumPolicy } from '../hooks/useCiliumPolicies';
import { useNamespaces } from '../hooks/useNamespaces';
import type { CiliumPolicyResponse, CiliumPolicyCreateRequest } from '../types/cilium_policy';
import { ActionBar } from '../components/common/ActionBar';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { Modal } from '@/components/common/Modal';
import { WizardStepIndicator } from '../components/common/WizardStepIndicator';
import { CiliumRuleBuilder, buildCiliumSpec, DEFAULT_RULE_STATE, type CiliumRuleState } from '../components/security/CiliumRuleBuilder';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function StatusBadge({ ready }: { ready: boolean }) {
  return ready ? (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-emerald-400 bg-emerald-500/10">
      <CheckCircle className="h-3.5 w-3.5" />
      Enforced
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-amber-400 bg-amber-500/10">
      <Clock className="h-3.5 w-3.5" />
      Pending
    </span>
  );
}

function policyType(policy: CiliumPolicyResponse): string {
  const spec = policy.spec as Record<string, unknown>;
  if (spec.egress && Array.isArray(spec.egress)) {
    const egress = spec.egress as Array<Record<string, unknown>>;
    if (egress.some((e) => e.toFQDNs)) return 'DNS';
    const ports = egress.flatMap((e) => (e.toPorts as Array<Record<string, unknown>> | undefined) ?? []);
    if (ports.some((p) => (p.rules as Record<string, unknown> | undefined)?.http)) return 'HTTP';
  }
  if (spec.egressDeny) return 'Block';
  return 'Custom';
}

// ---------------------------------------------------------------------------
// Detail Modal
// ---------------------------------------------------------------------------

function PolicyDetailModal({
  policy,
  onClose,
  onDelete,
  isDeleting,
}: {
  policy: CiliumPolicyResponse;
  onClose: () => void;
  onDelete: () => void;
  isDeleting: boolean;
}) {
  return (
    <Modal isOpen onClose={onClose} title={`Policy: ${policy.name}`} size="lg">
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <p className="text-surface-500">Namespace</p>
            <p className="font-mono text-surface-200">{policy.namespace}</p>
          </div>
          <div>
            <p className="text-surface-500">Type</p>
            <p className="text-surface-200">{policyType(policy)}</p>
          </div>
          <div>
            <p className="text-surface-500">Status</p>
            <div className="mt-0.5"><StatusBadge ready={policy.ready} /></div>
          </div>
        </div>

        {policy.yaml_repr && (
          <div>
            <p className="text-xs text-surface-500 mb-2">Spec (YAML)</p>
            <pre className="bg-surface-900 border border-surface-700 rounded-lg p-3 text-xs text-surface-300 font-mono overflow-auto max-h-64">
              {policy.yaml_repr}
            </pre>
          </div>
        )}

        <div className="flex justify-between gap-3 pt-2">
          <button
            onClick={onDelete}
            disabled={isDeleting}
            className="flex items-center gap-2 px-4 py-2 bg-red-900/20 hover:bg-red-900/40 border border-red-800/40 text-red-400 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
          >
            <Trash2 className="w-4 h-4" />
            {isDeleting ? 'Deleting...' : 'Delete Policy'}
          </button>
          <button onClick={onClose} className="btn-secondary">
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Create Wizard
// ---------------------------------------------------------------------------

const TEMPLATES = [
  {
    id: 'dns-allow',
    title: 'DNS Egress Allow',
    description: 'Allow outbound to specific domains using FQDN matching',
    icon: '🌐',
  },
  {
    id: 'http-filter',
    title: 'HTTP Filter',
    description: 'Allow HTTP traffic filtered by method and path patterns',
    icon: '🔀',
  },
  {
    id: 'block-egress',
    title: 'Block All Egress',
    description: 'Deny all outbound traffic from selected pods',
    icon: '🚫',
  },
  {
    id: 'rule-builder',
    title: 'Rule Builder',
    description: 'Visual builder for egress/ingress rules with FQDN, CIDR, L7 filtering',
    icon: '🔧',
  },
  {
    id: 'custom',
    title: 'Custom JSON',
    description: 'Write raw JSON spec for advanced use cases',
    icon: '⚙️',
  },
] as const;

type TemplateId = (typeof TEMPLATES)[number]['id'];

const WIZARD_STEPS = ['Template', 'Configure', 'Review'];

interface CreateWizardState {
  name: string;
  namespace: string;
  template: TemplateId;
  fqdns: string[];
  fqdnInput: string;
  httpMethods: string[];
  httpPaths: string[];
  httpPathInput: string;
  customYaml: string;
  ruleState: CiliumRuleState;
}

function CreatePolicyWizard({
  onClose,
  namespaces,
}: {
  onClose: () => void;
  namespaces: string[];
}) {
  const [step, setStep] = useState(0);
  const [state, setState] = useState<CreateWizardState>({
    name: '',
    namespace: namespaces[0] ?? 'default',
    template: 'dns-allow',
    fqdns: [],
    fqdnInput: '',
    httpMethods: ['GET'],
    httpPaths: ['/'],
    httpPathInput: '',
    customYaml: '',
    ruleState: DEFAULT_RULE_STATE,
  });

  const createPolicy = useCreateCiliumPolicy();

  const set = <K extends keyof CreateWizardState>(key: K, value: CreateWizardState[K]) =>
    setState((prev) => ({ ...prev, [key]: value }));

  const addFqdn = () => {
    const v = state.fqdnInput.trim();
    if (v && !state.fqdns.includes(v)) {
      set('fqdns', [...state.fqdns, v]);
      set('fqdnInput', '');
    }
  };

  const addPath = () => {
    const v = state.httpPathInput.trim();
    if (v && !state.httpPaths.includes(v)) {
      set('httpPaths', [...state.httpPaths, v]);
      set('httpPathInput', '');
    }
  };

  const toggleMethod = (m: string) => {
    set(
      'httpMethods',
      state.httpMethods.includes(m)
        ? state.httpMethods.filter((x) => x !== m)
        : [...state.httpMethods, m],
    );
  };

  const canNext = () => {
    if (step === 0) return true;
    if (step === 1) {
      if (!state.name || !state.namespace) return false;
      if (state.template === 'dns-allow') return state.fqdns.length > 0;
      if (state.template === 'http-filter') return state.httpMethods.length > 0;
      if (state.template === 'custom') return state.customYaml.trim().length > 0;
      if (state.template === 'rule-builder') return state.ruleState.egressEnabled || state.ruleState.ingressEnabled;
      return true;
    }
    return true;
  };

  const handleCreate = async () => {
    const req: CiliumPolicyCreateRequest = {
      name: state.name,
      namespace: state.namespace,
    };

    if (state.template === 'rule-builder') {
      req.custom_spec = buildCiliumSpec(state.ruleState);
    } else if (state.template === 'custom') {
      try {
        req.custom_spec = JSON.parse(state.customYaml);
      } catch {
        return;
      }
    } else {
      req.template = state.template;
      if (state.template === 'dns-allow') req.allowed_fqdns = state.fqdns;
      if (state.template === 'http-filter') {
        req.allowed_http_methods = state.httpMethods;
        req.allowed_http_paths = state.httpPaths;
      }
    }

    await createPolicy.mutateAsync(req);
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-2xl mx-4 shadow-2xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-5 border-b border-surface-700 shrink-0">
          <h2 className="text-lg font-semibold text-surface-100">Create Cilium Policy</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-5 pt-4 shrink-0">
          <WizardStepIndicator
            steps={WIZARD_STEPS}
            currentStep={step}
            onStepClick={(s) => { if (s < step) setStep(s); }}
          />
        </div>

        <div className="p-5 overflow-y-auto flex-1">
          {/* Step 0: Choose template */}
          {step === 0 && (
            <div className="grid grid-cols-2 gap-3">
              {TEMPLATES.map((t) => (
                <button
                  key={t.id}
                  onClick={() => set('template', t.id)}
                  className={clsx(
                    'text-left p-4 rounded-xl border transition-all',
                    state.template === t.id
                      ? 'border-primary-500 bg-primary-500/5'
                      : 'border-surface-700 hover:border-surface-600 bg-surface-900/50',
                  )}
                >
                  <div className="text-2xl mb-2">{t.icon}</div>
                  <p className="text-sm font-medium text-surface-100">{t.title}</p>
                  <p className="text-xs text-surface-500 mt-1">{t.description}</p>
                </button>
              ))}
            </div>
          )}

          {/* Step 1: Configure */}
          {step === 1 && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-surface-300 mb-1">Name *</label>
                  <input
                    type="text"
                    value={state.name}
                    onChange={(e) => set('name', e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                    placeholder="my-policy"
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                    required
                  />
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-1">Namespace *</label>
                  <select
                    value={state.namespace}
                    onChange={(e) => set('namespace', e.target.value)}
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                  >
                    {namespaces.map((ns) => (
                      <option key={ns} value={ns}>{ns}</option>
                    ))}
                  </select>
                </div>
              </div>

              {/* DNS Allow */}
              {state.template === 'dns-allow' && (
                <div>
                  <label className="block text-sm text-surface-300 mb-1">Allowed Domains</label>
                  <div className="flex gap-2 mb-2">
                    <input
                      type="text"
                      value={state.fqdnInput}
                      onChange={(e) => set('fqdnInput', e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addFqdn())}
                      placeholder="google.com, *.github.com"
                      className="flex-1 px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                    />
                    <button type="button" onClick={addFqdn} className="btn-secondary">
                      Add
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {state.fqdns.map((d) => (
                      <span key={d} className="flex items-center gap-1 px-2 py-1 bg-surface-700 rounded text-xs font-mono text-surface-200">
                        {d}
                        <button onClick={() => set('fqdns', state.fqdns.filter((x) => x !== d))}>
                          <X className="w-3 h-3 text-surface-400 hover:text-red-400" />
                        </button>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* HTTP Filter */}
              {state.template === 'http-filter' && (
                <div className="space-y-3">
                  <div>
                    <label className="block text-sm text-surface-300 mb-2">HTTP Methods</label>
                    <div className="flex gap-2 flex-wrap">
                      {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => (
                        <button
                          key={m}
                          type="button"
                          onClick={() => toggleMethod(m)}
                          className={clsx(
                            'px-3 py-1.5 text-xs rounded-lg border transition-colors font-mono',
                            state.httpMethods.includes(m)
                              ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                              : 'border-surface-700 text-surface-400 hover:border-surface-600',
                          )}
                        >
                          {m}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <label className="block text-sm text-surface-300 mb-1">Path Patterns</label>
                    <div className="flex gap-2 mb-2">
                      <input
                        type="text"
                        value={state.httpPathInput}
                        onChange={(e) => set('httpPathInput', e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addPath())}
                        placeholder="/api/v1/.*"
                        className="flex-1 px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                      />
                      <button type="button" onClick={addPath} className="btn-secondary">Add</button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {state.httpPaths.map((p) => (
                        <span key={p} className="flex items-center gap-1 px-2 py-1 bg-surface-700 rounded text-xs font-mono text-surface-200">
                          {p}
                          <button onClick={() => set('httpPaths', state.httpPaths.filter((x) => x !== p))}>
                            <X className="w-3 h-3 text-surface-400 hover:text-red-400" />
                          </button>
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* Block — no extra config needed */}
              {state.template === 'block-egress' && (
                <div className="p-4 bg-red-900/10 border border-red-800/30 rounded-lg">
                  <p className="text-sm text-red-400 flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4" />
                    This will deny ALL egress from pods in the selected namespace.
                  </p>
                </div>
              )}

              {/* Rule Builder */}
              {state.template === 'rule-builder' && (
                <CiliumRuleBuilder
                  value={state.ruleState}
                  onChange={(rs) => set('ruleState', rs)}
                />
              )}

              {/* Custom YAML/JSON */}
              {state.template === 'custom' && (
                <div>
                  <label className="block text-sm text-surface-300 mb-1">
                    Spec (JSON)
                    <span className="text-surface-500 ml-2 font-normal text-xs">CiliumNetworkPolicy .spec field</span>
                  </label>
                  <textarea
                    value={state.customYaml}
                    onChange={(e) => set('customYaml', e.target.value)}
                    rows={10}
                    placeholder='{"endpointSelector": {}, "egress": [...]}'
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-xs"
                  />
                </div>
              )}
            </div>
          )}

          {/* Step 2: Review */}
          {step === 2 && (
            <div className="space-y-4">
              <div className="border border-surface-700 rounded-lg p-4 space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-surface-400">Name</span>
                  <span className="font-mono text-surface-200">{state.name}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-surface-400">Namespace</span>
                  <span className="font-mono text-surface-200">{state.namespace}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-surface-400">Template</span>
                  <span className="text-surface-200">{state.template}</span>
                </div>
                {state.template === 'dns-allow' && (
                  <div className="flex justify-between">
                    <span className="text-surface-400">Domains</span>
                    <span className="text-surface-200">{state.fqdns.join(', ')}</span>
                  </div>
                )}
                {state.template === 'http-filter' && (
                  <>
                    <div className="flex justify-between">
                      <span className="text-surface-400">Methods</span>
                      <span className="text-surface-200">{state.httpMethods.join(', ')}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-surface-400">Paths</span>
                      <span className="text-surface-200">{state.httpPaths.join(', ')}</span>
                    </div>
                  </>
                )}
              </div>

              {(state.template === 'rule-builder' || state.template === 'custom') && (
                <div>
                  <p className="text-xs text-surface-500 mb-2">Generated spec</p>
                  <pre className="bg-surface-900 border border-surface-700 rounded-lg p-3 text-xs text-surface-300 font-mono overflow-auto max-h-48">
                    {JSON.stringify(
                      state.template === 'rule-builder' ? buildCiliumSpec(state.ruleState) : (() => { try { return JSON.parse(state.customYaml); } catch { return state.customYaml; } })(),
                      null, 2
                    )}
                  </pre>
                </div>
              )}

              {createPolicy.isError && (
                <div className="flex items-start gap-2 p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
                  <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
                  <p className="text-sm text-red-400">
                    {(createPolicy.error as Error)?.message || 'Failed to create policy'}
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between p-5 border-t border-surface-700 shrink-0">
          <button
            type="button"
            onClick={() => (step === 0 ? onClose() : setStep(step - 1))}
            className="btn-secondary flex items-center gap-1.5"
          >
            <ArrowLeft className="w-4 h-4" />
            {step === 0 ? 'Cancel' : 'Back'}
          </button>
          {step < 2 ? (
            <button
              type="button"
              onClick={() => setStep(step + 1)}
              disabled={!canNext()}
              className="btn-primary flex items-center gap-1.5"
            >
              Next
              <ArrowRight className="w-4 h-4" />
            </button>
          ) : (
            <button
              type="button"
              onClick={handleCreate}
              disabled={createPolicy.isPending}
              className="btn-primary flex items-center gap-1.5"
            >
              {createPolicy.isPending ? 'Creating...' : (
                <>
                  <Check className="w-4 h-4" />
                  Create Policy
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function CiliumPolicies() {
  const [showCreate, setShowCreate] = useState(false);
  const [detailPolicy, setDetailPolicy] = useState<CiliumPolicyResponse | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const { data, isLoading, refetch } = useCiliumPolicies();
  const deletePolicy = useDeleteCiliumPolicy();
  const { data: namespacesData } = useNamespaces();
  const namespaces = namespacesData?.items.map((n) => n.name) ?? ['default'];

  const items = data?.items ?? [];
  const filtered = searchQuery
    ? items.filter(
        (p) =>
          p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
          p.namespace.toLowerCase().includes(searchQuery.toLowerCase()),
      )
    : items;

  const handleDelete = async (policy: CiliumPolicyResponse) => {
    await deletePolicy.mutateAsync({ namespace: policy.namespace, name: policy.name });
    setDetailPolicy(null);
  };

  const columns: Column<CiliumPolicyResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (p) => <span className="font-mono font-medium text-surface-100">{p.name}</span>,
    },
    {
      key: 'namespace',
      header: 'Namespace',
      hideOnMobile: true,
      accessor: (p) => <span className="font-mono text-surface-300">{p.namespace}</span>,
    },
    {
      key: 'type',
      header: 'Type',
      hideOnMobile: true,
      accessor: (p) => <span className="text-surface-400 text-xs">{policyType(p)}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (p) => <StatusBadge ready={p.ready} />,
    },
  ];

  const getActions = (p: CiliumPolicyResponse): MenuItem[] => [
    {
      label: 'View Details',
      icon: <Eye className="h-4 w-4" />,
      onClick: () => setDetailPolicy(p),
    },
    {
      label: 'Delete',
      icon: <Trash2 className="h-4 w-4" />,
      onClick: () => handleDelete(p),
      variant: 'danger',
    },
  ];

  return (
    <div className="space-y-6">
      <ActionBar
        title="Cilium Policies"
        subtitle="CiliumNetworkPolicy resources — namespace-scoped network policy enforcement"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        <button onClick={() => setShowCreate(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create Policy
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filtered}
        loading={isLoading}
        keyExtractor={(p) => `${p.namespace}/${p.name}`}
        actions={getActions}
        onRowClick={(p) => setDetailPolicy(p)}
        searchable
        searchPlaceholder="Search policies..."
        onSearch={setSearchQuery}
        emptyState={{
          icon: <Shield className="h-16 w-16" />,
          title: 'No Cilium policies',
          description: 'Create a CiliumNetworkPolicy to control pod-level network traffic.',
          action: (
            <button onClick={() => setShowCreate(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create Policy
            </button>
          ),
        }}
      />

      {showCreate && (
        <CreatePolicyWizard namespaces={namespaces} onClose={() => setShowCreate(false)} />
      )}

      {detailPolicy && (
        <PolicyDetailModal
          policy={detailPolicy}
          onClose={() => setDetailPolicy(null)}
          onDelete={() => handleDelete(detailPolicy)}
          isDeleting={deletePolicy.isPending}
        />
      )}
    </div>
  );
}
