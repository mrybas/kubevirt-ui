/**
 * SubnetAclEditor — embedded ACL rule editor for NetworkDetail.
 * Follows the SecurityGroupDetail rule editor pattern.
 */

import { useState, useEffect } from 'react';
import { Plus, Trash2, RefreshCw, Save, AlertTriangle, Shield } from 'lucide-react';
import clsx from 'clsx';
import {
  useSubnetAcls,
  useAclPresets,
  useReplaceSubnetAcls,
} from '../../hooks/useSubnetAcls';
import type { SubnetAcl } from '../../types/subnet_acl';
import { Modal } from '../common/Modal';

// ---------------------------------------------------------------------------
// Add ACL Rule Modal
// ---------------------------------------------------------------------------

interface AddAclRuleModalProps {
  onClose: () => void;
  onAdd: (rule: SubnetAcl) => void;
}

function buildMatchFromParts(parts: {
  srcCidr: string;
  dstCidr: string;
  protocol: string;
  port: string;
  direction: string;
}): string {
  const clauses: string[] = [];

  if (parts.direction === 'from-lport') {
    if (parts.srcCidr) clauses.push(`ip4.src == ${parts.srcCidr}`);
    if (parts.dstCidr) clauses.push(`ip4.dst == ${parts.dstCidr}`);
  } else {
    if (parts.dstCidr) clauses.push(`ip4.dst == ${parts.dstCidr}`);
    if (parts.srcCidr) clauses.push(`ip4.src == ${parts.srcCidr}`);
  }

  if (parts.protocol === 'tcp') {
    if (parts.port) clauses.push(`tcp.dst == ${parts.port}`);
    else clauses.push('ip4 && tcp');
  } else if (parts.protocol === 'udp') {
    if (parts.port) clauses.push(`udp.dst == ${parts.port}`);
    else clauses.push('ip4 && udp');
  } else if (parts.protocol === 'icmp') {
    clauses.push('icmp4');
  }

  return clauses.length > 0 ? clauses.join(' && ') : 'ip';
}

