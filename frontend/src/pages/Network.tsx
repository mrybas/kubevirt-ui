import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Network as NetworkIcon,
  Globe,
  Layers,
  Plus,
  Trash2,
  Eye,
  Activity,
  CheckCircle,
  AlertCircle,
} from 'lucide-react';
import {
  useProviderNetworks,
  useVlans,
  useSubnets,
  useDeleteSubnet,
} from '../hooks/useNetwork';
import type { Subnet } from '../types/network';
import { CreateNetworkWizard } from '../components/network/CreateNetworkWizard';
import { Modal } from '@/components/common/Modal';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';

// System subnets that are internal to Kube-OVN and not routable externally
const SYSTEM_SUBNET_NAMES = ['ovn-default', 'join', 'node-local-switch'];

function isSystemSubnet(subnet: Subnet): boolean {
  return !subnet.provider || SYSTEM_SUBNET_NAMES.includes(subnet.name);
}

export function Network({
  openCreate,
  onCreateOpened,
}: {
  openCreate?: boolean;
  onCreateOpened?: () => void;
} = {}) {
  const navigate = useNavigate();
  const [showCreateWizard, setShowCreateWizard] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  // Open create wizard when parent signals
  useEffect(() => {
    if (openCreate) {
      setShowCreateWizard(true);
      onCreateOpened?.();
    }
  }, [openCreate]);

  const { data: providerNetworks } = useProviderNetworks();
  const { data: vlans } = useVlans();
  const { data: subnets, isLoading: subnetsLoading } = useSubnets();

  const deleteSubnet = useDeleteSubnet();

  // Filter user subnets
  const allSubnets = subnets || [];
  const userSubnets = allSubnets.filter(s => !isSystemSubnet(s));

  // Calculate stats
  const totalIpsUsed = userSubnets.reduce((acc, s) => acc + (s.statistics?.used || 0), 0);
  const totalIps = userSubnets.reduce((acc, s) => acc + (s.statistics?.total || 0), 0);

  const filteredSubnets = searchQuery
    ? userSubnets.filter(s =>
        s.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (s.vpc ?? '').toLowerCase().includes(searchQuery.toLowerCase()) ||
        s.cidr_block.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : userSubnets;

  const handleDelete = async () => {
    if (!deleteConfirm) return;
    await deleteSubnet.mutateAsync(deleteConfirm);
    setDeleteConfirm(null);
  };

  const columns: Column<Subnet>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (subnet) => (
        <div className="flex items-center gap-2">
          <Layers className="h-3.5 w-3.5 text-primary-400 shrink-0" />
          <span className="font-medium text-surface-100">{subnet.name}</span>
        </div>
      ),
    },
    {
      key: 'vpc',
      header: 'VPC / Provider',
      sortable: true,
      hideOnMobile: true,
      accessor: (subnet) => (
        <span className="text-surface-300">
          {subnet.vpc ? (
            <span className="font-mono">{subnet.vpc}</span>
          ) : subnet.provider ? (
            <span className="font-mono text-surface-400">{subnet.provider}</span>
          ) : (
            <span className="text-surface-500">-</span>
          )}
        </span>
      ),
    },
    {
      key: 'cidr',
      header: 'CIDR',
      accessor: (subnet) => (
        <span className="font-mono text-surface-300">{subnet.cidr_block}</span>
      ),
    },
    {
      key: 'gateway',
      header: 'Gateway',
      hideOnMobile: true,
      accessor: (subnet) => (
        <span className="font-mono text-surface-400">{subnet.gateway}</span>
      ),
    },
    {
      key: 'usage',
      header: 'Usage',
      hideOnMobile: true,
      accessor: (subnet) => {
        const stats = subnet.statistics;
        const usagePercent = stats ? Math.round((stats.used / (stats.total || 1)) * 100) : 0;
        return (
          <div className="flex items-center gap-2">
            <div className="w-16 h-1.5 bg-surface-700 rounded-full overflow-hidden">
              <div
                className={`h-full ${
                  usagePercent > 80 ? 'bg-red-500' : usagePercent > 50 ? 'bg-amber-500' : 'bg-emerald-500'
                }`}
                style={{ width: `${usagePercent}%` }}
              />
            </div>
            <span className="text-xs text-surface-500">
              {stats?.used || 0}/{stats?.total || 0}
            </span>
          </div>
        );
      },
    },
    {
      key: 'purpose',
      header: 'Purpose',
      hideOnMobile: true,
      accessor: (subnet) => (
        <span className={`px-1.5 py-0.5 text-[10px] rounded font-medium ${
          subnet.purpose === 'infrastructure'
            ? 'bg-amber-500/20 text-amber-300'
            : 'bg-primary-500/20 text-primary-300'
        }`}>
          {subnet.purpose === 'infrastructure' ? 'Infra' : 'VM'}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (subnet) => (
        subnet.ready ? (
          <span className="flex items-center gap-1 text-emerald-400 text-xs">
            <CheckCircle className="h-3.5 w-3.5" />
            Ready
          </span>
        ) : (
          <span className="flex items-center gap-1 text-amber-400 text-xs">
            <AlertCircle className="h-3.5 w-3.5" />
            Pending
          </span>
        )
      ),
    },
  ];

  const getActions = (subnet: Subnet): MenuItem[] => [
    { label: 'View Details', icon: <Eye className="h-4 w-4" />, onClick: () => navigate(`/network/subnets/${subnet.name}`) },
    { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setDeleteConfirm(subnet.name), variant: 'danger' },
  ];

  return (
    <div className="space-y-6">
      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-primary-500/10 text-primary-400">
              <Layers className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">Total Subnets</p>
              <p className="text-2xl font-semibold text-surface-100">{userSubnets.length}</p>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-emerald-500/10 text-emerald-400">
              <Globe className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">Provider Networks</p>
              <p className="text-2xl font-semibold text-surface-100">{providerNetworks?.length || 0}</p>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-amber-500/10 text-amber-400">
              <NetworkIcon className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">VLANs</p>
              <p className="text-2xl font-semibold text-surface-100">{vlans?.length || 0}</p>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-sky-500/10 text-sky-400">
              <Activity className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">IPs Used / Total</p>
              <p className="text-2xl font-semibold text-surface-100">
                {totalIpsUsed} <span className="text-surface-500 text-lg">/ {totalIps}</span>
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* DataTable */}
      <DataTable
        columns={columns}
        data={filteredSubnets}
        loading={subnetsLoading}
        keyExtractor={(subnet) => subnet.name}
        actions={getActions}
        onRowClick={(subnet) => navigate(`/network/subnets/${subnet.name}`)}
        searchable
        searchPlaceholder="Search subnets by name, VPC, CIDR..."
        onSearch={setSearchQuery}
        emptyState={{
          icon: <Layers className="h-16 w-16" />,
          title: 'No subnets found',
          description: 'Create a network to get started with subnets.',
          action: (
            <button onClick={() => setShowCreateWizard(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create Network
            </button>
          ),
        }}
      />

      {/* Create Network Wizard */}
      {showCreateWizard && (
        <CreateNetworkWizard
          onClose={() => setShowCreateWizard(false)}
        />
      )}

      {/* Delete Confirmation */}
      <Modal
        isOpen={!!deleteConfirm}
        onClose={() => setDeleteConfirm(null)}
        title="Delete Subnet?"
      >
        <p className="text-surface-400 mb-4">
          Are you sure you want to delete "<strong className="text-surface-200">{deleteConfirm}</strong>"? This action cannot be undone.
        </p>
        <div className="flex justify-end gap-3">
          <button onClick={() => setDeleteConfirm(null)} className="btn-secondary">
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
      </Modal>
    </div>
  );
}
