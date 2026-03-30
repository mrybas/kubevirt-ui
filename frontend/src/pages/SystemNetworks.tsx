// import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Lock,
  Layers,
  Activity,
  Server,
  CheckCircle,
  AlertCircle,
  RefreshCw,
  Info,
  Eye,
} from 'lucide-react';
import { useSubnets } from '../hooks/useNetwork';
import type { Subnet } from '../types/network';
import { DataTable, type Column } from '@/components/common/DataTable';
// import type { MenuItem } from '@/components/common/KebabMenu';

// System subnets that are internal to Kube-OVN and not routable externally
const SYSTEM_SUBNET_NAMES = ['ovn-default', 'join', 'node-local-switch'];

function isSystemSubnet(subnet: Subnet): boolean {
  return !subnet.provider || SYSTEM_SUBNET_NAMES.includes(subnet.name);
}

export function SystemNetworks() {
  const navigate = useNavigate();
  // const [searchQuery, setSearchQuery] = useState(''); // TODO: wire up search
  const { data: subnets, isLoading, refetch } = useSubnets();

  // Filter to only show system subnets
  const systemSubnets = (subnets || []).filter(isSystemSubnet);

  // Calculate stats for system networks
  const totalIpsUsed = systemSubnets.reduce((acc, s) => acc + (s.statistics?.used || 0), 0);
  const totalIpsAvailable = systemSubnets.reduce((acc, s) => acc + (s.statistics?.available || 0), 0);

  const subnetColumns: Column<Subnet>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (s) => (
        <div className="flex items-center gap-2">
          <Lock className="h-4 w-4 text-surface-500" />
          <span className="font-medium text-surface-300">{s.name}</span>
          <span className="px-1.5 py-0.5 text-[10px] font-medium rounded bg-surface-700 text-surface-400">SYSTEM</span>
        </div>
      ),
    },
    { key: 'cidr', header: 'CIDR', accessor: (s) => <span className="font-mono text-sm text-surface-400">{s.cidr_block}</span> },
    { key: 'gateway', header: 'Gateway', hideOnMobile: true, accessor: (s) => <span className="font-mono text-sm text-surface-400">{s.gateway}</span> },
    {
      key: 'purpose',
      header: 'Purpose',
      hideOnMobile: true,
      accessor: (s) => {
        const purpose = s.name === 'ovn-default' ? 'Pod Network' : s.name === 'join' ? 'Node Connectivity' : 'Internal';
        return <span className="text-sm text-surface-400">{purpose}</span>;
      },
    },
    {
      key: 'usage',
      header: 'Usage',
      hideOnMobile: true,
      accessor: (s) => {
        const stats = s.statistics;
        const pct = stats ? Math.round((stats.used / (stats.total || 1)) * 100) : 0;
        return (
          <div className="flex items-center gap-2">
            <div className="w-24 h-2 bg-surface-700 rounded-full overflow-hidden">
              <div className="h-full bg-surface-500" style={{ width: `${Math.min(pct, 100)}%` }} />
            </div>
            <span className="text-xs text-surface-500">{(stats?.used || 0).toLocaleString()}/{(stats?.total || 0).toLocaleString()}</span>
          </div>
        );
      },
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (s) => s.ready ? (
        <span className="flex items-center gap-1 text-emerald-400 text-xs"><CheckCircle className="h-3.5 w-3.5" /> Ready</span>
      ) : (
        <span className="flex items-center gap-1 text-amber-400 text-xs"><AlertCircle className="h-3.5 w-3.5" /> Pending</span>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-surface-100 flex items-center gap-2">
            <Lock className="h-6 w-6 text-surface-400" />
            System Networks
          </h1>
          <p className="text-surface-400 mt-1">
            Internal Kube-OVN networks managed by the cluster
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="btn-secondary"
          disabled={isLoading}
        >
          <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Info Banner */}
      <div className="card border-amber-500/30 bg-amber-500/5">
        <div className="card-body flex items-start gap-4">
          <div className="rounded-xl p-3 bg-amber-500/10 text-amber-400">
            <Info className="h-6 w-6" />
          </div>
          <div>
            <h3 className="font-semibold text-amber-400 mb-1">Read-Only View</h3>
            <p className="text-surface-400 text-sm">
              System networks are managed automatically by Kube-OVN for internal cluster communication.
              These networks are <strong className="text-surface-200">not routable externally</strong> and
              are used for pod-to-pod communication, node connectivity, and internal services.
            </p>
          </div>
        </div>
      </div>

      {/* Overview Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-surface-700/50 text-surface-400">
              <Layers className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">System Subnets</p>
              <p className="text-2xl font-semibold text-surface-100">
                {systemSubnets.length}
              </p>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-surface-700/50 text-surface-400">
              <Activity className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">IPs Used (Internal)</p>
              <p className="text-2xl font-semibold text-surface-100">
                {totalIpsUsed.toLocaleString()}
              </p>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-body flex items-center gap-4">
            <div className="rounded-xl p-3 bg-surface-700/50 text-surface-400">
              <Server className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm text-surface-400">IPs Available</p>
              <p className="text-2xl font-semibold text-surface-100">
                {totalIpsAvailable.toLocaleString()}
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Subnets Table */}
      <DataTable
        columns={subnetColumns}
        data={systemSubnets}
        loading={isLoading}
        keyExtractor={(s) => s.name}
        actions={(subnet) => [
          { label: 'View Details', icon: <Eye className="h-4 w-4" />, onClick: () => navigate(`/network/subnets/${subnet.name}`) },
        ]}
        onRowClick={(subnet) => navigate(`/network/subnets/${subnet.name}`)}
        emptyState={{
          icon: <Lock className="h-16 w-16" />,
          title: 'No system networks found',
          description: "This cluster doesn't have any detected system networks.",
        }}
      />

      {/* Description of each system network */}
      <div className="card">
        <div className="px-4 py-3 border-b border-surface-700">
          <h3 className="font-medium text-surface-200">Network Descriptions</h3>
        </div>
        <div className="card-body space-y-4">
          <div className="flex gap-4">
            <div className="rounded-lg p-2 bg-surface-800 h-fit">
              <Layers className="h-5 w-5 text-primary-400" />
            </div>
            <div>
              <h4 className="font-medium text-surface-200">ovn-default</h4>
              <p className="text-sm text-surface-400">
                The default network for pods. All pods that don't specify a custom network
                are connected to this subnet. Provides east-west traffic routing between pods.
              </p>
            </div>
          </div>
          
          <div className="flex gap-4">
            <div className="rounded-lg p-2 bg-surface-800 h-fit">
              <Server className="h-5 w-5 text-emerald-400" />
            </div>
            <div>
              <h4 className="font-medium text-surface-200">join</h4>
              <p className="text-sm text-surface-400">
                Node connectivity network. Used for communication between the OVN control plane
                and worker nodes. Critical for cluster networking operations.
              </p>
            </div>
          </div>

          <div className="flex gap-4">
            <div className="rounded-lg p-2 bg-surface-800 h-fit">
              <Lock className="h-5 w-5 text-amber-400" />
            </div>
            <div>
              <h4 className="font-medium text-surface-200">node-local-switch</h4>
              <p className="text-sm text-surface-400">
                Local switch for node-specific networking needs. Handles traffic that stays
                within a single node without crossing the overlay network.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
