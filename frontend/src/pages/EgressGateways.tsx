/**
 * Egress Gateways Page
 *
 * List, create, delete egress gateways; attach/detach VPCs.
 */

import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Plus,
  RefreshCw,
  Trash2,
  Globe,
  X,
  CheckCircle,
  Clock,
  Link,
  Unlink,
  Info,
  AlertTriangle,
  Server,
  Eye,
} from 'lucide-react';
import clsx from 'clsx';
import { useEgressGateways, useCreateEgressGateway, useDeleteEgressGateway, useAttachVpc, useDetachVpc } from '../hooks/useEgressGateways';
import { useVpcs } from '../hooks/useVpcs';
import type { EgressGateway, CreateEgressGatewayRequest, AttachVpcRequest } from '../types/egress';
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
// Node Selector editor (key-value pairs)
// ---------------------------------------------------------------------------

function NodeSelectorEditor({
  value,
  onChange,
}: {
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
}) {
  const entries = Object.entries(value);

  const addEntry = () => onChange({ ...value, '': '' });
  const removeEntry = (key: string) => {
    const next = { ...value };
    delete next[key];
    onChange(next);
  };
  const updateKey = (oldKey: string, newKey: string) => {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(value)) {
      next[k === oldKey ? newKey : k] = v;
    }
    onChange(next);
  };
  const updateValue = (key: string, val: string) => {
    onChange({ ...value, [key]: val });
  };

  return (
    <div className="space-y-2">
      {entries.map(([k, v], i) => (
        <div key={i} className="flex items-center gap-2">
          <input
            type="text"
            value={k}
            onChange={(e) => updateKey(k, e.target.value)}
            placeholder="key"
            className="flex-1 px-3 py-1.5 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 text-sm focus:outline-none focus:border-primary-500 font-mono"
          />
          <span className="text-surface-500">=</span>
          <input
            type="text"
            value={v}
            onChange={(e) => updateValue(k, e.target.value)}
            placeholder="value"
            className="flex-1 px-3 py-1.5 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 text-sm focus:outline-none focus:border-primary-500 font-mono"
          />
          <button
            type="button"
            onClick={() => removeEntry(k)}
            className="p-1.5 text-surface-500 hover:text-red-400 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={addEntry}
        className="flex items-center gap-1.5 text-sm text-surface-500 hover:text-primary-400 transition-colors"
      >
        <Plus className="w-4 h-4" />
        Add label
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

const isValidCIDR = (cidr: string): boolean => {
  const match = cidr.match(/^(\d{1,3}\.){3}\d{1,3}\/(\d{1,2})$/);
  if (!match) return false;
  const parts = cidr.split('/');
  const prefix = parseInt(parts[1]!);
  if (prefix < 8 || prefix > 30) return false;
  const octets = parts[0]!.split('.').map(Number);
  return octets.every(o => o >= 0 && o <= 255);
};

const isValidIP = (ip: string): boolean => {
  const match = ip.match(/^(\d{1,3}\.){3}\d{1,3}$/);
  if (!match) return false;
  const octets = ip.split('.').map(Number);
  return octets.every(o => o >= 0 && o <= 255);
};

// Validate IP or IP range (e.g., "192.168.1.1" or "192.168.1.1..192.168.1.10")
const isValidIPOrRange = (entry: string): boolean => {
  if (entry.includes('..')) {
    const [start, end] = entry.split('..');
    if (!start || !end) return false;
    if (!isValidIP(start) || !isValidIP(end)) return false;
    return ipToNum(start) <= ipToNum(end);
  }
  return isValidIP(entry);
};

// Count IPs in an exclude entry (single IP = 1, range = end - start + 1)
const countExcludeEntry = (entry: string): number => {
  if (entry.includes('..')) {
    const [start, end] = entry.split('..');
    if (!start || !end || !isValidIP(start) || !isValidIP(end)) return 0;
    return ipToNum(end) - ipToNum(start) + 1;
  }
  return 1;
};

const ipToNum = (ip: string): number => {
  const parts = ip.split('.').map(Number);
  return ((parts[0]! << 24) | (parts[1]! << 16) | (parts[2]! << 8) | parts[3]!) >>> 0;
};

const cidrsOverlap = (a: string, b: string): boolean => {
  if (!isValidCIDR(a) || !isValidCIDR(b)) return false;
  const [aIp, aPrefix] = a.split('/') as [string, string];
  const [bIp, bPrefix] = b.split('/') as [string, string];
  const aMask = ~((1 << (32 - parseInt(aPrefix))) - 1) >>> 0;
  const bMask = ~((1 << (32 - parseInt(bPrefix))) - 1) >>> 0;
  const aNet = ipToNum(aIp) & aMask;
  const bNet = ipToNum(bIp) & bMask;
  const commonMask = Math.min(parseInt(aPrefix), parseInt(bPrefix));
  const mask = ~((1 << (32 - commonMask)) - 1) >>> 0;
  return (aNet & mask) === (bNet & mask);
};

const availableIpsInCidr = (cidr: string, excludeCount: number): number | null => {
  if (!isValidCIDR(cidr)) return null;
  const prefix = parseInt(cidr.split('/')[1]!);
  const total = Math.pow(2, 32 - prefix) - 2; // minus network + broadcast
  return Math.max(0, total - excludeCount);
};

// ---------------------------------------------------------------------------
// Create Egress Gateway Modal
// ---------------------------------------------------------------------------

const DEFAULT_FORM: CreateEgressGatewayRequest = {
  name: '',
  gw_vpc_cidr: '10.199.0.0/24',
  transit_cidr: '10.255.0.0/24',
  replicas: 2,
  bfd_enabled: false,
  node_selector: {},
  exclude_ips: [],
};

type ExternalMode = 'existing' | 'create';

function CreateEgressGatewayModal({
  subnets,
  onClose,
}: {
  subnets: { name: string; cidr: string }[];
  onClose: () => void;
}) {
  const [form, setForm] = useState<CreateEgressGatewayRequest>(DEFAULT_FORM);
  const [externalMode, setExternalMode] = useState<ExternalMode>(subnets.length > 0 ? 'existing' : 'create');
  const [excludeIpInput, setExcludeIpInput] = useState('');
  const createGateway = useCreateEgressGateway();

  const gwCidrValid = form.gw_vpc_cidr.length === 0 || isValidCIDR(form.gw_vpc_cidr);
  const transitCidrValid = form.transit_cidr.length === 0 || isValidCIDR(form.transit_cidr);
  const cidrOverlap = isValidCIDR(form.gw_vpc_cidr) && isValidCIDR(form.transit_cidr) && cidrsOverlap(form.gw_vpc_cidr, form.transit_cidr);

  const excludeIpCount = (form.exclude_ips ?? []).reduce((sum, e) => sum + countExcludeEntry(e), 0);
  const externalCidr = externalMode === 'existing'
    ? subnets.find((s) => s.name === form.macvlan_subnet)?.cidr
    : form.external_cidr;
  const externalAvailable = externalCidr && isValidCIDR(externalCidr)
    ? availableIpsInCidr(externalCidr, excludeIpCount)
    : null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Build request — only send relevant fields for chosen mode
    const req: CreateEgressGatewayRequest = {
      ...form,
      macvlan_subnet: externalMode === 'existing' ? form.macvlan_subnet : undefined,
      external_interface: externalMode === 'create' ? form.external_interface : undefined,
      external_cidr: externalMode === 'create' ? form.external_cidr : undefined,
      external_gateway: externalMode === 'create' ? form.external_gateway : undefined,
    };
    await createGateway.mutateAsync(req);
    onClose();
  };

  const set = <K extends keyof CreateEgressGatewayRequest>(
    field: K,
    value: CreateEgressGatewayRequest[K]
  ) => setForm((prev) => ({ ...prev, [field]: value }));

  const addExcludeIps = (input: string) => {
    const newEntries = input
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0 && isValidIPOrRange(s) && !(form.exclude_ips ?? []).includes(s));
    if (newEntries.length > 0) {
      set('exclude_ips', [...(form.exclude_ips ?? []), ...newEntries]);
    }
    setExcludeIpInput('');
  };

  const removeExcludeIp = (ip: string) => {
    set('exclude_ips', (form.exclude_ips ?? []).filter((i) => i !== ip));
  };

  const externalValid = externalMode === 'existing'
    ? (form.macvlan_subnet ?? '').length > 0
    : (form.external_interface ?? '').length > 0
      && isValidCIDR(form.external_cidr ?? '')
      && isValidIP(form.external_gateway ?? '');

  const isValid =
    form.name.length > 0 &&
    externalValid &&
    isValidCIDR(form.gw_vpc_cidr) &&
    isValidCIDR(form.transit_cidr) &&
    !cidrOverlap;

  return (
    <Modal isOpen onClose={onClose} title="Create Egress Gateway" size="lg">
        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm text-surface-300 mb-1">Name</label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => set('name', e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
              placeholder="shared-egress"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>

          {/* Gateway VPC CIDR */}
          <div>
            <label className="block text-sm text-surface-300 mb-1">
              Gateway VPC CIDR{' '}
              <Tooltip text="Internal network for gateway pods. Must not overlap with tenant or transit CIDRs.">
                <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
              </Tooltip>
            </label>
            <input
              type="text"
              value={form.gw_vpc_cidr}
              onChange={(e) => set('gw_vpc_cidr', e.target.value)}
              placeholder="10.199.0.0/24"
              className={clsx(
                'w-full px-3 py-2 bg-surface-900 border rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none font-mono text-sm',
                !gwCidrValid ? 'border-red-500 focus:border-red-500' : 'border-surface-700 focus:border-primary-500'
              )}
            />
            {!gwCidrValid && (
              <p className="text-xs text-red-400 mt-1">Invalid CIDR format (e.g. 10.199.0.0/24, prefix /8-/30)</p>
            )}
          </div>

          {/* Transit CIDR */}
          <div>
            <label className="block text-sm text-surface-300 mb-1">
              Transit CIDR{' '}
              <Tooltip text="Transit network used for routing between tenant VPCs and the egress gateway. Each VPC attachment uses an address from this range.">
                <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
              </Tooltip>
            </label>
            <input
              type="text"
              value={form.transit_cidr}
              onChange={(e) => set('transit_cidr', e.target.value)}
              placeholder="10.255.0.0/24"
              className={clsx(
                'w-full px-3 py-2 bg-surface-900 border rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none font-mono text-sm',
                !transitCidrValid ? 'border-red-500 focus:border-red-500' : 'border-surface-700 focus:border-primary-500'
              )}
            />
            {!transitCidrValid && (
              <p className="text-xs text-red-400 mt-1">Invalid CIDR format (e.g. 10.255.0.0/24, prefix /8-/30)</p>
            )}
          </div>

          {/* CIDR overlap check */}
          {isValidCIDR(form.gw_vpc_cidr) && isValidCIDR(form.transit_cidr) && (
            cidrOverlap ? (
              <div className="flex items-center gap-2 p-2 bg-red-900/10 border border-red-800/30 rounded-lg">
                <AlertTriangle className="w-4 h-4 text-red-400 shrink-0" />
                <p className="text-xs text-red-400">Gateway VPC CIDR and Transit CIDR overlap. They must use separate address ranges.</p>
              </div>
            ) : (
              <div className="flex items-center gap-2 p-2 bg-emerald-900/10 border border-emerald-800/30 rounded-lg">
                <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                <p className="text-xs text-emerald-400">No CIDR overlaps detected</p>
              </div>
            )
          )}

          {/* External Network */}
          <div className="space-y-3">
            <label className="block text-sm text-surface-300">
              External Network{' '}
              <Tooltip text="Gateway pods get IPs from this network via macvlan. Use an existing VLAN subnet or create a new macvlan subnet from the node's physical network.">
                <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
              </Tooltip>
            </label>

            {/* Mode toggle */}
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setExternalMode('existing')}
                className={clsx(
                  'px-3 py-2 rounded-lg border text-sm text-left transition-colors',
                  externalMode === 'existing'
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 text-surface-400 hover:border-surface-600'
                )}
              >
                Use existing subnet
              </button>
              <button
                type="button"
                onClick={() => setExternalMode('create')}
                className={clsx(
                  'px-3 py-2 rounded-lg border text-sm text-left transition-colors',
                  externalMode === 'create'
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 text-surface-400 hover:border-surface-600'
                )}
              >
                Create new (macvlan)
              </button>
            </div>

            {externalMode === 'existing' ? (
              <div>
                {subnets.length > 0 ? (
                  <select
                    value={form.macvlan_subnet ?? ''}
                    onChange={(e) => set('macvlan_subnet', e.target.value)}
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                  >
                    <option value="">Select a subnet...</option>
                    {subnets.map((s) => (
                      <option key={s.name} value={s.name}>
                        {s.name} ({s.cidr})
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={form.macvlan_subnet ?? ''}
                    onChange={(e) => set('macvlan_subnet', e.target.value)}
                    placeholder="subnet-name"
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                  />
                )}
              </div>
            ) : (
              <div className="space-y-3 p-3 bg-surface-800/30 rounded-lg border border-surface-700">
                <div>
                  <label className="block text-xs text-surface-400 mb-1">
                    Interface{' '}
                    <Tooltip text="Physical NIC on cluster nodes that has access to the external network. Gateway pods attach to this interface via macvlan. Use the node management interface (e.g. eth0) or a dedicated NIC.">
                      <span className="text-surface-500 cursor-help">[?]</span>
                    </Tooltip>
                  </label>
                  <input
                    type="text"
                    value={form.external_interface ?? ''}
                    onChange={(e) => set('external_interface', e.target.value)}
                    placeholder="eth0"
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-surface-400 mb-1">CIDR</label>
                    <input
                      type="text"
                      value={form.external_cidr ?? ''}
                      onChange={(e) => set('external_cidr', e.target.value)}
                      placeholder="192.168.196.0/24"
                      className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-surface-400 mb-1">Gateway</label>
                    <input
                      type="text"
                      value={form.external_gateway ?? ''}
                      onChange={(e) => set('external_gateway', e.target.value)}
                      placeholder="192.168.196.1"
                      className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                    />
                  </div>
                </div>
                <div className="flex gap-2 p-2 bg-blue-900/10 border border-blue-800/30 rounded-lg">
                  <Info className="w-3.5 h-3.5 text-blue-400 shrink-0 mt-0.5" />
                  <p className="text-xs text-blue-300/80">
                    Creates a macvlan NetworkAttachmentDefinition + kube-ovn Subnet automatically.
                    Use this for the node management network or any network already present on the interface.
                  </p>
                </div>
              </div>
            )}

            {externalAvailable !== null && (
              <p className="text-xs text-surface-400">
                {externalAvailable} IPs available{excludeIpCount > 0 ? ` (${excludeIpCount} excluded)` : ''}
              </p>
            )}
          </div>

          {/* Replicas */}
          <div>
            <label className="block text-sm text-surface-300 mb-1">
              Replicas{' '}
              <Tooltip text="Number of gateway pod replicas. Use 2+ for high availability. Each replica runs on a different node.">
                <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
              </Tooltip>
            </label>
            <div className="flex gap-2">
              {[1, 2, 3, 4].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => set('replicas', n)}
                  className={clsx(
                    'px-4 py-2 rounded-lg border text-sm font-medium transition-colors',
                    form.replicas === n
                      ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                      : 'border-surface-700 bg-surface-900 text-surface-300 hover:border-surface-600'
                  )}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>

          {/* BFD Enabled */}
          <div className="flex items-center justify-between p-3 bg-surface-900 rounded-lg border border-surface-700">
            <div>
              <p className="text-sm font-medium text-surface-200">BFD Enabled</p>
              <p className="text-xs text-surface-500 mt-0.5">
                Bidirectional Forwarding Detection for sub-second failover detection
              </p>
            </div>
            <button
              type="button"
              onClick={() => set('bfd_enabled', !form.bfd_enabled)}
              className={clsx(
                'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
                form.bfd_enabled ? 'bg-primary-500' : 'bg-surface-600'
              )}
            >
              <span
                className={clsx(
                  'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
                  form.bfd_enabled ? 'translate-x-6' : 'translate-x-1'
                )}
              />
            </button>
          </div>

          {/* Exclude IPs */}
          <div>
            <label className="block text-sm text-surface-300 mb-1">
              Exclude IPs{' '}
              <Tooltip text="IPs or IP ranges in the external subnet that should not be assigned to gateway pods. Use single IPs (192.168.1.1) or ranges (192.168.1.1..192.168.1.10). Required when your hosting provider assigns specific IPs and you need to reserve some for other services.">
                <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
              </Tooltip>
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={excludeIpInput}
                onChange={(e) => setExcludeIpInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    addExcludeIps(excludeIpInput);
                  }
                }}
                placeholder="192.168.1.1 or 192.168.1.2..192.168.1.10"
                className="flex-1 px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              />
              <button
                type="button"
                onClick={() => addExcludeIps(excludeIpInput)}
                disabled={!excludeIpInput.trim()}
                className="px-3 py-2 bg-surface-700 hover:bg-surface-600 disabled:opacity-50 text-surface-300 rounded-lg text-sm transition-colors"
              >
                Add
              </button>
            </div>
            {(form.exclude_ips ?? []).length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-2">
                {(form.exclude_ips ?? []).map((ip) => (
                  <span
                    key={ip}
                    className="inline-flex items-center gap-1 px-2 py-1 bg-surface-900 border border-surface-700 rounded-md text-xs font-mono text-surface-300"
                  >
                    {ip}
                    <button
                      type="button"
                      onClick={() => removeExcludeIp(ip)}
                      className="text-surface-500 hover:text-red-400 transition-colors"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Provider guidance */}
          <div className="flex gap-3 p-3 bg-blue-900/10 border border-blue-800/30 rounded-lg">
            <Info className="w-4 h-4 text-blue-400 shrink-0 mt-0.5" />
            <p className="text-xs text-blue-300/80">
              Egress gateway requires additional IP addresses from your hosting provider. The macvlan subnet must correspond to a real IP block assigned to your server. Contact your provider to purchase additional IPs if needed.
            </p>
          </div>

          {/* Node Selector */}
          <div>
            <label className="block text-sm text-surface-300 mb-2">
              Node Selector{' '}
              <Tooltip text="Schedule gateway pods only on nodes with these labels. E.g. role=egress to pin to dedicated egress nodes.">
                <span className="text-surface-500 cursor-help text-xs ml-1">[?]</span>
              </Tooltip>
            </label>
            <NodeSelectorEditor
              value={form.node_selector}
              onChange={(v) => set('node_selector', v)}
            />
          </div>

          {createGateway.isError && (
            <div className="flex items-start gap-2 p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
              <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
              <p className="text-sm text-red-400">
                {(createGateway.error as Error)?.message || 'Failed to create egress gateway'}
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
// Attach VPC Modal
// ---------------------------------------------------------------------------

function AttachVpcModal({
  gatewayName,
  existingVpcNames,
  onClose,
}: {
  gatewayName: string;
  existingVpcNames: string[];
  onClose: () => void;
}) {
  const { data: vpcsData } = useVpcs();
  const attachVpcMutation = useAttachVpc(gatewayName);

  const [vpcName, setVpcName] = useState('');
  const [subnetName, setSubnetName] = useState('');
  const [cidr, setCidr] = useState('');

  const availableVpcs = (vpcsData?.items ?? []).filter((v: any) => !existingVpcNames.includes(v.name));
  const selectedVpc = vpcsData?.items.find((v: any) => v.name === vpcName);

  const handleVpcChange = (name: string) => {
    setVpcName(name);
    setSubnetName('');
    setCidr('');
  };

  const handleSubnetChange = (name: string) => {
    setSubnetName(name);
    const subnet = selectedVpc?.subnets.find((s: any) => s.name === name);
    if (subnet) setCidr(subnet.cidr_block);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const req: AttachVpcRequest = { vpc_name: vpcName, subnet_name: subnetName, cidr };
    await attachVpcMutation.mutateAsync(req);
    onClose();
  };

  const isValid = vpcName && subnetName && cidr;

  return (
    <Modal isOpen onClose={onClose} title="Attach VPC">
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-surface-300 mb-1">VPC</label>
            <select
              value={vpcName}
              onChange={(e) => handleVpcChange(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
              required
            >
              <option value="">Select VPC...</option>
              {availableVpcs.map((v: any) => (
                <option key={v.name} value={v.name}>
                  {v.name}
                </option>
              ))}
            </select>
            {availableVpcs.length === 0 && (
              <p className="text-xs text-surface-500 mt-1">All VPCs are already attached</p>
            )}
          </div>

          {selectedVpc && (
            <div>
              <label className="block text-sm text-surface-300 mb-1">Subnet</label>
              <select
                value={subnetName}
                onChange={(e) => handleSubnetChange(e.target.value)}
                className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                required
              >
                <option value="">Select subnet...</option>
                {selectedVpc.subnets.map((s: any) => (
                  <option key={s.name} value={s.name}>
                    {s.name} ({s.cidr_block})
                  </option>
                ))}
              </select>
            </div>
          )}

          {subnetName && (
            <div>
              <label className="block text-sm text-surface-300 mb-1">Subnet CIDR</label>
              <input
                type="text"
                value={cidr}
                onChange={(e) => setCidr(e.target.value)}
                placeholder="10.200.0.0/24"
                className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              />
            </div>
          )}

          {attachVpcMutation.isError && (
            <p className="text-sm text-red-400">
              {(attachVpcMutation.error as Error)?.message || 'Failed to attach VPC'}
            </p>
          )}

          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button
              type="submit"
              disabled={!isValid || attachVpcMutation.isPending}
              className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
            >
              {attachVpcMutation.isPending ? 'Attaching...' : 'Attach'}
            </button>
          </div>
        </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Detail Modal (view gateway details + attached VPCs + assigned IPs)
// ---------------------------------------------------------------------------

function GatewayDetailModal({
  gateway,
  onClose,
}: {
  gateway: EgressGateway;
  onClose: () => void;
}) {
  const [showAttach, setShowAttach] = useState(false);
  const detachVpcMutation = useDetachVpc(gateway.name);

  const handleDetach = async (vpcName: string, subnetName: string) => {
    await detachVpcMutation.mutateAsync({ vpc_name: vpcName, subnet_name: subnetName });
  };

  return (
    <>
      <Modal isOpen onClose={onClose} title={`Egress Gateway: ${gateway.name}`} size="lg">
        <div className="space-y-5">
          {/* Summary */}
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-surface-400">GW VPC CIDR</span>
              <p className="font-mono text-surface-200">{gateway.gw_vpc_cidr}</p>
            </div>
            <div>
              <span className="text-surface-400">Transit CIDR</span>
              <p className="font-mono text-surface-200">{gateway.transit_cidr}</p>
            </div>
            <div>
              <span className="text-surface-400">Macvlan Subnet</span>
              <p className="font-mono text-surface-200">{gateway.macvlan_subnet}</p>
            </div>
            <div>
              <span className="text-surface-400">BFD</span>
              <p className="text-surface-200">{gateway.bfd_enabled ? 'Enabled' : 'Disabled'}</p>
            </div>
          </div>

          {/* Node Selector */}
          {Object.keys(gateway.node_selector).length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">Node Selector</h4>
              <div className="flex flex-wrap gap-2">
                {Object.entries(gateway.node_selector).map(([k, v]) => (
                  <span key={k} className="px-2 py-1 bg-surface-900 border border-surface-700 rounded text-xs font-mono text-surface-300">
                    {k}={v}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Attached VPCs */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider">Attached VPCs</h4>
              <button onClick={() => setShowAttach(true)} className="text-xs text-primary-400 hover:text-primary-300 flex items-center gap-1">
                <Link className="h-3.5 w-3.5" />
                Attach VPC
              </button>
            </div>
            {gateway.attached_vpcs.length === 0 ? (
              <p className="text-sm text-surface-500 italic">No VPCs attached</p>
            ) : (
              <div className="divide-y divide-surface-700 border border-surface-700 rounded-xl overflow-hidden">
                {gateway.attached_vpcs.map((vpc) => (
                  <div
                    key={`${vpc.vpc_name}/${vpc.subnet_name}`}
                    className="flex items-center justify-between px-4 py-2.5 bg-surface-800/50 hover:bg-surface-800 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <Globe className="w-3.5 h-3.5 text-surface-500" />
                      <span className="text-sm font-mono text-surface-200">{vpc.vpc_name}</span>
                      <span className="text-xs text-surface-500">/{vpc.subnet_name}</span>
                      <span className="text-xs font-mono text-surface-400">{vpc.cidr}</span>
                    </div>
                    <button
                      onClick={() => handleDetach(vpc.vpc_name, vpc.subnet_name)}
                      disabled={detachVpcMutation.isPending}
                      className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                      title="Detach VPC"
                    >
                      <Unlink className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Assigned IPs */}
          {gateway.assigned_ips.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">Assigned IPs</h4>
              <div className="border border-surface-700 rounded-xl overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-800/80 text-xs text-surface-400">
                      <th className="text-left px-4 py-2 font-medium">Pod</th>
                      <th className="text-left px-4 py-2 font-medium">Node</th>
                      <th className="text-left px-4 py-2 font-medium">Internal IP</th>
                      <th className="text-left px-4 py-2 font-medium">External IP</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-surface-700">
                    {gateway.assigned_ips.map((pod) => (
                      <tr key={pod.pod} className="bg-surface-800/50 hover:bg-surface-800 transition-colors">
                        <td className="px-4 py-2">
                          <div className="flex items-center gap-2">
                            <Server className="w-3.5 h-3.5 text-surface-500" />
                            <span className="font-mono text-surface-200">{pod.pod}</span>
                          </div>
                        </td>
                        <td className="px-4 py-2 font-mono text-surface-300">{pod.node}</td>
                        <td className="px-4 py-2 font-mono text-surface-300">{pod.internal_ip || '-'}</td>
                        <td className="px-4 py-2 font-mono text-primary-400">{pod.external_ip || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </Modal>

      {showAttach && (
        <AttachVpcModal
          gatewayName={gateway.name}
          existingVpcNames={gateway.attached_vpcs.map((v) => v.vpc_name)}
          onClose={() => setShowAttach(false)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function EgressGateways() {
  const [showCreate, setShowCreate] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [detailGateway, setDetailGateway] = useState<EgressGateway | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const { data, isLoading, refetch } = useEgressGateways();
  // const { data: vpcsData } = useVpcs();
  const deleteGateway = useDeleteEgressGateway();

  // Fetch all subnets for macvlan dropdown — filter to VLAN-backed subnets only
  const { data: allSubnets } = useQuery({ queryKey: ['subnets'], queryFn: listSubnets });
  const subnetInfos = useMemo(
    () => (allSubnets ?? [])
      .filter((s) => s.vlan) // Only VLAN-backed subnets can be used for macvlan
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

  const columns: Column<EgressGateway>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (gw) => (
        <span className="font-medium font-mono text-surface-100">{gw.name}</span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (gw) => (
        gw.ready ? (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-emerald-400 bg-emerald-500/10">
            <CheckCircle className="h-3.5 w-3.5" />
            Ready
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-amber-400 bg-amber-500/10">
            <Clock className="h-3.5 w-3.5" />
            Not Ready
          </span>
        )
      ),
    },
    {
      key: 'replicas',
      header: 'Replicas',
      hideOnMobile: true,
      accessor: (gw) => <span>{gw.replicas}</span>,
    },
    {
      key: 'external_ip',
      header: 'External IP',
      hideOnMobile: true,
      accessor: (gw) => {
        const ips = gw.assigned_ips.filter(p => p.external_ip).map(p => p.external_ip);
        return ips.length > 0
          ? <span className="font-mono text-primary-400">{ips.join(', ')}</span>
          : <span className="text-surface-500">-</span>;
      },
    },
    {
      key: 'attached_vpcs',
      header: 'Attached VPCs',
      hideOnMobile: true,
      accessor: (gw) => (
        <span>{gw.attached_vpcs.length} VPC{gw.attached_vpcs.length !== 1 ? 's' : ''}</span>
      ),
    },
  ];

  const getActions = (gw: EgressGateway): MenuItem[] => {
    const items: MenuItem[] = [
      { label: 'View Details', icon: <Eye className="h-4 w-4" />, onClick: () => setDetailGateway(gw) },
    ];
    if (gw.attached_vpcs.length === 0) {
      items.push({ label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setDeleteConfirm(gw.name), variant: 'danger' });
    }
    return items;
  };

  return (
    <div className="space-y-6">
      <ActionBar
        title="Egress Gateways"
        subtitle="Hub-and-spoke internet egress for tenant VPCs"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        <button onClick={() => setShowCreate(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create Egress Gateway
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
        searchPlaceholder="Search egress gateways..."
        onSearch={setSearchQuery}
        expandable={(gw) => (
          <div className="px-4 py-3 bg-surface-900/50">
            <div className="grid grid-cols-2 gap-4 text-sm mb-3">
              <div>
                <span className="text-xs text-surface-400">GW VPC CIDR</span>
                <p className="font-mono text-surface-200">{gw.gw_vpc_cidr}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">Transit CIDR</span>
                <p className="font-mono text-surface-200">{gw.transit_cidr}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">Macvlan Subnet</span>
                <p className="font-mono text-surface-200">{gw.macvlan_subnet}</p>
              </div>
              <div>
                <span className="text-xs text-surface-400">BFD</span>
                <p className="text-surface-200">{gw.bfd_enabled ? 'Enabled' : 'Disabled'}</p>
              </div>
            </div>
            {gw.attached_vpcs.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">Attached VPCs</div>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-surface-400">
                      <th className="text-left py-1 pr-4">VPC</th>
                      <th className="text-left py-1 pr-4">Subnet</th>
                      <th className="text-left py-1">CIDR</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gw.attached_vpcs.map(vpc => (
                      <tr key={`${vpc.vpc_name}/${vpc.subnet_name}`} className="border-t border-surface-800">
                        <td className="py-1.5 pr-4 font-mono">{vpc.vpc_name}</td>
                        <td className="py-1.5 pr-4 font-mono">{vpc.subnet_name}</td>
                        <td className="py-1.5 font-mono">{vpc.cidr}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
        emptyState={{
          icon: <Globe className="h-16 w-16" />,
          title: 'No egress gateways',
          description: 'Create a gateway to provide internet access for tenant VPCs.',
          action: (
            <button onClick={() => setShowCreate(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create Egress Gateway
            </button>
          ),
        }}
      />

      {/* Create modal */}
      {showCreate && (
        <CreateEgressGatewayModal
          subnets={subnetInfos}
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
          title="Delete Egress Gateway"
          size="sm"
        >
          <p className="text-sm text-surface-400 text-center mb-4">
            Delete <strong className="text-surface-200 font-mono">{deleteConfirm}</strong>? This cannot be undone.
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
