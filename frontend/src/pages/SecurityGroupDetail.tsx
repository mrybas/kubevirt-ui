/**
 * SecurityGroup Detail Page — rule editor (ingress + egress columns)
 */

import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Shield,
  ArrowLeft,
  RefreshCw,
  Trash2,
  Plus,
  X,
  AlertTriangle,
  Save,
  ArrowDownToLine,
  ArrowUpFromLine,
} from 'lucide-react';
import clsx from 'clsx';
import { useSecurityGroup, useUpdateSecurityGroup, useDeleteSecurityGroup } from '../hooks/useSecurityGroups';
import type { SecurityGroupRule, SgProtocol, SgAction } from '../types/vpc';

const PROTOCOLS: SgProtocol[] = ['tcp', 'udp', 'icmp', 'all'];
const ACTIONS: SgAction[] = ['allow', 'drop'];

function emptyRule(): SecurityGroupRule {
  return { priority: 100, protocol: 'tcp', port_range: '', remote_address: '0.0.0.0/0', action: 'allow' };
}

function hasGatewayAllowRule(rules: SecurityGroupRule[]): boolean {
  return rules.some((r) => r.priority === 1 && r.action === 'allow');
}

export default function SecurityGroupDetail() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();

  const { data: sg, isLoading, refetch } = useSecurityGroup(name);
  const updateSg = useUpdateSecurityGroup();
  const deleteSg = useDeleteSecurityGroup();

  const [ingress, setIngress] = useState<SecurityGroupRule[]>([]);
  const [egress, setEgress] = useState<SecurityGroupRule[]>([]);
  const [dirty, setDirty] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  useEffect(() => {
    if (sg) {
      setIngress(sg.ingress_rules);
      setEgress(sg.egress_rules);
      setDirty(false);
    }
  }, [sg]);

  const handleSave = async () => {
    if (!sg) return;
    await updateSg.mutateAsync({ name: sg.name, request: { ingress_rules: ingress, egress_rules: egress } });
    setDirty(false);
  };

  const handleDelete = async () => {
    if (!sg) return;
    await deleteSg.mutateAsync(sg.name);
    navigate('/network/security-groups');
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-8 h-8 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!sg) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-surface-400">
        <Shield className="w-12 h-12 mb-4 opacity-50" />
        <p>Security group not found</p>
        <button onClick={() => navigate('/network/security-groups')} className="mt-4 btn-secondary text-sm">
          Back
        </button>
      </div>
    );
  }

  const missingIngressGateway = !hasGatewayAllowRule(ingress);
  const missingEgressGateway = !hasGatewayAllowRule(egress);

  return (
    <div className="space-y-6">
      {/* Back */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/network/security-groups')}
          className="p-1.5 text-surface-500 hover:text-surface-300 hover:bg-surface-800 rounded-lg transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <span className="text-surface-500 text-sm">Security Groups</span>
        <span className="text-surface-600">/</span>
        <span className="text-surface-200 text-sm font-mono">{sg.name}</span>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 bg-amber-600/20 rounded-xl flex items-center justify-center">
            <Shield className="w-6 h-6 text-amber-400" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-surface-100">{sg.name}</h1>
            <div className="flex items-center gap-3 mt-1 text-sm text-surface-400">
              <span>{sg.ingress_rules.length} ingress rules</span>
              <span>·</span>
              <span>{sg.egress_rules.length} egress rules</span>
              {sg.created_at && (
                <>
                  <span>·</span>
                  <span>Created {new Date(sg.created_at).toLocaleDateString()}</span>
                </>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          {dirty && (
            <button
              onClick={handleSave}
              disabled={updateSg.isPending}
              className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 text-white rounded-lg transition-colors"
            >
              <Save className="h-4 w-4" />
              {updateSg.isPending ? 'Saving...' : 'Save Changes'}
            </button>
          )}
          <button
            onClick={() => setShowDeleteModal(true)}
            className="p-2 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Rules Editor — two columns */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <RulesEditor
          direction="ingress"
          rules={ingress}
          onChange={(rules) => { setIngress(rules); setDirty(true); }}
          missingGatewayRule={missingIngressGateway}
        />
        <RulesEditor
          direction="egress"
          rules={egress}
          onChange={(rules) => { setEgress(rules); setDirty(true); }}
          missingGatewayRule={missingEgressGateway}
        />
      </div>

      {/* Unsaved changes banner */}
      {dirty && (
        <div className="fixed bottom-6 right-6 flex items-center gap-3 bg-surface-800 border border-primary-500/50 rounded-xl px-4 py-3 shadow-xl">
          <span className="text-sm text-surface-300">Unsaved changes</span>
          <button onClick={() => { setIngress(sg.ingress_rules); setEgress(sg.egress_rules); setDirty(false); }} className="btn-secondary text-xs">
            Discard
          </button>
          <button
            onClick={handleSave}
            disabled={updateSg.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-primary-600 hover:bg-primary-500 text-white rounded-lg transition-colors text-xs"
          >
            <Save className="h-3.5 w-3.5" />
            {updateSg.isPending ? 'Saving...' : 'Save'}
          </button>
        </div>
      )}

      {showDeleteModal && (
        <DeleteSgModal
          sgName={sg.name}
          onConfirm={handleDelete}
          onCancel={() => setShowDeleteModal(false)}
          isDeleting={deleteSg.isPending}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RulesEditor
// ---------------------------------------------------------------------------

function RulesEditor({
  direction,
  rules,
  onChange,
  missingGatewayRule,
}: {
  direction: 'ingress' | 'egress';
  rules: SecurityGroupRule[];
  onChange: (rules: SecurityGroupRule[]) => void;
  missingGatewayRule: boolean;
}) {
  const Icon = direction === 'ingress' ? ArrowDownToLine : ArrowUpFromLine;
  const label = direction === 'ingress' ? 'Ingress Rules' : 'Egress Rules';
  const accentColor = direction === 'ingress' ? 'text-blue-400' : 'text-purple-400';
  const bgAccent = direction === 'ingress' ? 'bg-blue-600/20' : 'bg-purple-600/20';

  const addRule = () => onChange([...rules, emptyRule()]);

  const removeRule = (i: number) => onChange(rules.filter((_, idx) => idx !== i));

  const updateRule = (i: number, field: keyof SecurityGroupRule, value: string | number) => {
    onChange(rules.map((r, idx) => idx === i ? { ...r, [field]: value } : r));
  };

  const sortedRules = [...rules].sort((a, b) => a.priority - b.priority);

  return (
    <div className="card">
      <div className="card-body space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className={clsx('w-7 h-7 rounded-lg flex items-center justify-center', bgAccent)}>
              <Icon className={clsx('w-4 h-4', accentColor)} />
            </div>
            <h3 className="font-medium text-surface-100">{label}</h3>
            <span className="text-xs text-surface-500 bg-surface-700 px-2 py-0.5 rounded-full">
              {rules.length}
            </span>
          </div>
          <button
            onClick={addRule}
            className="flex items-center gap-1 text-xs text-surface-400 hover:text-primary-400 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            Add Rule
          </button>
        </div>

        {/* Warning */}
        {missingGatewayRule && (
          <div className="flex items-center gap-2 px-3 py-2 bg-amber-900/20 border border-amber-800/30 rounded-lg text-xs text-amber-400">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            No priority-1 allow rule. Default-deny may block gateway traffic.
          </div>
        )}

        {/* Column headers */}
        {rules.length > 0 && (
          <div className="grid grid-cols-[48px_64px_80px_1fr_72px_24px] gap-2 px-1 text-xs text-surface-500 font-medium uppercase tracking-wider">
            <span>Prio</span>
            <span>Proto</span>
            <span>Port</span>
            <span>Address</span>
            <span>Action</span>
            <span />
          </div>
        )}

        {/* Rules */}
        <div className="space-y-2">
          {sortedRules.length === 0 ? (
            <div className="text-center py-6 text-surface-500 text-sm">
              No rules — traffic will be handled by default policy
            </div>
          ) : (
            sortedRules.map((rule, displayIdx) => {
              // Find original index for mutation
              const origIdx = rules.indexOf(rule);
              return (
                <RuleRow
                  key={displayIdx}
                  rule={rule}
                  onChange={(field, value) => updateRule(origIdx, field, value)}
                  onRemove={() => removeRule(origIdx)}
                />
              );
            })
          )}
        </div>

        {rules.length > 0 && (
          <button
            onClick={addRule}
            className="flex items-center gap-1.5 text-sm text-surface-500 hover:text-primary-400 transition-colors w-full justify-center py-2 border border-dashed border-surface-700 hover:border-primary-600 rounded-lg"
          >
            <Plus className="w-4 h-4" />
            Add Rule
          </button>
        )}
      </div>
    </div>
  );
}

function RuleRow({
  rule,
  onChange,
  onRemove,
}: {
  rule: SecurityGroupRule;
  onChange: (field: keyof SecurityGroupRule, value: string | number) => void;
  onRemove: () => void;
}) {
  const actionColor = rule.action === 'allow'
    ? 'bg-emerald-900/30 text-emerald-400 border-emerald-800/30'
    : 'bg-red-900/30 text-red-400 border-red-800/30';

  return (
    <div className="grid grid-cols-[48px_64px_80px_1fr_72px_24px] gap-2 items-center">
      {/* Priority */}
      <input
        type="number"
        value={rule.priority}
        onChange={(e) => onChange('priority', parseInt(e.target.value) || 0)}
        min={1}
        max={9999}
        className="input text-xs px-2 py-1.5 text-center font-mono"
      />
      {/* Protocol */}
      <select
        value={rule.protocol}
        onChange={(e) => onChange('protocol', e.target.value)}
        className="input text-xs px-1.5 py-1.5"
      >
        {PROTOCOLS.map((p) => (
          <option key={p} value={p}>{p}</option>
        ))}
      </select>
      {/* Port */}
      <input
        type="text"
        value={rule.port_range}
        onChange={(e) => onChange('port_range', e.target.value)}
        placeholder="any"
        className="input text-xs px-2 py-1.5 font-mono"
        disabled={rule.protocol === 'icmp' || rule.protocol === 'all'}
      />
      {/* Address */}
      <input
        type="text"
        value={rule.remote_address}
        onChange={(e) => onChange('remote_address', e.target.value)}
        placeholder="0.0.0.0/0"
        className="input text-xs px-2 py-1.5 font-mono"
      />
      {/* Action */}
      <select
        value={rule.action}
        onChange={(e) => onChange('action', e.target.value)}
        className={clsx('text-xs px-1.5 py-1.5 rounded-lg border font-medium', actionColor)}
      >
        {ACTIONS.map((a) => (
          <option key={a} value={a}>{a}</option>
        ))}
      </select>
      {/* Remove */}
      <button
        onClick={onRemove}
        className="p-0.5 text-surface-500 hover:text-red-400 transition-colors"
      >
        <X className="w-4 h-4" />
      </button>
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
