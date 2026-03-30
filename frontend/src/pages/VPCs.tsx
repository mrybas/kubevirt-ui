/**
 * VPC List Page
 */

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Plus,
  Network,
  RefreshCw,
  Trash2,
  X,
  Eye,
  CheckCircle,
  AlertCircle,
  Check,
  AlertTriangle,
} from 'lucide-react';
import { useVpcs, useCreateVpc, useDeleteVpc } from '../hooks/useVpcs';
import type { Vpc } from '../types/vpc';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

export default function VPCs({
  openCreate,
  onCreateOpened,
}: {
  openCreate?: boolean;
  onCreateOpened?: () => void;
} = {}) {
  const navigate = useNavigate();
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  // Open create modal when parent signals
  useEffect(() => {
    if (openCreate) {
      setShowCreateModal(true);
      onCreateOpened?.();
    }
  }, [openCreate]);

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
        <span className="font-medium text-surface-100">{vpc.name}</span>
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
        <button onClick={() => setShowCreateModal(true)} className="btn-primary">
          <Plus className="w-4 h-4" />
          Create VPC
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
          description: 'Create a VPC to set up isolated L3 networking.',
          action: (
            <button onClick={() => setShowCreateModal(true)} className="btn-primary">
              <Plus className="w-4 h-4" />
              Create your first VPC
            </button>
          ),
        }}
      />

      {showCreateModal && (
        <CreateVpcModal onClose={() => setShowCreateModal(false)} />
      )}

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
// CreateVpcModal — simple one-step form
// ---------------------------------------------------------------------------

function CreateVpcModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('');
  const [cidr, setCidr] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState('');
  const createVpc = useCreateVpc();

  const canCreate = name.length > 0;

  const handleCreate = async () => {
    setIsCreating(true);
    setError('');
    try {
      await createVpc.mutateAsync({
        name,
        ...(cidr ? { subnet_cidr: cidr } : {}),
      });
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create VPC');
      setIsCreating(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between p-5 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">Create VPC</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-5 space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Name *</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
              placeholder="my-vpc"
              className="input w-full font-mono text-sm"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Subnet CIDR</label>
            <input
              type="text"
              value={cidr}
              onChange={(e) => setCidr(e.target.value)}
              placeholder="10.0.0.0/24"
              className="input w-full font-mono text-sm"
            />
            <p className="text-xs text-surface-500 mt-1">
              CIDR for the default subnet. Leave empty to auto-allocate.
            </p>
          </div>

          {error && (
            <div className="flex items-center gap-2 text-sm text-red-400 bg-red-900/20 border border-red-800/30 rounded-lg p-3">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 p-5 border-t border-surface-700">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="button"
            onClick={handleCreate}
            disabled={!canCreate || isCreating}
            className="btn-primary flex items-center gap-1.5"
          >
            {isCreating ? 'Creating...' : (
              <>
                <Check className="w-4 h-4" />
                Create VPC
              </>
            )}
          </button>
        </div>
      </div>
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
