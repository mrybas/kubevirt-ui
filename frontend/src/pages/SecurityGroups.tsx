/**
 * SecurityGroup List Page
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Shield,
  Plus,
  RefreshCw,
  Trash2,
  X,
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  Pencil,
  ArrowDownToLine,
  ArrowUpFromLine,
} from 'lucide-react';
import { useSecurityGroups, useCreateSecurityGroup, useDeleteSecurityGroup } from '../hooks/useSecurityGroups';
import { WizardStepIndicator } from '../components/common/WizardStepIndicator';
import type { SecurityGroup, SecurityGroupRule } from '../types/vpc';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

function hasGatewayAllowRule(sg: SecurityGroup): boolean {
  // Check if there's a high-priority (1) allow rule for gateway traffic
  return sg.egress_rules.some((r) => r.priority === 1 && r.action === 'allow') ||
         sg.ingress_rules.some((r) => r.priority === 1 && r.action === 'allow');
}

export default function SecurityGroups() {
  const navigate = useNavigate();
  const [searchQuery, setSearchQuery] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState<string | null>(null);

  const { data, isLoading, refetch } = useSecurityGroups();
  const deleteSg = useDeleteSecurityGroup();

  const items = data?.items ?? [];
  const filtered = searchQuery
    ? items.filter((sg) => sg.name.toLowerCase().includes(searchQuery.toLowerCase()))
    : items;

  const handleDelete = async (name: string) => {
    await deleteSg.mutateAsync(name);
    setShowDeleteModal(null);
  };

  const columns: Column<SecurityGroup>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (sg) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-surface-100">{sg.name}</span>
          {!hasGatewayAllowRule(sg) && (
            <span className="flex items-center gap-1 text-xs text-amber-400 bg-amber-900/20 px-2 py-0.5 rounded-full shrink-0">
              <AlertTriangle className="w-3 h-3" />
              No gateway rule
            </span>
          )}
        </div>
      ),
    },
    {
      key: 'ingress',
      header: 'Ingress Rules',
      hideOnMobile: true,
      accessor: (sg) => <span>{sg.ingress_rules.length}</span>,
    },
    {
      key: 'egress',
      header: 'Egress Rules',
      hideOnMobile: true,
      accessor: (sg) => <span>{sg.egress_rules.length}</span>,
    },
  ];

  const getActions = (sg: SecurityGroup): MenuItem[] => [
    { label: 'Edit', icon: <Pencil className="h-4 w-4" />, onClick: () => navigate(`/network/security-groups/${sg.name}`) },
    { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setShowDeleteModal(sg.name), variant: 'danger' },
  ];

  const renderExpandedRow = (sg: SecurityGroup) => {
    if (sg.ingress_rules.length === 0 && sg.egress_rules.length === 0) return null;
    return (
      <div className="grid grid-cols-2 gap-6">
        <div>
          <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2 flex items-center gap-1">
            <ArrowDownToLine className="h-3 w-3" /> Ingress Rules
          </h4>
          {sg.ingress_rules.length === 0 ? (
            <p className="text-xs text-surface-500">No ingress rules</p>
          ) : (
            <div className="space-y-1">
              {sg.ingress_rules.map((r, i) => (
                <div key={i} className="flex items-center gap-3 text-xs bg-surface-800/50 rounded px-2 py-1.5">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${r.action === 'allow' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400'}`}>
                    {r.action}
                  </span>
                  <span className="text-surface-400">{r.protocol}</span>
                  <span className="font-mono text-surface-300">{r.remote_address || '*'}</span>
                  {r.port_range && <span className="text-surface-500">:{r.port_range}</span>}
                  <span className="text-surface-600 ml-auto">pri {r.priority}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2 flex items-center gap-1">
            <ArrowUpFromLine className="h-3 w-3" /> Egress Rules
          </h4>
          {sg.egress_rules.length === 0 ? (
            <p className="text-xs text-surface-500">No egress rules</p>
          ) : (
            <div className="space-y-1">
              {sg.egress_rules.map((r, i) => (
                <div key={i} className="flex items-center gap-3 text-xs bg-surface-800/50 rounded px-2 py-1.5">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${r.action === 'allow' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400'}`}>
                    {r.action}
                  </span>
                  <span className="text-surface-400">{r.protocol}</span>
                  <span className="font-mono text-surface-300">{r.remote_address || '*'}</span>
                  {r.port_range && <span className="text-surface-500">:{r.port_range}</span>}
                  <span className="text-surface-600 ml-auto">pri {r.priority}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <ActionBar
        title="Security Groups"
        subtitle="Firewall rules for VM network traffic — ingress and egress policies"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        <button onClick={() => setShowCreateModal(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create Security Group
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filtered}
        loading={isLoading}
        keyExtractor={(sg) => sg.name}
        actions={getActions}
        expandable={renderExpandedRow}
        onRowClick={(sg) => navigate(`/network/security-groups/${sg.name}`)}
        searchable
        searchPlaceholder="Search security groups..."
        onSearch={setSearchQuery}
        emptyState={{
          icon: <Shield className="h-16 w-16" />,
          title: 'No security groups yet',
          description: 'Create a security group to define firewall rules.',
          action: (
            <button onClick={() => setShowCreateModal(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create your first security group
            </button>
          ),
        }}
      />

      {showCreateModal && (
        <CreateSecurityGroupModal onClose={() => setShowCreateModal(false)} />
      )}

      {showDeleteModal && (
        <DeleteSgModal
          sgName={showDeleteModal}
          onConfirm={() => handleDelete(showDeleteModal)}
          onCancel={() => setShowDeleteModal(null)}
          isDeleting={deleteSg.isPending}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CreateSecurityGroupModal
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Rule helpers
// ---------------------------------------------------------------------------

function emptyRule(): SecurityGroupRule {
  return { priority: 100, protocol: 'tcp', port_range: '', remote_address: '0.0.0.0/0', action: 'allow' };
}

const RULE_TEMPLATES: { label: string; rule: Partial<SecurityGroupRule> }[] = [
  { label: 'SSH',    rule: { protocol: 'tcp', port_range: '22',  action: 'allow' } },
  { label: 'HTTP',   rule: { protocol: 'tcp', port_range: '80',  action: 'allow' } },
  { label: 'HTTPS',  rule: { protocol: 'tcp', port_range: '443', action: 'allow' } },
  { label: 'ICMP',   rule: { protocol: 'icmp', port_range: '',   action: 'allow' } },
  { label: 'All Out', rule: { protocol: 'all', port_range: '',   action: 'allow', remote_address: '0.0.0.0/0' } },
];

const SG_WIZARD_STEPS = ['Basic Info', 'Rules', 'Review'];

// ---------------------------------------------------------------------------
// CreateSecurityGroupWizard
// ---------------------------------------------------------------------------

function CreateSecurityGroupModal({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [ingressRules, setIngressRules] = useState<SecurityGroupRule[]>([]);
  const [egressRules, setEgressRules] = useState<SecurityGroupRule[]>([]);
  const createSg = useCreateSecurityGroup();

  const canNext = step === 0 ? name.length > 0 : true;

  const handleCreate = async () => {
    await createSg.mutateAsync({
      name,
      ingress_rules: ingressRules,
      egress_rules: egressRules,
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-3xl mx-4 shadow-2xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-5 border-b border-surface-700 shrink-0">
          <h2 className="text-lg font-semibold text-surface-100">Create Security Group</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Step Indicator */}
        <div className="px-5 pt-4 shrink-0">
          <WizardStepIndicator
            steps={SG_WIZARD_STEPS}
            currentStep={step}
            onStepClick={(s) => { if (s < step) setStep(s); }}
          />
        </div>

        {/* Content */}
        <div className="p-5 overflow-y-auto flex-1">
          {step === 0 && (
            <StepBasicInfo name={name} setName={setName} description={description} setDescription={setDescription} />
          )}
          {step === 1 && (
            <StepRules
              ingressRules={ingressRules}
              setIngressRules={setIngressRules}
              egressRules={egressRules}
              setEgressRules={setEgressRules}
            />
          )}
          {step === 2 && (
            <StepReview
              name={name}
              description={description}
              ingressRules={ingressRules}
              egressRules={egressRules}
              onEditStep={setStep}
            />
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
              disabled={!canNext}
              className="btn-primary flex items-center gap-1.5"
            >
              Next
              <ArrowRight className="w-4 h-4" />
            </button>
          ) : (
            <button
              type="button"
              onClick={handleCreate}
              disabled={createSg.isPending}
              className="btn-primary flex items-center gap-1.5"
            >
              {createSg.isPending ? 'Creating...' : (
                <>
                  <Check className="w-4 h-4" />
                  Create Security Group
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
// Step 1: Basic Info
// ---------------------------------------------------------------------------

function StepBasicInfo({
  name, setName, description, setDescription,
}: {
  name: string; setName: (v: string) => void;
  description: string; setDescription: (v: string) => void;
}) {
  return (
    <div className="space-y-4 max-w-md">
      <div>
        <label className="block text-sm font-medium text-surface-300 mb-1">Name *</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
          placeholder="my-security-group"
          className="input w-full font-mono text-sm"
        />
        <p className="text-xs text-surface-500 mt-1">Lowercase letters, numbers, and hyphens only.</p>
      </div>
      <div>
        <label className="block text-sm font-medium text-surface-300 mb-1">Description</label>
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Optional description"
          className="input w-full text-sm"
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2: Rules
// ---------------------------------------------------------------------------

function StepRules({
  ingressRules, setIngressRules, egressRules, setEgressRules,
}: {
  ingressRules: SecurityGroupRule[]; setIngressRules: (r: SecurityGroupRule[]) => void;
  egressRules: SecurityGroupRule[]; setEgressRules: (r: SecurityGroupRule[]) => void;
}) {
  return (
    <div className="space-y-6">
      <RuleSection
        title="Inbound Rules (Ingress)"
        rules={ingressRules}
        setRules={setIngressRules}
        templates={RULE_TEMPLATES.filter((t) => t.label !== 'All Out')}
      />
      <RuleSection
        title="Outbound Rules (Egress)"
        rules={egressRules}
        setRules={setEgressRules}
        templates={RULE_TEMPLATES}
      />
    </div>
  );
}

function RuleSection({
  title, rules, setRules, templates,
}: {
  title: string;
  rules: SecurityGroupRule[];
  setRules: (r: SecurityGroupRule[]) => void;
  templates: typeof RULE_TEMPLATES;
}) {
  const addRule = (base?: Partial<SecurityGroupRule>) => {
    const nextPriority = rules.length > 0
      ? Math.max(...rules.map(r => r.priority)) + 100
      : 100;
    setRules([...rules, { ...emptyRule(), priority: nextPriority, ...base }]);
  };

  const updateRule = (idx: number, field: keyof SecurityGroupRule, value: string | number) => {
    const updated = [...rules];
    updated[idx] = { ...updated[idx]!, [field]: value } as SecurityGroupRule;
    if (field === 'protocol' && (value === 'icmp' || value === 'all')) {
      updated[idx]!.port_range = '';
    }
    setRules(updated);
  };

  const removeRule = (idx: number) => {
    setRules(rules.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-surface-200">{title}</h3>
        <span className="text-xs text-surface-500">{rules.length} rule{rules.length !== 1 ? 's' : ''}</span>
      </div>

      {/* Quick templates */}
      <div className="flex flex-wrap gap-1.5 mb-3">
        {templates.map((t) => (
          <button
            key={t.label}
            type="button"
            onClick={() => addRule(t.rule)}
            className="px-2.5 py-1 text-xs bg-surface-700 hover:bg-surface-600 text-surface-300 rounded-lg transition-colors"
          >
            + {t.label}
          </button>
        ))}
      </div>

      {rules.length === 0 ? (
        <div className="text-center py-6 border border-dashed border-surface-700 rounded-lg text-sm text-surface-500">
          No rules yet. Add from templates above or click below.
        </div>
      ) : (
        <div className="border border-surface-700 rounded-lg overflow-hidden">
          {/* Table header */}
          <div className="grid grid-cols-[60px_80px_80px_1fr_80px_32px] gap-2 px-3 py-2 bg-surface-700/50 text-xs font-medium text-surface-400">
            <span>Priority</span>
            <span>Protocol</span>
            <span>Port</span>
            <span>Remote CIDR</span>
            <span>Action</span>
            <span />
          </div>
          {/* Rules */}
          {rules.map((rule, idx) => (
            <div key={idx} className="grid grid-cols-[60px_80px_80px_1fr_80px_32px] gap-2 px-3 py-1.5 border-t border-surface-700/50 items-center">
              <input
                type="number"
                value={rule.priority}
                onChange={(e) => updateRule(idx, 'priority', parseInt(e.target.value) || 0)}
                min={1}
                max={9999}
                className="input text-xs px-1.5 py-1 font-mono"
              />
              <select
                value={rule.protocol}
                onChange={(e) => updateRule(idx, 'protocol', e.target.value)}
                className="input text-xs px-1 py-1"
              >
                <option value="tcp">TCP</option>
                <option value="udp">UDP</option>
                <option value="icmp">ICMP</option>
                <option value="all">All</option>
              </select>
              <input
                type="text"
                value={rule.port_range}
                onChange={(e) => updateRule(idx, 'port_range', e.target.value)}
                placeholder="any"
                disabled={rule.protocol === 'icmp' || rule.protocol === 'all'}
                className="input text-xs px-1.5 py-1 font-mono disabled:opacity-40"
              />
              <input
                type="text"
                value={rule.remote_address}
                onChange={(e) => updateRule(idx, 'remote_address', e.target.value)}
                placeholder="0.0.0.0/0"
                className="input text-xs px-1.5 py-1 font-mono"
              />
              <select
                value={rule.action}
                onChange={(e) => updateRule(idx, 'action', e.target.value)}
                className={`text-xs px-1 py-1 rounded-lg border font-medium ${
                  rule.action === 'allow'
                    ? 'bg-emerald-900/30 text-emerald-400 border-emerald-800/30'
                    : 'bg-red-900/30 text-red-400 border-red-800/30'
                }`}
              >
                <option value="allow">Allow</option>
                <option value="drop">Drop</option>
              </select>
              <button
                type="button"
                onClick={() => removeRule(idx)}
                className="p-1 text-surface-500 hover:text-red-400 transition-colors"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      <button
        type="button"
        onClick={() => addRule()}
        className="mt-2 flex items-center gap-1.5 text-xs text-primary-400 hover:text-primary-300 transition-colors"
      >
        <Plus className="w-3.5 h-3.5" />
        Add Rule
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3: Review
// ---------------------------------------------------------------------------

function StepReview({
  name, description, ingressRules, egressRules, onEditStep,
}: {
  name: string;
  description: string;
  ingressRules: SecurityGroupRule[];
  egressRules: SecurityGroupRule[];
  onEditStep: (step: number) => void;
}) {
  return (
    <div className="space-y-5">
      {/* Basic info */}
      <div className="border border-surface-700 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-surface-200">Basic Info</h3>
          <button
            type="button"
            onClick={() => onEditStep(0)}
            className="text-xs text-primary-400 hover:text-primary-300"
          >
            Edit
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2 text-sm">
          <span className="text-surface-400">Name</span>
          <span className="text-surface-100 font-mono">{name}</span>
          {description && (
            <>
              <span className="text-surface-400">Description</span>
              <span className="text-surface-100">{description}</span>
            </>
          )}
        </div>
      </div>

      {/* Rules summary */}
      <ReviewRulesTable
        title="Inbound Rules"
        rules={ingressRules}
        onEdit={() => onEditStep(1)}
      />
      <ReviewRulesTable
        title="Outbound Rules"
        rules={egressRules}
        onEdit={() => onEditStep(1)}
      />
    </div>
  );
}

function ReviewRulesTable({
  title, rules, onEdit,
}: {
  title: string;
  rules: SecurityGroupRule[];
  onEdit: () => void;
}) {
  return (
    <div className="border border-surface-700 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-surface-200">
          {title} <span className="text-surface-500 font-normal">({rules.length})</span>
        </h3>
        <button
          type="button"
          onClick={onEdit}
          className="text-xs text-primary-400 hover:text-primary-300"
        >
          Edit
        </button>
      </div>
      {rules.length === 0 ? (
        <p className="text-xs text-surface-500">No rules defined.</p>
      ) : (
        <div className="border border-surface-700 rounded-lg overflow-hidden">
          <div className="grid grid-cols-[50px_70px_70px_1fr_70px] gap-2 px-3 py-1.5 bg-surface-700/50 text-xs font-medium text-surface-400">
            <span>Pri</span>
            <span>Proto</span>
            <span>Port</span>
            <span>Remote</span>
            <span>Action</span>
          </div>
          {[...rules].sort((a, b) => a.priority - b.priority).map((rule, idx) => (
            <div key={idx} className="grid grid-cols-[50px_70px_70px_1fr_70px] gap-2 px-3 py-1.5 border-t border-surface-700/50 text-xs">
              <span className="text-surface-300 font-mono">{rule.priority}</span>
              <span className="text-surface-300 uppercase">{rule.protocol}</span>
              <span className="text-surface-300 font-mono">{rule.port_range || '*'}</span>
              <span className="text-surface-300 font-mono">{rule.remote_address}</span>
              <span className={rule.action === 'allow' ? 'text-emerald-400' : 'text-red-400'}>
                {rule.action}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeleteSgModal
// ---------------------------------------------------------------------------

function DeleteSgModal({
  sgName,
  onConfirm,
  onCancel,
  isDeleting,
}: {
  sgName: string;
  onConfirm: () => void;
  onCancel: () => void;
  isDeleting: boolean;
}) {
  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl p-5">
        <div className="w-12 h-12 bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <Trash2 className="w-6 h-6 text-red-400" />
        </div>
        <h2 className="text-lg font-semibold text-surface-100 text-center mb-2">Delete Security Group</h2>
        <p className="text-sm text-surface-400 text-center mb-6">
          Delete <strong>{sgName}</strong>? VMs using this security group will lose these rules.
        </p>
        <div className="flex gap-3">
          <button onClick={onCancel} className="flex-1 btn-secondary">Cancel</button>
          <button
            onClick={onConfirm}
            disabled={isDeleting}
            className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
          >
            {isDeleting ? 'Deleting...' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
  );
}
