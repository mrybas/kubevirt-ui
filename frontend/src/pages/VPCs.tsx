/**
 * VPC List Page
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Network,
  RefreshCw,
  Trash2,
  Eye,
  CheckCircle,
  AlertCircle,
} from 'lucide-react';
import { useVpcs, useDeleteVpc } from '../hooks/useVpcs';
import type { Vpc } from '../types/vpc';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

export default function VPCs() {
  const navigate = useNavigate();
  const [showDeleteModal, setShowDeleteModal] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const { data, isLoading, refetch } = useVpcs();
  const deleteVpc = useDeleteVpc();

  const items = data?.items ?? [];
  const filtered = searchQuery
    ? items.filter((v) => v.name.toLowerCase().includes(searchQuery.toLowerCase()))
    : items;

  const handleDelete = async (name: string) => {
    await deleteVpc.mutateAsync(name);
    setShowDeleteModal(null);
  };

  const columns: Column<Vpc>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (vpc) => (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-surface-100">{vpc.name}</span>
          {vpc.folder && (
            <span className="px-1.5 py-0.5 text-[11px] font-medium rounded bg-primary-500/10 text-primary-400">
              {vpc.folder}{vpc.environment ? `/${vpc.environment}` : ''}
            </span>
          )}
        </div>
      ),
    },
    {
      key: 'cidrs',
      header: 'Subnets',
      hideOnMobile: true,
      accessor: (vpc) => (
        <div className="flex items-center gap-2 flex-wrap">
          {(vpc.subnets ?? []).map((s) => (
            <span key={s.name} className="text-xs font-mono text-surface-400">{s.cidr_block}</span>
          ))}
        </div>
      ),
    },
    {
      key: 'peerings',
      header: 'Peerings',
      hideOnMobile: true,
      accessor: (vpc) => <span>{(vpc.peerings ?? []).length}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (vpc) => (
        <span className={`flex items-center gap-1 text-xs ${vpc.ready ? 'text-emerald-400' : 'text-amber-400'}`}>
          {vpc.ready ? <CheckCircle className="h-3.5 w-3.5" /> : <AlertCircle className="h-3.5 w-3.5" />}
          {vpc.ready ? 'Ready' : 'Pending'}
        </span>
      ),
    },
  ];

  const getActions = (vpc: Vpc): MenuItem[] => [
    { label: 'View Details', icon: <Eye className="h-4 w-4" />, onClick: () => navigate(`/network/vpcs/${vpc.name}`) },
    { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setShowDeleteModal(vpc.name), variant: 'danger' },
  ];

  return (
    <div className="space-y-6">
      <ActionBar
        title="VPCs"
        subtitle="Virtual Private Clouds — isolated L3 networks with custom subnets"
      >
        <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filtered}
        loading={isLoading}
        keyExtractor={(vpc) => vpc.name}
        actions={getActions}
        onRowClick={(vpc) => navigate(`/network/vpcs/${vpc.name}`)}
        searchable
        searchPlaceholder="Search VPCs..."
        onSearch={setSearchQuery}
        expandable={(vpc) => (
          <div className="px-4 py-3 bg-surface-900/50">
            <div className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">Subnets</div>
            {(vpc.subnets ?? []).length === 0 ? (
              <p className="text-sm text-surface-500">No subnets</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-surface-400">
                    <th className="text-left py-1 pr-4">Name</th>
                    <th className="text-left py-1 pr-4">CIDR</th>
                    <th className="text-left py-1 pr-4">Gateway</th>
                    <th className="text-left py-1 pr-4">Available IPs</th>
                    <th className="text-left py-1">Used IPs</th>
                  </tr>
                </thead>
                <tbody>
                  {vpc.subnets.map(sub => (
                    <tr key={sub.name} className="border-t border-surface-800">
                      <td className="py-1.5 pr-4 font-mono">{sub.name}</td>
                      <td className="py-1.5 pr-4 font-mono">{sub.cidr_block}</td>
                      <td className="py-1.5 pr-4 font-mono">{sub.gateway}</td>
                      <td className="py-1.5 pr-4">{sub.available_ips}</td>
                      <td className="py-1.5">{sub.used_ips}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
        emptyState={{
          icon: <Network className="h-16 w-16" />,
          title: 'No VPCs yet',
          description: 'Use the Create button at the top of the Networks page.',
        }}
      />

      {showDeleteModal && (
        <DeleteVpcModal
          vpcName={showDeleteModal}
          onConfirm={() => handleDelete(showDeleteModal)}
          onCancel={() => setShowDeleteModal(null)}
          isDeleting={deleteVpc.isPending}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeleteVpcModal
// ---------------------------------------------------------------------------

function DeleteVpcModal({
  vpcName,
  onConfirm,
  onCancel,
  isDeleting,
}: {
  vpcName: string;
  onConfirm: () => void;
  onCancel: () => void;
  isDeleting: boolean;
}) {
  const [confirmName, setConfirmName] = useState('');

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl p-5">
        <div className="w-12 h-12 bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <Trash2 className="w-6 h-6 text-red-400" />
        </div>
        <h2 className="text-lg font-semibold text-surface-100 text-center mb-2">Delete VPC</h2>
        <p className="text-sm text-surface-400 text-center mb-4">
          This will delete <strong>{vpcName}</strong> and all associated resources. Cannot be undone.
        </p>
        <div className="mb-4">
          <label className="block text-sm text-surface-400 mb-1">
            Type <strong>{vpcName}</strong> to confirm:
          </label>
          <input
            type="text"
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
            placeholder={vpcName}
            className="input w-full focus:border-red-500"
          />
        </div>
        <div className="flex gap-3">
          <button onClick={onCancel} className="flex-1 btn-secondary">Cancel</button>
          <button
            onClick={onConfirm}
            disabled={confirmName !== vpcName || isDeleting}
            className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
          >
            {isDeleting ? 'Deleting...' : 'Delete VPC'}
          </button>
        </div>
      </div>
    </div>
  );
}
