/**
 * SecurityBaseline page — toggle-based cluster-wide security preset management.
 */

import { useState } from 'react';
import {
  RefreshCw,
  AlertTriangle,
  ShieldCheck,
  ShieldOff,
  Loader2,
  Trash2,
  Eye,
  Plus,
} from 'lucide-react';
import clsx from 'clsx';
import { useSecurityBaseline, useCreateSecurityBaseline, useDeleteSecurityBaseline } from '../hooks/useSecurityBaseline';
import type { SecurityBaselineResponse } from '../types/cilium_policy';
import { ActionBar } from '../components/common/ActionBar';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { Modal } from '@/components/common/Modal';
import { CiliumRuleBuilder, buildCiliumSpec, DEFAULT_RULE_STATE, type CiliumRuleState } from '../components/security/CiliumRuleBuilder';

// ---------------------------------------------------------------------------
// Preset metadata (matches backend PRESETS dict)
// ---------------------------------------------------------------------------

const PRESET_CARDS = [
  {
    preset: 'default-deny-external',
    title: 'Default Deny External',
    description: 'Block all egress to internet, allow internal cluster traffic (RFC1918 only)',
    icon: '🚫',
    danger: true,
  },
  {
    preset: 'allow-dns',
    title: 'Allow DNS',
    description: 'Allow DNS queries to kube-system. Required when deny-external is enabled.',
    icon: '🔍',
    danger: false,
  },
  {
    preset: 'block-metadata-api',
    title: 'Block Cloud Metadata API',
    description: 'Block access to 169.254.169.254 (AWS/GCP/Azure instance metadata service)',
    icon: '☁️',
    danger: false,
  },
  {
    preset: 'allow-monitoring',
    title: 'Allow Monitoring',
    description: 'Allow Prometheus scrape from monitoring namespace on port 9090',
    icon: '📊',
    danger: false,
  },
] as const;

// ---------------------------------------------------------------------------
// Toggle Card
// ---------------------------------------------------------------------------

