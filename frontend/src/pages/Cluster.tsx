import { useClusterStatus, useNodes, useNamespaces, useUserResources, useClusterSettings, useUpdateClusterSettings } from '@/hooks/useNamespaces';
import { Server, Box, Layers, CheckCircle, XCircle, RefreshCw, Cpu, MemoryStick, HardDrive, Settings } from 'lucide-react';
import ClusterMetrics from '@/components/charts/ClusterMetrics';
import { ResourceQuotaWarning } from '@/components/common/ResourceQuotaWarning';

function ResourceGauge({ label, icon: Icon, used, total, percentage }: {
  label: string;
  icon: typeof Cpu;
  used: string;
  total: string;
  percentage: number;
}) {
  const radius = 54;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (percentage / 100) * circumference;

  // Color logic
  let strokeColor = 'stroke-emerald-500';
  let textColor = 'text-emerald-400';
  let bgGlow = 'shadow-emerald-500/10';
  if (percentage >= 80) {
    strokeColor = 'stroke-red-500';
    textColor = 'text-red-400';
    bgGlow = 'shadow-red-500/10';
  } else if (percentage >= 60) {
    strokeColor = 'stroke-amber-500';
    textColor = 'text-amber-400';
    bgGlow = 'shadow-amber-500/10';
  }

  return (
    <div className={`card hover:shadow-lg ${bgGlow} transition-shadow`}>
      <div className="card-body flex flex-col items-center py-6">
        <div className="relative w-32 h-32 mb-4">
          <svg className="w-full h-full -rotate-90" viewBox="0 0 128 128">
            <circle cx="64" cy="64" r={radius} fill="none" stroke="currentColor"
              className="text-surface-800" strokeWidth="10" />
            <circle cx="64" cy="64" r={radius} fill="none"
              className={strokeColor} strokeWidth="10" strokeLinecap="round"
              strokeDasharray={circumference} strokeDashoffset={offset}
              style={{ transition: 'stroke-dashoffset 0.8s ease-out' }} />
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className={`text-2xl font-bold ${textColor}`}>
              {percentage.toFixed(0)}%
            </span>
            <span className="text-xs text-surface-500">used</span>
          </div>
        </div>
        <div className="flex items-center gap-2 mb-1">
          <Icon className={`h-4 w-4 ${textColor}`} />
          <span className="font-semibold text-surface-200">{label}</span>
        </div>
        <span className="text-sm text-surface-400">
          {used} / {total}
        </span>
      </div>
    </div>
  );
}

