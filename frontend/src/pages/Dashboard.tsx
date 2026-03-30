import {
  Server,
  HardDrive,
  Cpu,
  MemoryStick,
  Play,
  Square,
  Plus,
  Download,
  Check,
  Trash,
  Clock,
  ChevronRight,
  Zap,
  RefreshCw,
  Calendar,
} from 'lucide-react';
import { useUserResources, useRecentActivity } from '@/hooks/useNamespaces';
import { Link, useNavigate } from 'react-router-dom';
import { ResourceQuotaWarning } from '@/components/common/ResourceQuotaWarning';
import DashboardMetrics from '@/components/charts/DashboardMetrics';
import { PageTitle } from '@/components/common/PageTitle';

export function Dashboard() {
  const { data: resources, isLoading: resourcesLoading, isFetching: resourcesFetching, refetch: refetchResources } = useUserResources();
  const { data: activity, isLoading: activityLoading, isFetching: activityFetching, refetch: refetchActivity } = useRecentActivity(8);

  const handleRefresh = () => { refetchResources(); refetchActivity(); };
  const navigate = useNavigate();
  const isRefreshing = (resourcesFetching && !resourcesLoading) || (activityFetching && !activityLoading);

  if (resourcesLoading && !resources) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-pulse-slow text-surface-400">Loading...</div>
      </div>
    );
  }

  const getActivityIcon = (icon: string) => {
    const icons: Record<string, typeof Play> = {
      play: Play,
      square: Square,
      plus: Plus,
      trash: Trash,
      download: Download,
      check: Check,
      'hard-drive': HardDrive,
      server: Server,
    };
    return icons[icon] || Server;
  };

  const getActivityColor = (type: string) => {
    const colors: Record<string, string> = {
      vm_started: 'text-emerald-400 bg-emerald-500/10',
      vm_stopped: 'text-amber-400 bg-amber-500/10',
      vm_created: 'text-primary-400 bg-primary-500/10',
      vm_deleted: 'text-red-400 bg-red-500/10',
      image_imported: 'text-emerald-400 bg-emerald-500/10',
      image_importing: 'text-primary-400 bg-primary-500/10',
      storage_event: 'text-surface-400 bg-surface-500/10',
    };
    return colors[type] || 'text-surface-400 bg-surface-500/10';
  };

  const formatTimeAgo = (timestamp: string) => {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);

    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes}m ago`;
    if (hours < 24) return `${hours}h ago`;
    return `${days}d ago`;
  };

  const vmsTotal = resources?.vms_total ?? 0;
  const vmsRunning = resources?.vms_running ?? 0;
  const vmsPercentage = vmsTotal > 0 ? (vmsRunning / vmsTotal) * 100 : 0;

  return (
    <div className="space-y-6">
      <PageTitle title="Dashboard" subtitle="Overview of your virtual infrastructure">
        <button onClick={() => navigate('/vms')} className="btn-primary flex items-center gap-2">
          <Plus className="h-4 w-4" />
          Create VM
        </button>
        <button onClick={handleRefresh} className="btn-secondary" title="Refresh" disabled={isRefreshing}>
          <RefreshCw className={`h-4 w-4 ${isRefreshing ? 'animate-spin' : ''}`} />
        </button>
      </PageTitle>

      {/* Resource Quota Warning */}
      <ResourceQuotaWarning
        cpuUsage={resources?.cpu.percentage}
        memoryUsage={resources?.memory.percentage}
        storageUsage={resources?.storage.percentage}
        maxSchedulableCpu={resources?.max_schedulable?.cpu_cores}
        maxSchedulableMemory={resources?.max_schedulable?.memory_gi}
      />

      {/* KPI Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          icon={Server}
          label="Total VMs"
          value={String(vmsTotal)}
          subLabel={`${vmsRunning} running`}
          percentage={vmsPercentage}
          delay={100}
          onClick={() => navigate('/vms')}
        />
        <KpiCard
          icon={Cpu}
          label="CPU Usage"
          value={`${(resources?.cpu.percentage ?? 0).toFixed(1)}%`}
          subLabel={`${resources?.cpu.used ?? '0'} / ${resources?.cpu.total ?? '0'}`}
          percentage={resources?.cpu.percentage ?? 0}
          delay={200}
        />
        <KpiCard
          icon={MemoryStick}
          label="Memory Usage"
          value={`${(resources?.memory.percentage ?? 0).toFixed(1)}%`}
          subLabel={`${resources?.memory.used ?? '0'} / ${resources?.memory.total ?? '0'}`}
          percentage={resources?.memory.percentage ?? 0}
          delay={300}
        />
        <KpiCard
          icon={HardDrive}
          label="Storage"
          value={`${(resources?.storage.percentage ?? 0).toFixed(1)}%`}
          subLabel={`${resources?.storage.used ?? '0'} / ${resources?.storage.total ?? '0'}`}
          percentage={resources?.storage.percentage ?? 0}
          delay={400}
        />
      </div>

      {/* Cluster Metrics */}
      <DashboardMetrics />

      {/* Activity Feed */}
      <div className="card animate-slide-in" style={{ animationDelay: '500ms' }}>
        <div className="card-header flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Zap className="h-5 w-5 text-primary-400" />
            <h3 className="font-display text-lg font-semibold">Recent Activity</h3>
          </div>
          <Link to="/vms" className="text-sm text-primary-400 hover:text-primary-300 flex items-center gap-1">
            View all <ChevronRight className="h-4 w-4" />
          </Link>
        </div>
        <div className="divide-y divide-surface-800">
          {activityLoading && !activity ? (
            <div className="p-6 text-center text-surface-400">Loading activity...</div>
          ) : activity?.items.length === 0 ? (
            <div className="p-8 text-center">
              <Clock className="h-12 w-12 mx-auto text-surface-600 mb-3" />
              <p className="text-surface-400">No recent activity</p>
              <p className="text-sm text-surface-500 mt-1">Your VM events will appear here</p>
            </div>
          ) : (
            activity?.items.map((item, index) => {
              const Icon = getActivityIcon(item.icon);
              const isVM = item.type.startsWith('vm_');
              const detailPath = isVM
                ? `/vms/${item.resource_namespace}/${item.resource_name}`
                : null;
              return (
                <div
                  key={item.id}
                  className={`flex items-center gap-4 px-6 py-4 hover:bg-surface-800/30 transition-colors ${detailPath ? 'cursor-pointer' : ''}`}
                  style={{ animationDelay: `${600 + index * 50}ms` }}
                  onClick={() => detailPath && navigate(detailPath)}
                >
                  <div className={`rounded-lg p-2 ${getActivityColor(item.type)}`}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-surface-100 truncate">
                      <span className="font-medium">{item.resource_name}</span>
                      <span className="text-surface-500 ml-2">{item.message.split(':')[0]}</span>
                    </p>
                    <p className="text-xs text-surface-500">
                      {item.resource_namespace}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-surface-500 whitespace-nowrap">
                      {formatTimeAgo(item.timestamp)}
                    </span>
                    {detailPath && (
                      <ChevronRight className="h-3.5 w-3.5 text-surface-600" />
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Recent Events */}
      <div className="card animate-slide-in" style={{ animationDelay: '600ms' }}>
        <div className="card-header flex items-center gap-2">
          <Calendar className="h-5 w-5 text-primary-400" />
          <h3 className="font-display text-lg font-semibold">Recent Events</h3>
        </div>
        <div className="p-8 text-center">
          <p className="text-surface-400 text-sm">Recent activity will appear here</p>
        </div>
      </div>
    </div>
  );
}

interface KpiCardProps {
  icon: typeof Server;
  label: string;
  value: string;
  subLabel?: string;
  percentage: number;
  delay: number;
  onClick?: () => void;
}

function KpiCard({ icon: Icon, label, value, subLabel, percentage, delay, onClick }: KpiCardProps) {
  let barColor = 'bg-emerald-500';
  let valueColor = 'text-surface-100';
  if (percentage >= 80) {
    barColor = 'bg-red-500';
    valueColor = 'text-red-400';
  } else if (percentage >= 60) {
    barColor = 'bg-amber-500';
    valueColor = 'text-amber-400';
  }

  return (
    <div
      className={`bg-surface-900 border border-surface-800 rounded-md p-4 animate-slide-in ${onClick ? 'cursor-pointer hover:border-primary-500/50 transition-colors' : ''}`}
      style={{ animationDelay: `${delay}ms` }}
      onClick={onClick}
    >
      <div className="flex items-center gap-2 mb-3">
        <Icon className="h-4 w-4 text-surface-400" />
        <p className="text-sm text-surface-400">{label}</p>
      </div>
      <p className={`text-3xl font-bold mb-1 ${valueColor}`}>{value}</p>
      {subLabel && <p className="text-xs text-surface-400 mb-3">{subLabel}</p>}
      <div className="h-1.5 bg-surface-800 rounded-full overflow-hidden">
        <div
          className={`h-full ${barColor} rounded-full transition-all duration-500`}
          style={{ width: `${Math.min(percentage, 100)}%` }}
        />
      </div>
    </div>
  );
}
