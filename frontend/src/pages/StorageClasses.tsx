/**
 * Storage Classes page - dedicated view for K8s StorageClasses with capacity stats
 */

import { Server, HardDrive, RefreshCw, Database } from 'lucide-react';
import { useStorageClassDetails } from '@/hooks/useStorage';
import type { StorageClassDetail } from '@/types/storage';
import clsx from 'clsx';
import { DataTable, type Column } from '@/components/common/DataTable';

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function CapacityBar({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min((used / total) * 100, 100) : 0;
  const color = pct > 90 ? 'bg-red-500' : pct > 70 ? 'bg-amber-500' : 'bg-primary-500';

  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-surface-400">
        <span>{formatBytes(used)} used</span>
        <span>{formatBytes(total)} total</span>
      </div>
      <div className="h-2 bg-surface-700 rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="text-xs text-surface-500 text-right">{pct.toFixed(1)}% utilized</div>
    </div>
  );
}

export function StorageClasses() {
  const { data: storageClasses, isLoading, refetch } = useStorageClassDetails();

  const classes = storageClasses || [];

  // Aggregate stats
  const totalPVs = classes.reduce((a, c) => a + c.pv_count, 0);
  const totalPVCs = classes.reduce((a, c) => a + c.pvc_count, 0);
  const totalCap = classes.reduce((a, c) => a + c.total_capacity_bytes, 0);
  const totalUsed = classes.reduce((a, c) => a + c.used_capacity_bytes, 0);

  const columns: Column<StorageClassDetail>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (sc) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-surface-100">{sc.name}</span>
          {sc.is_default && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-primary-500/20 text-primary-400">default</span>
          )}
        </div>
      ),
    },
    {
      key: 'provisioner',
      header: 'Provisioner',
      accessor: (sc) => <span className="text-xs font-mono text-surface-400">{sc.provisioner}</span>,
    },
    {
      key: 'reclaim',
      header: 'Reclaim',
      hideOnMobile: true,
      accessor: (sc) => (
        <span className={clsx(
          'text-xs px-1.5 py-0.5 rounded',
          sc.reclaim_policy === 'Delete' ? 'bg-red-500/10 text-red-400' : 'bg-green-500/10 text-green-400'
        )}>{sc.reclaim_policy || '-'}</span>
      ),
    },
    {
      key: 'binding',
      header: 'Binding',
      hideOnMobile: true,
      accessor: (sc) => <span className="text-xs text-surface-400">{sc.volume_binding_mode || '-'}</span>,
    },
    {
      key: 'pvs',
      header: 'PVs',
      hideOnMobile: true,
      accessor: (sc) => <span className="text-sm text-surface-200">{sc.pv_count}</span>,
    },
    {
      key: 'pvcs',
      header: 'PVCs',
      hideOnMobile: true,
      accessor: (sc) => <span className="text-sm text-surface-200">{sc.pvc_count}</span>,
    },
    {
      key: 'capacity',
      header: 'Capacity',
      hideOnMobile: true,
      accessor: (sc) => {
        const pct = sc.total_capacity_bytes > 0
          ? ((sc.used_capacity_bytes / sc.total_capacity_bytes) * 100).toFixed(0)
          : '0';
        return sc.total_capacity_bytes > 0 ? (
          <div className="flex items-center gap-2 min-w-[160px]">
            <div className="flex-1 h-1.5 bg-surface-700 rounded-full overflow-hidden">
              <div
                className={clsx(
                  'h-full rounded-full',
                  Number(pct) > 90 ? 'bg-red-500' : Number(pct) > 70 ? 'bg-amber-500' : 'bg-primary-500'
                )}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="text-xs text-surface-400 w-16 text-right">
              {formatBytes(sc.total_capacity_bytes)}
            </span>
          </div>
        ) : (
          <span className="text-xs text-surface-500">-</span>
        );
      },
    },
    {
      key: 'expand',
      header: 'Expand',
      hideOnMobile: true,
      accessor: (sc) => sc.allow_volume_expansion ? (
        <span className="text-xs text-green-400">Yes</span>
      ) : (
        <span className="text-xs text-surface-500">No</span>
      ),
    },
  ];

  const renderExpandedRow = (sc: StorageClassDetail) => (
    <div className="grid grid-cols-3 gap-6">
      {/* Parameters */}
      <div>
        <h4 className="text-sm font-medium text-surface-200 mb-2">Parameters</h4>
        {Object.keys(sc.parameters).length > 0 ? (
          <div className="space-y-1">
            {Object.entries(sc.parameters).map(([k, v]) => (
              <div key={k} className="flex justify-between text-xs">
                <span className="font-mono text-surface-400">{k}</span>
                <span className="font-mono text-surface-300 ml-4 truncate max-w-[200px]">{v}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-surface-500">No parameters</p>
        )}
      </div>

      {/* Capacity Breakdown */}
      <div>
        <h4 className="text-sm font-medium text-surface-200 mb-2">Capacity</h4>
        <CapacityBar used={sc.used_capacity_bytes} total={sc.total_capacity_bytes} />
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
          <div className="bg-surface-800 rounded p-2">
            <span className="text-surface-500">PVs:</span>
            <span className="text-surface-200 ml-1">{sc.pv_count}</span>
          </div>
          <div className="bg-surface-800 rounded p-2">
            <span className="text-surface-500">PVCs:</span>
            <span className="text-surface-200 ml-1">{sc.pvc_count}</span>
          </div>
        </div>
      </div>

      {/* Info */}
      <div>
        <h4 className="text-sm font-medium text-surface-200 mb-2">Details</h4>
        <div className="space-y-1.5 text-xs">
          <div className="flex justify-between">
            <span className="text-surface-500">Provisioner</span>
            <span className="text-surface-300 font-mono">{sc.provisioner}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-500">Reclaim Policy</span>
            <span className="text-surface-300">{sc.reclaim_policy || 'N/A'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-500">Volume Binding</span>
            <span className="text-surface-300">{sc.volume_binding_mode || 'N/A'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-500">Volume Expansion</span>
            <span className={sc.allow_volume_expansion ? 'text-green-400' : 'text-surface-500'}>
              {sc.allow_volume_expansion ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {sc.created && (
            <div className="flex justify-between">
              <span className="text-surface-500">Created</span>
              <span className="text-surface-300">{new Date(sc.created).toLocaleDateString()}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-surface-100">Storage Classes</h1>
          <p className="text-sm text-surface-400 mt-1">Cluster storage provisioners and capacity overview</p>
        </div>
        <button
          onClick={() => refetch()}
          className="btn-secondary"
        >
          <RefreshCw className="h-4 w-4" />
          Refresh
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-surface-800/50 border border-surface-700 rounded-lg p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-primary-500/10">
              <Server className="h-5 w-5 text-primary-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{classes.length}</p>
              <p className="text-sm text-surface-400">Storage Classes</p>
            </div>
          </div>
        </div>
        <div className="bg-surface-800/50 border border-surface-700 rounded-lg p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-blue-500/10">
              <HardDrive className="h-5 w-5 text-blue-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{totalPVs}</p>
              <p className="text-sm text-surface-400">Persistent Volumes</p>
            </div>
          </div>
        </div>
        <div className="bg-surface-800/50 border border-surface-700 rounded-lg p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-amber-500/10">
              <Database className="h-5 w-5 text-amber-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-surface-100">{totalPVCs}</p>
              <p className="text-sm text-surface-400">Claims (PVCs)</p>
            </div>
          </div>
        </div>
        <div className="bg-surface-800/50 border border-surface-700 rounded-lg p-4">
          <div className="space-y-2">
            <p className="text-sm text-surface-400">Total Capacity</p>
            <CapacityBar used={totalUsed} total={totalCap} />
          </div>
        </div>
      </div>

      {/* Table */}
      <DataTable
        columns={columns}
        data={classes}
        loading={isLoading}
        keyExtractor={(sc) => sc.name}
        expandable={renderExpandedRow}
        emptyState={{
          icon: <Server className="h-16 w-16" />,
          title: 'No storage classes found',
          description: 'Storage classes will appear when a CSI driver is installed.',
        }}
      />
    </div>
  );
}