export function Cluster() {
  const { data: status, isLoading: statusLoading, refetch: refetchStatus } = useClusterStatus();
  const { data: nodes, isLoading: nodesLoading, refetch: refetchNodes } = useNodes();
  const { data: namespaces, isLoading: nsLoading, refetch: refetchNs } = useNamespaces();
  const { data: resources, refetch: refetchResources } = useUserResources();
  const { data: clusterSettings } = useClusterSettings();
  const updateSettings = useUpdateClusterSettings();

  const handleRefresh = () => { refetchStatus(); refetchNodes(); refetchNs(); refetchResources(); };

  const isLoading = statusLoading || nodesLoading || nsLoading;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-pulse-slow text-surface-400">Loading cluster info...</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-surface-100">Cluster</h1>
          <p className="text-surface-400 mt-1">Cluster status, nodes, and components</p>
        </div>
        <button onClick={handleRefresh} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
      </div>

      {/* Resource Warning */}
      <ResourceQuotaWarning
        cpuUsage={resources?.cpu.percentage}
        memoryUsage={resources?.memory.percentage}
        storageUsage={resources?.storage.percentage}
        maxSchedulableCpu={resources?.max_schedulable?.cpu_cores}
        maxSchedulableMemory={resources?.max_schedulable?.memory_gi}
      />

      {/* Cluster Settings */}
      <div className="card">
        <div className="card-header">
          <h3 className="font-display text-lg font-semibold flex items-center gap-2">
            <Settings className="h-5 w-5 text-primary-400" />
            Cluster Settings
          </h3>
        </div>
        <div className="card-body">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-surface-200">CPU Overcommit Ratio</p>
              <p className="text-xs text-surface-400 mt-0.5">
                Allows scheduling more VMs than physical CPU cores. VMs share CPU time when overcommitted.
              </p>
            </div>
            <div className="flex items-center gap-2">
              {[1, 2, 4, 8].map((ratio) => (
                <button
                  key={ratio}
                  onClick={() => updateSettings.mutate({ cpu_overcommit: ratio })}
                  className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${
                    clusterSettings?.cpu_overcommit === ratio
                      ? 'bg-primary-500/20 border-primary-500/50 text-primary-300'
                      : 'bg-surface-800 border-surface-700 text-surface-400 hover:border-surface-500'
                  }`}
                >
                  {ratio === 1 ? '1:1' : `${ratio}:1`}
                </button>
              ))}
              {updateSettings.isPending && (
                <span className="text-xs text-surface-500">Saving...</span>
              )}
            </div>
          </div>
          {clusterSettings && clusterSettings.cpu_overcommit > 1 && (
            <div className="mt-3 px-3 py-2 rounded bg-amber-500/10 border border-amber-500/20 text-xs text-amber-300">
              VMs will request {clusterSettings.cpu_overcommit}x less CPU from scheduler. Example: a 2 vCPU VM will only reserve {(2 / clusterSettings.cpu_overcommit).toFixed(1)} cores.
            </div>
          )}
        </div>
      </div>

      {/* Components Status */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* KubeVirt */}
        <div className="card">
          <div className="card-header">
            <h3 className="font-display text-lg font-semibold flex items-center gap-2">
              <Box className="h-5 w-5 text-primary-400" />
              KubeVirt
            </h3>
          </div>
          <div className="card-body">
            <div className="flex items-center justify-between mb-4">
              <span className="text-surface-400">Status</span>
              {status?.kubevirt.installed ? (
                <span className="badge-success flex items-center gap-1">
                  <CheckCircle className="h-3 w-3" />
                  {status.kubevirt.phase}
                </span>
              ) : (
                <span className="badge-error flex items-center gap-1">
                  <XCircle className="h-3 w-3" />
                  Not Installed
                </span>
              )}
            </div>
            {status?.kubevirt.version && (
              <div className="flex items-center justify-between">
                <span className="text-surface-400">Version</span>
                <span className="text-surface-100 font-mono text-sm">
                  {status.kubevirt.version}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* CDI */}
        <div className="card">
          <div className="card-header">
            <h3 className="font-display text-lg font-semibold flex items-center gap-2">
              <Layers className="h-5 w-5 text-emerald-400" />
              CDI (Containerized Data Importer)
            </h3>
          </div>
          <div className="card-body">
            <div className="flex items-center justify-between">
              <span className="text-surface-400">Status</span>
              {status?.cdi.installed ? (
                <span className="badge-success flex items-center gap-1">
                  <CheckCircle className="h-3 w-3" />
                  {status.cdi.phase}
                </span>
              ) : (
                <span className="badge-error flex items-center gap-1">
                  <XCircle className="h-3 w-3" />
                  Not Installed
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Resource Utilization */}
      {resources && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <ResourceGauge
            label="CPU Requests"
            icon={Cpu}
            used={resources.cpu.used}
            total={`${resources.cpu.total} cores`}
            percentage={resources.cpu.percentage}
          />
          <ResourceGauge
            label="Memory Requests"
            icon={MemoryStick}
            used={resources.memory.used}
            total={resources.memory.total}
            percentage={resources.memory.percentage}
          />
          <ResourceGauge
            label="Storage"
            icon={HardDrive}
            used={resources.storage.used}
            total={resources.storage.total}
            percentage={resources.storage.percentage}
          />
        </div>
      )}

      {/* Nodes */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h3 className="font-display text-lg font-semibold flex items-center gap-2">
            <Server className="h-5 w-5" />
            Nodes ({nodes?.total ?? 0})
          </h3>
          <span className="badge-success">
            {status?.nodes_ready} / {status?.nodes_count} Ready
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Roles</th>
                <th>CPU Usage</th>
                <th>Memory Usage</th>
                <th>IP</th>
              </tr>
            </thead>
            <tbody>
              {nodes?.items.map((node) => (
                <tr key={node.name}>
                  <td>
                    <div className="font-medium text-surface-100">{node.name}</div>
                    <div className="text-xs text-surface-500">{node.version}</div>
                  </td>
                  <td>
                    <span
                      className={
                        node.status === 'Ready' ? 'badge-success' : 'badge-error'
                      }
                    >
                      {node.status}
                    </span>
                  </td>
                  <td>
                    <div className="flex gap-1 flex-wrap">
                      {node.roles.map((role) => (
                        <span key={role} className="badge-neutral text-xs">
                          {role}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="min-w-[160px]">
                    {node.cpu_usage ? (
                      <div>
                        <div className="flex justify-between text-xs mb-1">
                          <span className="text-surface-300">{node.cpu_usage.used} / {node.cpu_usage.total} cores</span>
                          <span className={node.cpu_usage.percentage > 80 ? 'text-red-400' : 'text-surface-400'}>{node.cpu_usage.percentage}%</span>
                        </div>
                        <div className="w-full h-2 bg-surface-700 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all ${
                              node.cpu_usage.percentage > 90 ? 'bg-red-500' :
                              node.cpu_usage.percentage > 70 ? 'bg-amber-500' : 'bg-primary-500'
                            }`}
                            style={{ width: `${Math.min(node.cpu_usage.percentage, 100)}%` }}
                          />
                        </div>
                        <div className="text-xs text-surface-500 mt-0.5">{node.cpu_usage.free} free</div>
                      </div>
                    ) : (
                      <span className="text-surface-500">{node.cpu}</span>
                    )}
                  </td>
                  <td className="min-w-[160px]">
                    {node.memory_usage ? (
                      <div>
                        <div className="flex justify-between text-xs mb-1">
                          <span className="text-surface-300">{node.memory_usage.used} / {node.memory_usage.total} Gi</span>
                          <span className={node.memory_usage.percentage > 80 ? 'text-red-400' : 'text-surface-400'}>{node.memory_usage.percentage}%</span>
                        </div>
                        <div className="w-full h-2 bg-surface-700 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all ${
                              node.memory_usage.percentage > 90 ? 'bg-red-500' :
                              node.memory_usage.percentage > 70 ? 'bg-amber-500' : 'bg-primary-500'
                            }`}
                            style={{ width: `${Math.min(node.memory_usage.percentage, 100)}%` }}
                          />
                        </div>
                        <div className="text-xs text-surface-500 mt-0.5">{node.memory_usage.free} Gi free</div>
                      </div>
                    ) : (
                      <span className="text-surface-500">{node.memory}</span>
                    )}
                  </td>
                  <td className="text-surface-400 font-mono text-sm">
                    {node.internal_ip}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Node Metrics */}
      <ClusterMetrics nodeNames={nodes?.items.map((n) => n.name) ?? []} />

      {/* Namespaces */}
      <div className="card">
        <div className="card-header">
          <h3 className="font-display text-lg font-semibold">
            Namespaces ({namespaces?.total ?? 0})
          </h3>
        </div>
        <div className="card-body">
          <div className="flex flex-wrap gap-2">
            {namespaces?.items.map((ns) => (
              <span
                key={ns.name}
                className={`badge ${
                  ns.status === 'Active' ? 'badge-success' : 'badge-warning'
                }`}
              >
                {ns.name}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