function AddAclRuleModal({ onClose, onAdd }: AddAclRuleModalProps) {
  const [direction, setDirection] = useState<'from-lport' | 'to-lport'>('from-lport');
  const [action, setAction] = useState<SubnetAcl['action']>('allow');
  const [priority, setPriority] = useState(2000);
  const [useRaw, setUseRaw] = useState(false);
  const [rawMatch, setRawMatch] = useState('ip');
  const [srcCidr, setSrcCidr] = useState('');
  const [dstCidr, setDstCidr] = useState('');
  const [protocol, setProtocol] = useState('any');
  const [port, setPort] = useState('');

  const generatedMatch = buildMatchFromParts({ srcCidr, dstCidr, protocol, port, direction });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onAdd({
      direction,
      action,
      priority,
      match: useRaw ? rawMatch : generatedMatch,
    });
    onClose();
  };

  return (
    <Modal isOpen onClose={onClose} title="Add ACL Rule" size="lg">
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Direction + Action */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-surface-300 mb-1.5">Direction</label>
            <div className="flex gap-2">
              {(['from-lport', 'to-lport'] as const).map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => setDirection(d)}
                  className={clsx(
                    'flex-1 py-2 text-xs rounded-lg border font-medium transition-colors',
                    direction === d
                      ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                      : 'border-surface-700 bg-surface-900 text-surface-400 hover:border-surface-600',
                  )}
                >
                  {d === 'from-lport' ? 'Egress (from pod)' : 'Ingress (to pod)'}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm text-surface-300 mb-1.5">Action</label>
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as SubnetAcl['action'])}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
            >
              <option value="allow">Allow</option>
              <option value="allow-related">Allow-Related</option>
              <option value="drop">Drop</option>
              <option value="reject">Reject</option>
            </select>
          </div>
        </div>

        {/* Priority */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            Priority{' '}
            <span className="text-surface-500 text-xs">(0–32767, higher = evaluated first)</span>
          </label>
          <input
            type="number"
            value={priority}
            onChange={(e) => setPriority(parseInt(e.target.value) || 0)}
            min={0}
            max={32767}
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 font-mono text-sm"
          />
        </div>

        {/* Match toggle */}
        <div className="flex gap-2 p-0.5 bg-surface-900 rounded-lg border border-surface-700 w-fit">
          <button
            type="button"
            onClick={() => setUseRaw(false)}
            className={clsx(
              'px-3 py-1.5 text-xs rounded-md transition-colors',
              !useRaw ? 'bg-surface-700 text-surface-100' : 'text-surface-500 hover:text-surface-300',
            )}
          >
            Builder
          </button>
          <button
            type="button"
            onClick={() => { setUseRaw(true); setRawMatch(generatedMatch); }}
            className={clsx(
              'px-3 py-1.5 text-xs rounded-md transition-colors',
              useRaw ? 'bg-surface-700 text-surface-100' : 'text-surface-500 hover:text-surface-300',
            )}
          >
            Raw Match
          </button>
        </div>

        {useRaw ? (
          <div>
            <label className="block text-sm text-surface-300 mb-1">Match Expression</label>
            <input
              type="text"
              value={rawMatch}
              onChange={(e) => setRawMatch(e.target.value)}
              placeholder="ip4.src == 10.0.0.0/8 && tcp.dst == 80"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-surface-400 mb-1">Source CIDR (optional)</label>
                <input
                  type="text"
                  value={srcCidr}
                  onChange={(e) => setSrcCidr(e.target.value)}
                  placeholder="10.0.0.0/8"
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-surface-400 mb-1">Destination CIDR (optional)</label>
                <input
                  type="text"
                  value={dstCidr}
                  onChange={(e) => setDstCidr(e.target.value)}
                  placeholder="192.168.0.0/16"
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-surface-400 mb-1">Protocol</label>
                <select
                  value={protocol}
                  onChange={(e) => setProtocol(e.target.value)}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                >
                  <option value="any">Any</option>
                  <option value="tcp">TCP</option>
                  <option value="udp">UDP</option>
                  <option value="icmp">ICMP</option>
                </select>
              </div>
              <div>
                <label className="block text-xs text-surface-400 mb-1">Port (optional)</label>
                <input
                  type="text"
                  value={port}
                  onChange={(e) => setPort(e.target.value)}
                  placeholder="80"
                  disabled={protocol === 'any' || protocol === 'icmp'}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm disabled:opacity-40"
                />
              </div>
            </div>

            {/* Preview */}
            <div className="p-2 bg-surface-900/80 rounded border border-surface-700">
              <p className="text-xs text-surface-500 mb-1">Generated match:</p>
              <code className="text-xs text-primary-400 font-mono">{generatedMatch}</code>
            </div>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button type="submit" className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 text-white rounded-lg text-sm font-medium transition-colors">
            <Plus className="w-4 h-4" />
            Add Rule
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// SubnetAclEditor (main export)
// ---------------------------------------------------------------------------

interface SubnetAclEditorProps {
  subnetName: string;
}

const ACTION_COLORS: Record<string, string> = {
  allow: 'bg-emerald-500/10 text-emerald-400',
  'allow-related': 'bg-emerald-500/10 text-emerald-300',
  drop: 'bg-red-500/10 text-red-400',
  reject: 'bg-orange-500/10 text-orange-400',
};

export function SubnetAclEditor({ subnetName }: SubnetAclEditorProps) {
  const { data, isLoading, refetch } = useSubnetAcls(subnetName);
  const { data: presets } = useAclPresets(subnetName);
  const replaceAcls = useReplaceSubnetAcls(subnetName);

  // Local editable copy
  const [localAcls, setLocalAcls] = useState<SubnetAcl[]>([]);
  const [isDirty, setIsDirty] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);

  useEffect(() => {
    if (data?.acls) {
      setLocalAcls(data.acls);
      setIsDirty(false);
    }
  }, [data]);

  const handleAddRule = (rule: SubnetAcl) => {
    setLocalAcls((prev) => [...prev, rule]);
    setIsDirty(true);
  };

  const handleDeleteLocal = (idx: number) => {
    setLocalAcls((prev) => prev.filter((_, i) => i !== idx));
    setIsDirty(true);
  };

  const handleSave = async () => {
    await replaceAcls.mutateAsync({ acls: localAcls });
    setIsDirty(false);
  };

  const handleApplyPreset = (preset: { acls: SubnetAcl[] }) => {
    setLocalAcls((prev) => {
      const existing = new Set(prev.map((a) => `${a.direction}|${a.match}|${a.action}`));
      const newRules = preset.acls.filter(
        (a) => !existing.has(`${a.direction}|${a.match}|${a.action}`),
      );
      return [...prev, ...newRules];
    });
    setIsDirty(true);
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-32">
        <RefreshCw className="h-6 w-6 animate-spin text-surface-500" />
      </div>
    );
  }

  const sorted = [...localAcls].sort((a, b) => b.priority - a.priority);

  return (
    <div className="space-y-4 p-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-surface-200 flex items-center gap-2">
            <Shield className="w-4 h-4 text-primary-400" />
            ACL Rules
            <span className="text-xs font-normal text-surface-500">
              ({localAcls.length} rule{localAcls.length !== 1 ? 's' : ''})
            </span>
          </h3>
          <p className="text-xs text-surface-500 mt-0.5">
            OVN subnet-level ACLs — evaluated in priority order (highest first)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => refetch()} className="btn-secondary p-2" title="Reload from cluster">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={() => setShowAddModal(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-surface-700 hover:bg-surface-600 text-surface-200 rounded-lg transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            Add Rule
          </button>
          {isDirty && (
            <button
              onClick={handleSave}
              disabled={replaceAcls.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-500 disabled:opacity-50 text-white rounded-lg transition-colors"
            >
              <Save className="w-3.5 h-3.5" />
              {replaceAcls.isPending ? 'Saving...' : 'Save Changes'}
            </button>
          )}
        </div>
      </div>

      {/* Preset buttons */}
      {presets && presets.length > 0 && (
        <div>
          <p className="text-xs text-surface-500 mb-2">Quick presets:</p>
          <div className="flex flex-wrap gap-2">
            {presets.map((preset) => (
              <button
                key={preset.name}
                onClick={() => handleApplyPreset(preset)}
                title={preset.description}
                className="px-2.5 py-1 text-xs bg-surface-800 hover:bg-surface-700 border border-surface-700 hover:border-surface-600 text-surface-300 rounded-lg transition-colors"
              >
                + {preset.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {replaceAcls.isError && (
        <div className="flex items-start gap-2 p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
          <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
          <p className="text-sm text-red-400">
            {(replaceAcls.error as Error)?.message || 'Failed to save ACL rules'}
          </p>
        </div>
      )}

      {/* ACL Table */}
      {sorted.length === 0 ? (
        <div className="text-center py-10 border border-dashed border-surface-700 rounded-lg">
          <Shield className="w-8 h-8 text-surface-600 mx-auto mb-2" />
          <p className="text-sm text-surface-500">No ACL rules defined</p>
          <p className="text-xs text-surface-600 mt-1">All traffic is permitted by default</p>
        </div>
      ) : (
        <div className="border border-surface-700 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800/80 text-xs text-surface-400">
                <th className="text-right px-4 py-2.5 font-medium w-20">Priority</th>
                <th className="text-left px-4 py-2.5 font-medium w-32">Direction</th>
                <th className="text-left px-4 py-2.5 font-medium">Match</th>
                <th className="text-left px-4 py-2.5 font-medium w-28">Action</th>
                <th className="px-4 py-2.5 w-10" />
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-700/50">
              {sorted.map((acl, idx) => {
                const originalIdx = localAcls.indexOf(acl);
                return (
                  <tr key={idx} className="bg-surface-800/30 hover:bg-surface-800/60 transition-colors">
                    <td className="px-4 py-2.5 text-right font-mono text-surface-400 text-xs">
                      {acl.priority}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="text-xs text-surface-400">
                        {acl.direction === 'from-lport' ? '↑ Egress' : '↓ Ingress'}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-surface-300 max-w-xs truncate">
                      {acl.match}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={clsx(
                          'px-2 py-0.5 rounded text-xs font-medium',
                          ACTION_COLORS[acl.action] ?? 'bg-surface-700 text-surface-300',
                        )}
                      >
                        {acl.action}
                      </span>
                    </td>
                    <td className="px-4 py-2.5">
                      <button
                        onClick={() => handleDeleteLocal(originalIdx)}
                        className="p-1 text-surface-600 hover:text-red-400 rounded transition-colors"
                        title="Remove rule"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {isDirty && (
        <p className="text-xs text-amber-400 flex items-center gap-1">
          <AlertTriangle className="w-3.5 h-3.5" />
          Unsaved changes — click "Save Changes" to apply to cluster
        </p>
      )}

      {showAddModal && (
        <AddAclRuleModal onClose={() => setShowAddModal(false)} onAdd={handleAddRule} />
      )}
    </div>
  );
}