function PresetToggleCard({
  title,
  description,
  icon,
  danger,
  active,
  activeItem,
  onToggle,
  isLoading,
}: {
  preset: string;
  title: string;
  description: string;
  icon: string;
  danger: boolean;
  active: boolean;
  activeItem?: SecurityBaselineResponse;
  onToggle: () => void;
  isLoading: boolean;
}) {
  return (
    <div
      className={clsx(
        'bg-surface-800 border rounded-xl p-5 transition-all',
        active ? 'border-primary-500/40' : 'border-surface-700',
      )}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 flex-1">
          <span className="text-2xl shrink-0 mt-0.5">{icon}</span>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-semibold text-surface-100">{title}</h3>
              {active ? (
                <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400">
                  <ShieldCheck className="w-3 h-3" />
                  Active
                </span>
              ) : (
                <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-surface-700 text-surface-500">
                  <ShieldOff className="w-3 h-3" />
                  Inactive
                </span>
              )}
            </div>
            <p className="text-xs text-surface-500 mt-1">{description}</p>
            {danger && !active && (
              <p className="text-xs text-amber-400 mt-1 flex items-center gap-1">
                <AlertTriangle className="w-3 h-3" />
                Will affect all pods cluster-wide
              </p>
            )}
          </div>
        </div>

        {/* Toggle switch */}
        <button
          onClick={onToggle}
          disabled={isLoading}
          className={clsx(
            'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
            'focus:outline-none disabled:opacity-50',
            active ? 'bg-primary-500' : 'bg-surface-600',
          )}
          aria-checked={active}
          role="switch"
        >
          {isLoading ? (
            <Loader2 className="h-3 w-3 text-white animate-spin mx-auto" />
          ) : (
            <span
              className={clsx(
                'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
                active ? 'translate-x-6' : 'translate-x-1',
              )}
            />
          )}
        </button>
      </div>

      {activeItem && (
        <div className="mt-3 pt-3 border-t border-surface-700/50">
          <p className="text-xs text-surface-500">
            Policy: <code className="font-mono text-surface-300">{activeItem.name}</code>
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail modal for custom policies
// ---------------------------------------------------------------------------

function CustomPolicyDetailModal({
  policy,
  onClose,
  onDelete,
  isDeleting,
}: {
  policy: SecurityBaselineResponse;
  onClose: () => void;
  onDelete: () => void;
  isDeleting: boolean;
}) {
  return (
    <Modal isOpen onClose={onClose} title={`Policy: ${policy.name}`} size="lg">
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <p className="text-surface-500">Preset</p>
            <p className="font-mono text-surface-200">{policy.preset}</p>
          </div>
          <div>
            <p className="text-surface-500">Status</p>
            <p className="text-emerald-400">Active</p>
          </div>
        </div>
        {policy.yaml_repr && (
          <div>
            <p className="text-xs text-surface-500 mb-2">Spec</p>
            <pre className="bg-surface-900 border border-surface-700 rounded-lg p-3 text-xs text-surface-300 font-mono overflow-auto max-h-48">
              {policy.yaml_repr}
            </pre>
          </div>
        )}
        <div className="flex justify-between gap-3 pt-2">
          <button
            onClick={onDelete}
            disabled={isDeleting}
            className="flex items-center gap-2 px-4 py-2 bg-red-900/20 hover:bg-red-900/40 border border-red-800/40 text-red-400 rounded-lg text-sm transition-colors disabled:opacity-50"
          >
            <Trash2 className="w-4 h-4" />
            {isDeleting ? 'Deleting...' : 'Delete'}
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
// Main Page
// ---------------------------------------------------------------------------

export default function SecurityBaseline() {
  const { data, isLoading, refetch } = useSecurityBaseline();
  const createBaseline = useCreateSecurityBaseline();
  const deleteBaseline = useDeleteSecurityBaseline();
  const [detailItem, setDetailItem] = useState<SecurityBaselineResponse | null>(null);
  const [showCustomCreate, setShowCustomCreate] = useState(false);
  const [customName, setCustomName] = useState('');
  const [customDescription, setCustomDescription] = useState('');
  const [customRuleState, setCustomRuleState] = useState<CiliumRuleState>(DEFAULT_RULE_STATE);

  const items = data?.items ?? [];

  // Build map preset → active item
  const activeByPreset = new Map<string, SecurityBaselineResponse>();
  for (const item of items) {
    activeByPreset.set(item.preset, item);
  }

  // Custom policies (preset = 'custom' or not in known list)
  const knownPresets = new Set<string>(PRESET_CARDS.map((c) => c.preset));
  const customItems = items.filter((item) => !knownPresets.has(item.preset));

  const handleToggle = async (preset: string) => {
    const active = activeByPreset.get(preset);
    if (active) {
      await deleteBaseline.mutateAsync(active.name);
    } else {
      await createBaseline.mutateAsync({ preset });
    }
  };

  const isPresetLoading = (preset: string) =>
    (createBaseline.isPending && createBaseline.variables?.preset === preset) ||
    (deleteBaseline.isPending && activeByPreset.get(preset)?.name === deleteBaseline.variables);

  const columns: Column<SecurityBaselineResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (p) => <span className="font-mono font-medium text-surface-100">{p.name}</span>,
    },
    {
      key: 'preset',
      header: 'Preset',
      hideOnMobile: true,
      accessor: (p) => <span className="text-surface-400 text-xs">{p.preset}</span>,
    },
    {
      key: 'description',
      header: 'Description',
      hideOnMobile: true,
      accessor: (p) => <span className="text-surface-400 text-xs">{p.description || '-'}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      accessor: () => (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400">
          <ShieldCheck className="w-3 h-3" />
          Active
        </span>
      ),
    },
  ];

  const getActions = (p: SecurityBaselineResponse): MenuItem[] => [
    {
      label: 'View Details',
      icon: <Eye className="h-4 w-4" />,
      onClick: () => setDetailItem(p),
    },
    {
      label: 'Delete',
      icon: <Trash2 className="h-4 w-4" />,
      onClick: () => deleteBaseline.mutate(p.name),
      variant: 'danger',
    },
  ];

  return (
    <div className="space-y-6">
      <ActionBar
        title="Security Baseline"
        subtitle="Cluster-wide CiliumClusterwideNetworkPolicy presets"
      >
        <button onClick={() => refetch()} disabled={isLoading} className="btn-secondary" title="Refresh">
          <RefreshCw className={clsx('h-4 w-4', isLoading && 'animate-spin')} />
        </button>
      </ActionBar>

      {/* Warning banner */}
      <div className="flex items-start gap-3 p-4 bg-amber-900/10 border border-amber-800/30 rounded-xl">
        <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-medium text-amber-300">Cluster-wide Impact</p>
          <p className="text-xs text-amber-400/80 mt-0.5">
            Security baseline rules apply to <strong>ALL pods</strong> in the cluster via CiliumClusterwideNetworkPolicy. Enable carefully.
          </p>
        </div>
      </div>

      {/* Preset toggle cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {PRESET_CARDS.map((card) => (
          <PresetToggleCard
            key={card.preset}
            preset={card.preset}
            title={card.title}
            description={card.description}
            icon={card.icon}
            danger={card.danger}
            active={activeByPreset.has(card.preset)}
            activeItem={activeByPreset.get(card.preset)}
            onToggle={() => handleToggle(card.preset)}
            isLoading={isPresetLoading(card.preset)}
          />
        ))}
      </div>

      {(createBaseline.isError || deleteBaseline.isError) && (
        <div className="flex items-start gap-2 p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
          <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
          <p className="text-sm text-red-400">
            {(createBaseline.error as Error)?.message ||
              (deleteBaseline.error as Error)?.message ||
              'Operation failed'}
          </p>
        </div>
      )}

      {/* Custom rules table */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-surface-300 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-primary-400" />
            Custom Rules
            <span className="text-xs font-normal text-surface-500">({customItems.length})</span>
          </h2>
          <button onClick={() => setShowCustomCreate(true)} className="btn-secondary text-xs flex items-center gap-1.5">
            <Plus className="w-3.5 h-3.5" />
            Add Custom Rule
          </button>
        </div>
        <DataTable
          columns={columns}
          data={customItems}
          loading={isLoading}
          keyExtractor={(p) => p.name}
          actions={getActions}
          onRowClick={(p) => setDetailItem(p)}
          emptyState={{
            icon: <ShieldCheck className="h-10 w-10" />,
            title: 'No custom baseline rules',
            description: 'Custom CiliumClusterwideNetworkPolicies with our label will appear here.',
          }}
        />
      </div>

      {detailItem && (
        <CustomPolicyDetailModal
          policy={detailItem}
          onClose={() => setDetailItem(null)}
          onDelete={() => {
            deleteBaseline.mutate(detailItem.name);
            setDetailItem(null);
          }}
          isDeleting={deleteBaseline.isPending}
        />
      )}

      {/* Custom Rule Create Modal */}
      {showCustomCreate && (
        <Modal
          isOpen
          onClose={() => setShowCustomCreate(false)}
          title="Add Custom Baseline Rule"
          size="lg"
        >
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-surface-300 mb-1">Rule Name *</label>
                <input
                  type="text"
                  value={customName}
                  onChange={(e) => setCustomName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                  placeholder="my-rule"
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-surface-300 mb-1">Description</label>
                <input
                  type="text"
                  value={customDescription}
                  onChange={(e) => setCustomDescription(e.target.value)}
                  placeholder="What this rule does"
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 text-sm"
                />
              </div>
            </div>

            <CiliumRuleBuilder
              value={customRuleState}
              onChange={setCustomRuleState}
              hideEndpointSelector
            />

            <div className="flex justify-end gap-3 pt-2">
              <button onClick={() => setShowCustomCreate(false)} className="btn-secondary">Cancel</button>
              <button
                onClick={async () => {
                  await createBaseline.mutateAsync({
                    preset: 'custom',
                    name: customName,
                    description: customDescription,
                    custom_spec: buildCiliumSpec(customRuleState),
                  });
                  setShowCustomCreate(false);
                  setCustomName('');
                  setCustomDescription('');
                  setCustomRuleState(DEFAULT_RULE_STATE);
                }}
                disabled={!customName || createBaseline.isPending}
                className="btn-primary"
              >
                {createBaseline.isPending ? 'Creating...' : 'Create Rule'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
