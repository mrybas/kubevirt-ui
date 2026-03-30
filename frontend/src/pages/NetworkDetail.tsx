import { useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import {
  ArrowLeft,
  Layers,
  Server,
  Network,
  Copy,
  CheckCircle,
  AlertCircle,
  Plus,
  Trash2,
  RefreshCw,
  Monitor,
  Box,
  Lock,
  Unlock,
  Info,
} from 'lucide-react';
import { useSubnetDetail, useReserveIP, useUnreserveIP, useDeleteSubnet } from '../hooks/useNetwork';
import type { IPLease, ReservedIP } from '../types/network';
import { useNotifications } from '../store/notifications';
import { SubnetAclEditor } from '../components/subnet/SubnetAclEditor';

type TabType = 'leases' | 'reserved' | 'acls' | 'settings';

export function NetworkDetail() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const { addNotification } = useNotifications();
  
  const [activeTab, setActiveTab] = useState<TabType>('leases');
  const [showReserveModal, setShowReserveModal] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [copiedIP, setCopiedIP] = useState<string | null>(null);

  const { data, isLoading, refetch } = useSubnetDetail(name || '');
  const reserveIP = useReserveIP(name || '');
  const unreserveIP = useUnreserveIP(name || '');
  const deleteSubnet = useDeleteSubnet();

  const copyToClipboard = async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopiedIP(text);
    setTimeout(() => setCopiedIP(null), 2000);
    addNotification({
      type: 'success',
      title: 'Copied',
      message: `Copied "${text}" to clipboard`,
    });
  };

  const handleDelete = async () => {
    if (!name) return;
    await deleteSubnet.mutateAsync(name);
    navigate('/network');
  };

  if (isLoading || !data) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-8 w-8 animate-spin text-surface-500" />
      </div>
    );
  }

  const { subnet, leases, reserved } = data;
  const stats = subnet.statistics;
  const usagePercent = stats ? Math.round((stats.used / (stats.total || 1)) * 100) : 0;

  const tabs = [
    { id: 'leases' as const, label: 'IP Leases', count: leases.length },
    { id: 'reserved' as const, label: 'Reserved', count: reserved.length },
    { id: 'acls' as const, label: 'ACL Rules' },
    { id: 'settings' as const, label: 'Settings' },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link
            to="/network"
            className="p-2 hover:bg-surface-700 rounded-lg text-surface-400"
          >
            <ArrowLeft className="h-5 w-5" />
          </Link>
          <div>
            <div className="flex items-center gap-3">
              <Layers className="h-6 w-6 text-primary-400" />
              <h1 className="text-2xl font-bold text-surface-100">{subnet.name}</h1>
              {subnet.ready ? (
                <span className="badge badge-success flex items-center gap-1">
                  <CheckCircle className="h-3 w-3" />
                  Ready
                </span>
              ) : (
                <span className="badge badge-warning flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" />
                  Pending
                </span>
              )}
            </div>
            <p className="text-surface-400 mt-1">
              {subnet.cidr_block} • Gateway: {subnet.gateway}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button onClick={() => refetch()} className="btn-secondary">
            <RefreshCw className="h-4 w-4" />
            Refresh
          </button>
          <button
            onClick={() => setShowDeleteConfirm(true)}
            className="btn-danger"
          >
            <Trash2 className="h-4 w-4" />
            Delete
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="card">
          <div className="card-body">
            <div className="flex items-center justify-between">
              <p className="text-sm text-surface-400">CIDR Block</p>
              <Network className="h-4 w-4 text-surface-500" />
            </div>
            <p className="text-xl font-semibold text-surface-100 font-mono mt-1">
              {subnet.cidr_block}
            </p>
          </div>
        </div>

        <div className="card">
          <div className="card-body">
            <div className="flex items-center justify-between">
              <p className="text-sm text-surface-400">Gateway</p>
              <Server className="h-4 w-4 text-surface-500" />
            </div>
            <p className="text-xl font-semibold text-surface-100 font-mono mt-1">
              {subnet.gateway}
            </p>
          </div>
        </div>

        <div className="card">
          <div className="card-body">
            <div className="flex items-center justify-between">
              <p className="text-sm text-surface-400">IPs Used</p>
              <span className="text-emerald-400 text-sm font-medium">
                {stats?.available || 0} available
              </span>
            </div>
            <div className="mt-2">
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-semibold text-surface-100">
                  {stats?.used || 0}
                </span>
                <span className="text-surface-500">/ {stats?.total || 0}</span>
              </div>
              <div className="mt-2 h-2 bg-surface-700 rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all ${
                    usagePercent > 80
                      ? 'bg-red-500'
                      : usagePercent > 50
                      ? 'bg-amber-500'
                      : 'bg-emerald-500'
                  }`}
                  style={{ width: `${usagePercent}%` }}
                />
              </div>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-body">
            <div className="flex items-center justify-between">
              <p className="text-sm text-surface-400">Reserved</p>
              <Lock className="h-4 w-4 text-surface-500" />
            </div>
            <p className="text-xl font-semibold text-surface-100 mt-1">
              {stats?.reserved || 0} IPs
            </p>
            <p className="text-xs text-surface-500 mt-1">
              {reserved.length} range(s)
            </p>
          </div>
        </div>
      </div>

      {/* Provider info */}
      {subnet.provider && (
        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-lg p-2 bg-emerald-500/10">
              <Server className="h-5 w-5 text-emerald-400" />
            </div>
            <div>
              <p className="text-sm text-surface-400">Provider Network</p>
              <p className="text-surface-100 font-medium">{subnet.provider}</p>
            </div>
            {subnet.vlan && (
              <>
                <div className="h-8 w-px bg-surface-700" />
                <div>
                  <p className="text-sm text-surface-400">VLAN</p>
                  <p className="text-surface-100 font-medium">{subnet.vlan}</p>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-surface-700">
        <nav className="flex gap-4">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab.id
                  ? 'border-primary-500 text-primary-400'
                  : 'border-transparent text-surface-400 hover:text-surface-200'
              }`}
            >
              {tab.label}
              {tab.count !== undefined && (
                <span className="ml-2 px-2 py-0.5 rounded-full bg-surface-700 text-xs">
                  {tab.count}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Content */}
      <div className="card">
        {activeTab === 'leases' && (
          <LeasesTable leases={leases} copiedIP={copiedIP} onCopy={copyToClipboard} />
        )}

        {activeTab === 'reserved' && (
          <ReservedTable
            reserved={reserved}
            onAdd={() => setShowReserveModal(true)}
            onRemove={(ip) => unreserveIP.mutate(ip)}
            isRemoving={unreserveIP.isPending}
          />
        )}

        {activeTab === 'acls' && (
          <SubnetAclEditor subnetName={subnet.name} />
        )}

        {activeTab === 'settings' && (
          <SubnetSettings subnet={subnet} />
        )}
      </div>

      {/* Reserve IP Modal */}
      {showReserveModal && (
        <ReserveIPModal
          subnetName={name || ''}
          cidr={subnet.cidr_block}
          onClose={() => setShowReserveModal(false)}
          onReserve={(data) => {
            reserveIP.mutate(data, {
              onSuccess: () => setShowReserveModal(false),
            });
          }}
          isLoading={reserveIP.isPending}
        />
      )}

      {/* Delete Confirmation */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="card max-w-md w-full mx-4">
            <div className="card-body">
              <h3 className="text-lg font-semibold text-surface-100 mb-2">
                Delete Subnet?
              </h3>
              <p className="text-surface-400 mb-4">
                Are you sure you want to delete "{subnet.name}"? This will release all IP allocations.
              </p>
              <div className="flex justify-end gap-3">
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  className="btn-danger"
                  disabled={deleteSubnet.isPending}
                >
                  <Trash2 className="h-4 w-4" />
                  Delete
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Leases Table
// ============================================================================

interface LeasesTableProps {
  leases: IPLease[];
  copiedIP: string | null;
  onCopy: (ip: string) => void;
}

function LeasesTable({ leases, copiedIP, onCopy }: LeasesTableProps) {
  if (leases.length === 0) {
    return (
      <div className="card-body text-center py-16">
        <Network className="h-16 w-16 mx-auto text-surface-600 mb-4" />
        <h3 className="text-lg font-semibold text-surface-100 mb-2">
          No IP leases
        </h3>
        <p className="text-surface-400 max-w-md mx-auto">
          IP addresses will appear here when VMs or pods use this subnet.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="table w-full">
        <thead>
          <tr>
            <th>IP Address</th>
            <th>Resource</th>
            <th>Namespace</th>
            <th>Node</th>
            <th>MAC Address</th>
          </tr>
        </thead>
        <tbody>
          {leases.map((lease) => (
            <tr key={lease.ip_address}>
              <td>
                <button
                  onClick={() => onCopy(lease.ip_address)}
                  className="flex items-center gap-2 group"
                >
                  <span className="font-mono text-surface-100">{lease.ip_address}</span>
                  {copiedIP === lease.ip_address ? (
                    <CheckCircle className="h-4 w-4 text-emerald-400" />
                  ) : (
                    <Copy className="h-4 w-4 text-surface-500 opacity-0 group-hover:opacity-100" />
                  )}
                </button>
              </td>
              <td>
                <div className="flex items-center gap-2">
                  {lease.resource_type === 'vm' ? (
                    <Monitor className="h-4 w-4 text-primary-400" />
                  ) : (
                    <Box className="h-4 w-4 text-sky-400" />
                  )}
                  <span className="text-surface-200">
                    {lease.resource_name || lease.pod_name}
                  </span>
                  <span className="text-xs text-surface-500 capitalize">
                    ({lease.resource_type})
                  </span>
                </div>
              </td>
              <td className="text-surface-300">{lease.namespace || '-'}</td>
              <td className="text-surface-300">{lease.node_name || '-'}</td>
              <td className="font-mono text-xs text-surface-400">
                {lease.mac_address || '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ============================================================================
// Reserved Table
// ============================================================================

interface ReservedTableProps {
  reserved: ReservedIP[];
  onAdd: () => void;
  onRemove: (ip: string) => void;
  isRemoving: boolean;
}

function ReservedTable({ reserved, onAdd, onRemove, isRemoving }: ReservedTableProps) {
  return (
    <div>
      <div className="card-body border-b border-surface-700 flex items-center justify-between">
        <div>
          <h3 className="font-medium text-surface-100">Reserved IP Ranges</h3>
          <p className="text-sm text-surface-400">
            These IPs won't be assigned to VMs
          </p>
        </div>
        <button onClick={onAdd} className="btn-primary">
          <Plus className="h-4 w-4" />
          Reserve IP
        </button>
      </div>

      {reserved.length === 0 ? (
        <div className="card-body text-center py-12">
          <Unlock className="h-12 w-12 mx-auto text-surface-600 mb-4" />
          <p className="text-surface-400">No reserved IPs</p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="table w-full">
            <thead>
              <tr>
                <th>IP / Range</th>
                <th>Count</th>
                <th>Note</th>
                <th className="w-20"></th>
              </tr>
            </thead>
            <tbody>
              {reserved.map((item) => (
                <tr key={item.ip_or_range}>
                  <td className="font-mono text-surface-100">{item.ip_or_range}</td>
                  <td>
                    <span className="badge badge-default">{item.count} IP(s)</span>
                  </td>
                  <td className="text-surface-400">{item.note || '-'}</td>
                  <td>
                    <button
                      onClick={() => onRemove(item.ip_or_range)}
                      disabled={isRemoving}
                      className="p-1 hover:bg-surface-700 rounded text-surface-400 hover:text-red-400 disabled:opacity-50"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Subnet Settings
// ============================================================================

interface SubnetSettingsProps {
  subnet: {
    name: string;
    cidr_block: string;
    gateway: string;
    protocol: string;
    enable_dhcp: boolean;
    provider?: string;
    vlan?: string;
    vpc?: string;
    namespace?: string;
    disable_gateway_check?: boolean;
  };
}

function SubnetSettings({ subnet }: SubnetSettingsProps) {
  return (
    <div className="card-body space-y-6">
      <div className="grid grid-cols-2 gap-6">
        <div>
          <h4 className="text-sm font-medium text-surface-400 mb-1">Protocol</h4>
          <p className="text-surface-100">{subnet.protocol}</p>
        </div>
        <div>
          <h4 className="text-sm font-medium text-surface-400 mb-1">DHCP</h4>
          <p className="text-surface-100">
            {subnet.enable_dhcp ? (
              <span className="flex items-center gap-1 text-emerald-400">
                <CheckCircle className="h-4 w-4" />
                Enabled
              </span>
            ) : (
              <span className="flex items-center gap-1 text-surface-500">
                <AlertCircle className="h-4 w-4" />
                Disabled
              </span>
            )}
          </p>
        </div>
        {subnet.provider && (
          <div>
            <h4 className="text-sm font-medium text-surface-400 mb-1">Provider</h4>
            <p className="text-surface-100">{subnet.provider}</p>
          </div>
        )}
        {subnet.vlan && (
          <div>
            <h4 className="text-sm font-medium text-surface-400 mb-1">VLAN</h4>
            <p className="text-surface-100">{subnet.vlan}</p>
          </div>
        )}
        {subnet.vpc && (
          <div>
            <h4 className="text-sm font-medium text-surface-400 mb-1">VPC</h4>
            <p className="text-surface-100">{subnet.vpc}</p>
          </div>
        )}
      </div>

      {subnet.namespace && (
        <div>
          <h4 className="text-sm font-medium text-surface-400 mb-1">Namespace</h4>
          <span className="badge badge-info">{subnet.namespace}</span>
        </div>
      )}

      {subnet.disable_gateway_check && (
        <div>
          <h4 className="text-sm font-medium text-surface-400 mb-1">Gateway Check</h4>
          <span className="text-amber-400 text-sm">Disabled</span>
        </div>
      )}

      <div className="bg-surface-800/50 rounded-lg p-4">
        <div className="flex items-start gap-2">
          <Info className="h-4 w-4 text-primary-400 mt-0.5" />
          <div className="text-sm text-surface-400">
            <p>
              To modify subnet settings, you need to edit the Subnet resource directly
              or delete and recreate it. Active IP leases will be affected.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Reserve IP Modal
// ============================================================================

interface ReserveIPModalProps {
  subnetName: string;
  cidr: string;
  onClose: () => void;
  onReserve: (data: { ip_or_range: string; note?: string }) => void;
  isLoading: boolean;
}

function ReserveIPModal({ cidr, onClose, onReserve, isLoading }: ReserveIPModalProps) {
  const [mode, setMode] = useState<'single' | 'range'>('single');
  const [singleIP, setSingleIP] = useState('');
  const [rangeFrom, setRangeFrom] = useState('');
  const [rangeTo, setRangeTo] = useState('');
  const [note, setNote] = useState('');

  const handleSubmit = () => {
    const ip_or_range = mode === 'single' ? singleIP : `${rangeFrom}..${rangeTo}`;
    onReserve({ ip_or_range, note: note || undefined });
  };

  // Extract network prefix for placeholder
  const networkPrefix = cidr.split('.').slice(0, 3).join('.') + '.';

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="card max-w-md w-full mx-4">
        <div className="card-body">
          <h3 className="text-lg font-semibold text-surface-100 mb-4">
            Reserve IP Address
          </h3>

          <div className="space-y-4">
            {/* Mode selection */}
            <div className="flex gap-2">
              <button
                onClick={() => setMode('single')}
                className={`flex-1 py-2 px-4 rounded-lg border-2 ${
                  mode === 'single'
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 text-surface-400'
                }`}
              >
                Single IP
              </button>
              <button
                onClick={() => setMode('range')}
                className={`flex-1 py-2 px-4 rounded-lg border-2 ${
                  mode === 'range'
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 text-surface-400'
                }`}
              >
                IP Range
              </button>
            </div>

            {mode === 'single' ? (
              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  IP Address
                </label>
                <input
                  type="text"
                  value={singleIP}
                  onChange={(e) => setSingleIP(e.target.value)}
                  className="input w-full font-mono"
                  placeholder={`${networkPrefix}250`}
                />
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <div className="flex-1">
                  <label className="block text-sm font-medium text-surface-200 mb-2">
                    From
                  </label>
                  <input
                    type="text"
                    value={rangeFrom}
                    onChange={(e) => setRangeFrom(e.target.value)}
                    className="input w-full font-mono"
                    placeholder={`${networkPrefix}240`}
                  />
                </div>
                <span className="text-surface-400 mt-6">—</span>
                <div className="flex-1">
                  <label className="block text-sm font-medium text-surface-200 mb-2">
                    To
                  </label>
                  <input
                    type="text"
                    value={rangeTo}
                    onChange={(e) => setRangeTo(e.target.value)}
                    className="input w-full font-mono"
                    placeholder={`${networkPrefix}250`}
                  />
                </div>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-surface-200 mb-2">
                Note (optional)
              </label>
              <input
                type="text"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                className="input w-full"
                placeholder="e.g., VRRP VIP, Load Balancer"
              />
            </div>
          </div>

          <div className="flex justify-end gap-3 mt-6">
            <button onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={isLoading || (mode === 'single' ? !singleIP : !rangeFrom || !rangeTo)}
              className="btn-primary"
            >
              {isLoading ? 'Reserving...' : 'Reserve'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
