/**
 * OVN Gateways Page
 *
 * List, create, delete OVN NAT gateways; manage DNAT rules and FIPs.
 * OVN-native NAT using OvnEip, OvnSnatRule, OvnDnatRule, OvnFip CRDs.
 */

import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Plus,
  RefreshCw,
  Trash2,
  Globe,
  CheckCircle,
  Clock,
  Eye,
  ArrowRight,
  AlertTriangle,
  Network,
} from 'lucide-react';
import clsx from 'clsx';
import {
  useOvnGateways,
  useCreateOvnGateway,
  useDeleteOvnGateway,
  useCreateDnatRule,
  useDeleteDnatRule,
  useCreateFip,
  useDeleteFip,
} from '../hooks/useOvnGateways';
import { useVpcs } from '../hooks/useVpcs';
import type {
  OvnGateway,
  CreateOvnGatewayRequest,
  CreateOvnDnatRuleRequest,
  CreateOvnFipRequest,
} from '../types/ovn_gateway';
import { Modal } from '@/components/common/Modal';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';
import { listSubnets } from '../api/network';

// ---------------------------------------------------------------------------
// Tooltip helper
// ---------------------------------------------------------------------------

function Tooltip({ text, children }: { text: string; children: React.ReactNode }) {
  return (
    <span className="group relative inline-flex items-center">
      {children}
      <span className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block z-50 w-64 rounded-lg bg-surface-800 border border-surface-600 px-3 py-2 text-xs text-surface-300 shadow-xl">
        {text}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------

function StatusBadge({ ready }: { ready: boolean }) {
  return ready ? (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-emerald-400 bg-emerald-500/10">
      <CheckCircle className="h-3.5 w-3.5" />
      Ready
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-amber-400 bg-amber-500/10">
      <Clock className="h-3.5 w-3.5" />
      Not Ready
    </span>
  );
}

// ---------------------------------------------------------------------------
// Create OVN Gateway Modal
// ---------------------------------------------------------------------------

const DEFAULT_CREATE_FORM: CreateOvnGatewayRequest = {
  name: '',
  vpc_name: '',
  subnet_name: '',
  external_subnet: '',
  auto_snat: true,
};

function CreateOvnGatewayModal({
  externalSubnets,
  onClose,
}: {
  externalSubnets: { name: string; cidr: string }[];
  onClose: () => void;
}) {
  const [form, setForm] = useState<CreateOvnGatewayRequest>(DEFAULT_CREATE_FORM);
  const { data: vpcsData } = useVpcs();
  const createGateway = useCreateOvnGateway();

  const selectedVpc = vpcsData?.items.find((v) => v.name === form.vpc_name);

  const handleVpcChange = (name: string) => {
    setForm((prev) => ({ ...prev, vpc_name: name, subnet_name: '' }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await createGateway.mutateAsync(form);
    onClose();
  };

  const set = <K extends keyof CreateOvnGatewayRequest>(
    field: K,
    value: CreateOvnGatewayRequest[K],
  ) => setForm((prev) => ({ ...prev, [field]: value }));

  const isValid = form.name.length > 0 && form.vpc_name.length > 0 && form.subnet_name.length > 0;

  return (
    <Modal isOpen onClose={onClose} title="Create OVN Gateway" size="lg">
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Name */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => set('name', e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
            placeholder="my-ovn-gateway"
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            required
          />
        </div>

        {/* VPC */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">VPC</label>
          <select
            value={form.vpc_name}
            onChange={(e) => handleVpcChange(e.target.value)}
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
            required
          >
            <option value="">Select VPC...</option>
            {(vpcsData?.items ?? []).map((v) => (
              <option key={v.name} value={v.name}>
                {v.name}
              </option>
            ))}
          </select>
        </div>

        {/* VPC Subnet */}
        {selectedVpc && (
          <div>
            <label className="block text-sm text-surface-300 mb-1">VPC Subnet (to SNAT)</label>
            <select
              value={form.subnet_name}
              onChange={(e) => set('subnet_name', e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
              required
            >
              <option value="">Select subnet...</option>
              {selectedVpc.subnets.map((s: { name: string; cidr_block: string }) => (
                <option key={s.name} value={s.name}>
                  {s.name} ({s.cidr_block})
                </option>
              ))}
            </select>
          </div>
        )}

        {/* External Subnet */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            External Subnet{' '}
            <Tooltip text="Infrastructure VLAN subnet for the EIP. Leave empty to auto-detect.">
              <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
            </Tooltip>
          </label>
          {externalSubnets.length > 0 ? (
            <select
              value={form.external_subnet ?? ''}
              onChange={(e) => set('external_subnet', e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
            >
              <option value="">Auto-detect...</option>
              {externalSubnets.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name} ({s.cidr})
                </option>
              ))}
            </select>
          ) : (
            <input
              type="text"
              value={form.external_subnet ?? ''}
              onChange={(e) => set('external_subnet', e.target.value)}
              placeholder="Leave empty to auto-detect"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            />
          )}
        </div>

        {/* Auto SNAT toggle */}
        <div className="flex items-center justify-between p-3 bg-surface-900 rounded-lg border border-surface-700">
          <div>
            <p className="text-sm font-medium text-surface-200">Auto SNAT</p>
            <p className="text-xs text-surface-500 mt-0.5">
              Automatically create SNAT rule for the VPC subnet
            </p>
          </div>
          <button
            type="button"
            onClick={() => set('auto_snat', !form.auto_snat)}
            className={clsx(
              'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
              form.auto_snat ? 'bg-primary-500' : 'bg-surface-600',
            )}
          >
            <span
              className={clsx(
                'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
                form.auto_snat ? 'translate-x-6' : 'translate-x-1',
              )}
            />
          </button>
        </div>

        {/* Info */}
        <div className="flex gap-3 p-3 bg-blue-900/10 border border-blue-800/30 rounded-lg">
          <Network className="w-4 h-4 text-blue-400 shrink-0 mt-0.5" />
          <p className="text-xs text-blue-300/80">
            OVN gateway uses native OVN NAT — no extra pods needed. EIP is allocated from the
            external subnet. Requires <code className="font-mono">ENABLE_NAT_GW=true</code> in
            kube-ovn and nodes labeled{' '}
            <code className="font-mono">ovn.kubernetes.io/external-gw=true</code>.
          </p>
        </div>

        {createGateway.isError && (
          <div className="flex items-start gap-2 p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
            <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
            <p className="text-sm text-red-400">
              {(createGateway.error as Error)?.message || 'Failed to create OVN gateway'}
            </p>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || createGateway.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
          >
            {createGateway.isPending ? 'Creating...' : 'Create Gateway'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Add DNAT Rule Modal
// ---------------------------------------------------------------------------

function AddDnatRuleModal({
  gateway,
  onClose,
}: {
  gateway: OvnGateway;
  onClose: () => void;
}) {
  const createDnat = useCreateDnatRule(gateway.name);
  const eipName = gateway.eip?.name ?? '';

  const [form, setForm] = useState<CreateOvnDnatRuleRequest>({
    ovn_eip: eipName,
    ip_name: '',
    protocol: 'tcp',
    internal_port: '',
    external_port: '',
  });

  const set = <K extends keyof CreateOvnDnatRuleRequest>(
    field: K,
    value: CreateOvnDnatRuleRequest[K],
  ) => setForm((prev) => ({ ...prev, [field]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await createDnat.mutateAsync(form);
    onClose();
  };

  const isValid =
    form.ovn_eip.length > 0 &&
    form.ip_name.length > 0 &&
    form.internal_port.length > 0 &&
    form.external_port.length > 0;

  return (
    <Modal isOpen onClose={onClose} title="Add DNAT Rule">
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* EIP */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            OVN EIP{' '}
            <Tooltip text="OvnEip resource name to use for external IP. Usually eip-<gateway-name>.">
              <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
            </Tooltip>
          </label>
          <input
            type="text"
            value={form.ovn_eip}
            onChange={(e) => set('ovn_eip', e.target.value)}
            placeholder={`eip-${gateway.name}`}
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            required
          />
        </div>

        {/* Internal endpoint */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            IP Name{' '}
            <Tooltip text="OVN IP resource name of the internal endpoint (e.g. pod/VM IP name in kube-ovn).">
              <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
            </Tooltip>
          </label>
          <input
            type="text"
            value={form.ip_name}
            onChange={(e) => set('ip_name', e.target.value)}
            placeholder="my-vm.default"
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            required
          />
        </div>

        {/* Protocol */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">Protocol</label>
          <div className="flex gap-2">
            {(['tcp', 'udp'] as const).map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => set('protocol', p)}
                className={clsx(
                  'px-4 py-2 rounded-lg border text-sm font-medium transition-colors uppercase',
                  form.protocol === p
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 bg-surface-900 text-surface-300 hover:border-surface-600',
                )}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        {/* Ports */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-surface-300 mb-1">External Port</label>
            <input
              type="text"
              value={form.external_port}
              onChange={(e) => set('external_port', e.target.value)}
              placeholder="8080"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>
          <div>
            <label className="block text-sm text-surface-300 mb-1">Internal Port</label>
            <input
              type="text"
              value={form.internal_port}
              onChange={(e) => set('internal_port', e.target.value)}
              placeholder="80"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>
        </div>

        {createDnat.isError && (
          <p className="text-sm text-red-400">
            {(createDnat.error as Error)?.message || 'Failed to create DNAT rule'}
          </p>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || createDnat.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
          >
            {createDnat.isPending ? 'Creating...' : 'Add Rule'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Add FIP Modal
// ---------------------------------------------------------------------------

function AddFipModal({
  gateway,
  onClose,
}: {
  gateway: OvnGateway;
  onClose: () => void;
}) {
  const createFipMutation = useCreateFip(gateway.name);
  const eipName = gateway.eip?.name ?? '';

  const [form, setForm] = useState<CreateOvnFipRequest>({
    ovn_eip: '',
    ip_name: '',
  });

  const set = <K extends keyof CreateOvnFipRequest>(
    field: K,
    value: CreateOvnFipRequest[K],
  ) => setForm((prev) => ({ ...prev, [field]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await createFipMutation.mutateAsync(form);
    onClose();
  };

  const isValid = form.ovn_eip.length > 0 && form.ip_name.length > 0;

  return (
    <Modal isOpen onClose={onClose} title="Add Floating IP">
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* EIP */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            OVN EIP{' '}
            <Tooltip text="Dedicated OvnEip for 1:1 NAT. Must be a different EIP than the SNAT one.">
              <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
            </Tooltip>
          </label>
          <input
            type="text"
            value={form.ovn_eip}
            onChange={(e) => set('ovn_eip', e.target.value)}
            placeholder={`eip-${gateway.name}-fip`}
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            required
          />
          {eipName && (
            <p className="text-xs text-surface-500 mt-1">
              Gateway SNAT EIP: <code className="font-mono">{eipName}</code>
            </p>
          )}
        </div>

        {/* IP Name */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            IP Name{' '}
            <Tooltip text="OVN IP resource name of the internal endpoint to map 1:1.">
              <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
            </Tooltip>
          </label>
          <input
            type="text"
            value={form.ip_name}
            onChange={(e) => set('ip_name', e.target.value)}
            placeholder="my-vm.default"
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            required
          />
        </div>

        {createFipMutation.isError && (
          <p className="text-sm text-red-400">
            {(createFipMutation.error as Error)?.message || 'Failed to create FIP'}
          </p>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || createFipMutation.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
          >
            {createFipMutation.isPending ? 'Creating...' : 'Add FIP'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Gateway Detail Modal
// ---------------------------------------------------------------------------

function GatewayDetailModal({
  gateway,
  onClose,
}: {
  gateway: OvnGateway;
  onClose: () => void;
}) {
  const [showAddDnat, setShowAddDnat] = useState(false);
  const [showAddFip, setShowAddFip] = useState(false);
  const deleteDnat = useDeleteDnatRule(gateway.name);
  const deleteFipMutation = useDeleteFip(gateway.name);

  return (
    <>
      <Modal isOpen onClose={onClose} title={`OVN Gateway: ${gateway.name}`} size="lg">
        <div className="space-y-5">
          {/* Summary */}
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-surface-400">VPC</span>
              <p className="font-mono text-surface-200">{gateway.vpc_name || '-'}</p>
            </div>
            <div>
              <span className="text-surface-400">VPC Subnet</span>
              <p className="font-mono text-surface-200">{gateway.subnet_name || '-'}</p>
            </div>
            <div>
              <span className="text-surface-400">External Subnet</span>
              <p className="font-mono text-surface-200">{gateway.external_subnet || '-'}</p>
            </div>
            <div>
              <span className="text-surface-400">EIP Address</span>
              <p className="font-mono text-primary-400">{gateway.eip?.v4ip || '-'}</p>
            </div>
            <div>
              <span className="text-surface-400">LSP Patched</span>
              <p className="text-surface-200">{gateway.lsp_patched ? 'Yes' : 'No'}</p>
            </div>
            <div>
              <span className="text-surface-400">Status</span>
              <div className="mt-0.5">
                <StatusBadge ready={gateway.ready} />
              </div>
            </div>
          </div>

          {/* SNAT Rules */}
          {gateway.snat_rules.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">
                SNAT Rules
              </h4>
              <div className="border border-surface-700 rounded-xl overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-800/80 text-xs text-surface-400">
                      <th className="text-left px-4 py-2 font-medium">Name</th>
                      <th className="text-left px-4 py-2 font-medium">EIP</th>
                      <th className="text-left px-4 py-2 font-medium">Subnet / CIDR</th>
                      <th className="text-left px-4 py-2 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-surface-700">
                    {gateway.snat_rules.map((rule) => (
                      <tr key={rule.name} className="bg-surface-800/50 hover:bg-surface-800 transition-colors">
                        <td className="px-4 py-2 font-mono text-surface-200">{rule.name}</td>
                        <td className="px-4 py-2 font-mono text-primary-400">{rule.v4ip || rule.ovn_eip}</td>
                        <td className="px-4 py-2 font-mono text-surface-300">
                          {rule.vpc_subnet || rule.internal_cidr || '-'}
                        </td>
                        <td className="px-4 py-2">
                          <StatusBadge ready={rule.ready} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* DNAT Rules */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider">
                DNAT Rules (Port Forwarding)
              </h4>
              <button
                onClick={() => setShowAddDnat(true)}
                className="text-xs text-primary-400 hover:text-primary-300 flex items-center gap-1"
              >
                <Plus className="h-3.5 w-3.5" />
                Add Rule
              </button>
            </div>
            {gateway.dnat_rules.length === 0 ? (
              <p className="text-sm text-surface-500 italic">No DNAT rules</p>
            ) : (
              <div className="border border-surface-700 rounded-xl overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-800/80 text-xs text-surface-400">
                      <th className="text-left px-4 py-2 font-medium">External</th>
                      <th className="text-left px-4 py-2 font-medium">Arrow</th>
                      <th className="text-left px-4 py-2 font-medium">Internal</th>
                      <th className="text-left px-4 py-2 font-medium">Protocol</th>
                      <th className="text-left px-4 py-2 font-medium">Status</th>
                      <th className="px-4 py-2" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-surface-700">
                    {gateway.dnat_rules.map((rule) => (
                      <tr key={rule.name} className="bg-surface-800/50 hover:bg-surface-800 transition-colors">
                        <td className="px-4 py-2 font-mono text-primary-400">
                          {rule.v4ip}:{rule.external_port}
                        </td>
                        <td className="px-4 py-2 text-surface-500">
                          <ArrowRight className="w-3.5 h-3.5" />
                        </td>
                        <td className="px-4 py-2 font-mono text-surface-300">
                          {rule.ip_name}:{rule.internal_port}
                        </td>
                        <td className="px-4 py-2 text-surface-400 uppercase text-xs">{rule.protocol}</td>
                        <td className="px-4 py-2">
                          <StatusBadge ready={rule.ready} />
                        </td>
                        <td className="px-4 py-2 text-right">
                          <button
                            onClick={() => deleteDnat.mutate(rule.name)}
                            disabled={deleteDnat.isPending}
                            className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                            title="Delete rule"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Floating IPs */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider">
                Floating IPs (1:1 NAT)
              </h4>
              <button
                onClick={() => setShowAddFip(true)}
                className="text-xs text-primary-400 hover:text-primary-300 flex items-center gap-1"
              >
                <Plus className="h-3.5 w-3.5" />
                Add FIP
              </button>
            </div>
            {gateway.fips.length === 0 ? (
              <p className="text-sm text-surface-500 italic">No floating IPs</p>
            ) : (
              <div className="border border-surface-700 rounded-xl overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-800/80 text-xs text-surface-400">
                      <th className="text-left px-4 py-2 font-medium">Name</th>
                      <th className="text-left px-4 py-2 font-medium">EIP</th>
                      <th className="text-left px-4 py-2 font-medium">Internal IP Name</th>
                      <th className="text-left px-4 py-2 font-medium">Status</th>
                      <th className="px-4 py-2" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-surface-700">
                    {gateway.fips.map((fip) => (
                      <tr key={fip.name} className="bg-surface-800/50 hover:bg-surface-800 transition-colors">
                        <td className="px-4 py-2 font-mono text-surface-200">{fip.name}</td>
                        <td className="px-4 py-2 font-mono text-primary-400">{fip.v4ip || fip.ovn_eip}</td>
                        <td className="px-4 py-2 font-mono text-surface-300">{fip.ip_name}</td>
                        <td className="px-4 py-2">
                          <StatusBadge ready={fip.ready} />
                        </td>
                        <td className="px-4 py-2 text-right">
                          <button
                            onClick={() => deleteFipMutation.mutate(fip.name)}
                            disabled={deleteFipMutation.isPending}
                            className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                            title="Delete FIP"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </Modal>

      {showAddDnat && (
        <AddDnatRuleModal gateway={gateway} onClose={() => setShowAddDnat(false)} />
      )}
      {showAddFip && (
        <AddFipModal gateway={gateway} onClose={() => setShowAddFip(false)} />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function OvnGateways() {
  const [showCreate, setShowCreate] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [detailGateway, setDetailGateway] = useState<OvnGateway | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const { data, isLoading, refetch } = useOvnGateways();
  const deleteGateway = useDeleteOvnGateway();

  // VLAN-backed subnets for external subnet dropdown
  const { data: allSubnets } = useQuery({ queryKey: ['subnets'], queryFn: listSubnets });
  const externalSubnets = useMemo(
    () =>
      (allSubnets ?? [])
        .filter((s) => s.vlan)
        .map((s) => ({ name: s.name, cidr: s.cidr_block })),
    [allSubnets],
  );

  const gateways = data?.items ?? [];
  const filtered = searchQuery
    ? gateways.filter((gw) => gw.name.toLowerCase().includes(searchQuery.toLowerCase()))
    : gateways;

  const handleDelete = async (name: string) => {
    await deleteGateway.mutateAsync(name);
    setDeleteConfirm(null);
  };

  const columns: Column<OvnGateway>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (gw) => (
        <span className="font-medium font-mono text-surface-100">{gw.name}</span>
      ),
    },
    {
      key: 'vpc_name',
      header: 'VPC',
      hideOnMobile: true,
      accessor: (gw) => (
        <span className="font-mono text-surface-300">{gw.vpc_name || '-'}</span>
      ),
    },
    {
      key: 'eip',
      header: 'EIP Address',
      hideOnMobile: true,
      accessor: (gw) =>
        gw.eip?.v4ip ? (
          <span className="font-mono text-primary-400">{gw.eip.v4ip}</span>
        ) : (
          <span className="text-surface-500">-</span>
        ),
    },
    {
      key: 'external_subnet',
      header: 'External Subnet',
      hideOnMobile: true,
      accessor: (gw) => (
        <span className="font-mono text-surface-300">{gw.external_subnet || '-'}</span>
      ),
    },
    {
      key: 'snat_rules',
      header: 'SNAT',
      hideOnMobile: true,
      accessor: (gw) => <span>{gw.snat_rules.length}</span>,
    },
    {
      key: 'dnat_rules',
      header: 'DNAT Rules',
      hideOnMobile: true,
      accessor: (gw) => <span>{gw.dnat_rules.length}</span>,
    },
    {
      key: 'fips',
      header: 'FIPs',
      hideOnMobile: true,
      accessor: (gw) => <span>{gw.fips.length}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (gw) => <StatusBadge ready={gw.ready} />,
    },
  ];

  const getActions = (gw: OvnGateway): MenuItem[] => [
    {
      label: 'View Details',
      icon: <Eye className="h-4 w-4" />,
      onClick: () => setDetailGateway(gw),
    },
    {
      label: 'Delete',
      icon: <Trash2 className="h-4 w-4" />,
      onClick: () => setDeleteConfirm(gw.name),
      variant: 'danger',
    },
  ];

  return (
    <div className="space-y-6">
      <ActionBar
        title="OVN Gateways"
        subtitle="OVN-native NAT gateways — no extra pods, NAT handled by OVN logical router"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        <button onClick={() => setShowCreate(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create OVN Gateway
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filtered}
        loading={isLoading}
        keyExtractor={(gw) => gw.name}
        actions={getActions}
        onRowClick={(gw) => setDetailGateway(gw)}
        searchable
        searchPlaceholder="Search OVN gateways..."
        onSearch={setSearchQuery}
        expandable={(gw) => (
          <div className="px-4 py-3 bg-surface-900/50">
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="text-xs text-surface-400">VPC</span>
                <p className="font-mono text-surface-200">{gw.vpc_name || '-'}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">VPC Subnet</span>
                <p className="font-mono text-surface-200">{gw.subnet_name || '-'}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">External Subnet</span>
                <p className="font-mono text-surface-200">{gw.external_subnet || '-'}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">EIP</span>
                <p className="font-mono text-primary-400">{gw.eip?.v4ip || '-'}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">LSP Patched</span>
                <p className="text-surface-200">{gw.lsp_patched ? 'Yes' : 'No'}</p>
              </div>
            </div>
          </div>
        )}
        emptyState={{
          icon: <Globe className="h-16 w-16" />,
          title: 'No OVN gateways',
          description: 'Create an OVN NAT gateway to provide internet access for VPC subnets.',
          action: (
            <button onClick={() => setShowCreate(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create OVN Gateway
            </button>
          ),
        }}
      />

      {/* Create modal */}
      {showCreate && (
        <CreateOvnGatewayModal
          externalSubnets={externalSubnets}
          onClose={() => setShowCreate(false)}
        />
      )}

      {/* Detail modal */}
      {detailGateway && (
        <GatewayDetailModal
          gateway={detailGateway}
          onClose={() => setDetailGateway(null)}
        />
      )}

      {/* Delete confirmation */}
      {deleteConfirm && (
        <Modal
          isOpen
          onClose={() => setDeleteConfirm(null)}
          title="Delete OVN Gateway"
          size="sm"
        >
          <p className="text-sm text-surface-400 text-center mb-4">
            Delete{' '}
            <strong className="text-surface-200 font-mono">{deleteConfirm}</strong>? This will
            remove all EIPs, SNAT/DNAT rules, and FIPs. This cannot be undone.
          </p>
          <div className="flex gap-3">
            <button onClick={() => setDeleteConfirm(null)} className="flex-1 btn-secondary">
              Cancel
            </button>
            <button
              onClick={() => handleDelete(deleteConfirm)}
              disabled={deleteGateway.isPending}
              className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {deleteGateway.isPending ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
