import { useState, useMemo } from 'react';
import { RefreshCw, Loader2, BarChart3 } from 'lucide-react';
import UPlotChart from './UPlotChart';
import { useMetricsRange, useMetricsStatus, type TimeRange } from '@/hooks/useMetrics';
import {
  buildChartOptions,
  fmtPercent,

  seriesLabel,
} from './chartUtils';

const TIME_RANGES: { label: string; value: TimeRange }[] = [
  { label: '1H', value: '1h' },
  { label: '6H', value: '6h' },
  { label: '24H', value: '24h' },
  { label: '7D', value: '7d' },
];

interface ClusterMetricsProps {
  nodeNames: string[];
}

export default function ClusterMetrics({ nodeNames }: ClusterMetricsProps) {
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const { data: metricsStatus } = useMetricsStatus();

  const enabled = metricsStatus?.available === true && nodeNames.length > 0;

  // Per-node CPU usage (group by instance since 'node' label is empty)
  const cpuPerNodeQuery = `100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`;
  const cpuData = useMetricsRange(cpuPerNodeQuery, timeRange, enabled);

  // Per-node memory usage percentage
  const memPerNodeQuery = `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)`;
  const memData = useMetricsRange(memPerNodeQuery, timeRange, enabled);

  // Per-node disk usage (/var is the data partition on Talos/immutable OS)
  const diskPerNodeQuery = `100 - (node_filesystem_avail_bytes{mountpoint="/var"} / node_filesystem_size_bytes{mountpoint="/var"} * 100)`;
  const diskData = useMetricsRange(diskPerNodeQuery, timeRange, enabled);

  const isLoading = cpuData.isLoading;
  const isRefreshing = cpuData.isFetching;

  // Build per-node CPU chart
  const cpuChartData = useMemo(() => {
    const result = cpuData.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [cpuData.data]);

  const cpuLabels = useMemo(
    () => (cpuData.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'instance')),
    [cpuData.data],
  );

  const cpuOpts = useMemo(
    () => buildChartOptions({ labels: cpuLabels.length ? cpuLabels : ['CPU'], yFormatter: fmtPercent, height: 220 }),
    [cpuLabels],
  );

  // Build per-node memory chart
  const memChartData = useMemo(() => {
    const result = memData.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [memData.data]);

  const memLabels = useMemo(
    () => (memData.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'node', ['instance'])),
    [memData.data],
  );

  const memOpts = useMemo(
    () => buildChartOptions({ labels: memLabels.length ? memLabels : ['Memory'], yFormatter: fmtPercent, height: 220, colors: ['#a78bfa', '#22d3ee', '#34d399', '#f97316'] }),
    [memLabels],
  );

  // Build per-node disk chart
  const diskChartData = useMemo(() => {
    const result = diskData.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [diskData.data]);

  const diskLabels = useMemo(
    () => (diskData.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'node', ['instance'])),
    [diskData.data],
  );

  const diskOpts = useMemo(
    () => buildChartOptions({ labels: diskLabels.length ? diskLabels : ['Disk'], yFormatter: fmtPercent, height: 220, colors: ['#34d399', '#f97316', '#60a5fa', '#f472b6'] }),
    [diskLabels],
  );

  const handleRefresh = () => {
    cpuData.refetch();
    memData.refetch();
    diskData.refetch();
  };

  if (!metricsStatus?.available) return null;

  return (
    <div className="card">
      <div className="card-header flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-primary-400" />
          <h3 className="font-display text-lg font-semibold">Node Metrics</h3>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-0.5 bg-surface-900 border border-surface-700 rounded-lg p-0.5">
            {TIME_RANGES.map((r) => (
              <button
                key={r.value}
                onClick={() => setTimeRange(r.value)}
                className={`px-2 py-1 text-xs font-medium rounded-md transition-all ${
                  timeRange === r.value
                    ? 'bg-primary-500/20 text-primary-300'
                    : 'text-surface-400 hover:text-surface-200'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
          {isRefreshing && <Loader2 className="h-3.5 w-3.5 animate-spin text-surface-400" />}
          <button onClick={handleRefresh} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg" disabled={isRefreshing}>
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-5 w-5 animate-spin text-primary-400 mr-2" />
          <span className="text-surface-400 text-sm">Loading node metrics...</span>
        </div>
      ) : (
        <div className="p-4 grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
            <h4 className="text-xs font-medium text-surface-400 mb-2">CPU Usage per Node (%)</h4>
            <UPlotChart options={cpuOpts} data={cpuChartData as any} />
          </div>
          <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
            <h4 className="text-xs font-medium text-surface-400 mb-2">Memory Usage per Node (%)</h4>
            <UPlotChart options={memOpts} data={memChartData as any} />
          </div>
          <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
            <h4 className="text-xs font-medium text-surface-400 mb-2">Disk Usage per Node (%)</h4>
            <UPlotChart options={diskOpts} data={diskChartData as any} />
          </div>
        </div>
      )}
    </div>
  );
}
