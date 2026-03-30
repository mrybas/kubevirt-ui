import { useState, useMemo } from 'react';
import { RefreshCw, Loader2, BarChart3, AlertTriangle } from 'lucide-react';
import UPlotChart from './UPlotChart';
import { useMetricsInstant, useMetricsRange, useMetricsStatus, RANGE_SECONDS, type TimeRange } from '@/hooks/useMetrics';
import {
  promRangeToUPlot,
  buildChartOptions,
  fmtPercent,
  fmtBytes,

} from './chartUtils';

const TIME_RANGES: { label: string; value: TimeRange }[] = [
  { label: '1H', value: '1h' },
  { label: '6H', value: '6h' },
  { label: '24H', value: '24h' },
];

export default function DashboardMetrics() {
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const { data: metricsStatus } = useMetricsStatus();

  const enabled = metricsStatus?.available === true;

  // Top VMs by CPU (instant)
  const topCpuQuery = 'topk(5, rate(kubevirt_vmi_cpu_usage_seconds_total[5m]) * 100)';
  const topCpu = useMetricsInstant(topCpuQuery, enabled);

  // Top VMs by Memory (instant)
  const topMemQuery = 'topk(5, kubevirt_vmi_memory_domain_bytes - kubevirt_vmi_memory_unused_bytes)';
  const topMem = useMetricsInstant(topMemQuery, enabled);

  // Cluster CPU usage over time (sum across all nodes)
  const clusterCpuQuery = '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)';
  const clusterCpu = useMetricsRange(clusterCpuQuery, timeRange, enabled);

  // Cluster memory usage over time
  const clusterMemUsedQuery = 'sum(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)';
  const clusterMemTotalQuery = 'sum(node_memory_MemTotal_bytes)';
  const clusterMemUsed = useMetricsRange(clusterMemUsedQuery, timeRange, enabled);
  const clusterMemTotal = useMetricsRange(clusterMemTotalQuery, timeRange, enabled);

  const isLoading = topCpu.isLoading || clusterCpu.isLoading;
  const isRefreshing = topCpu.isFetching || clusterCpu.isFetching;

  // Compute time window boundaries for X-axis pinning
  const xMax = useMemo(() => Math.floor(Date.now() / 1000), [timeRange]);
  const xMin = xMax - RANGE_SECONDS[timeRange];

  // Top CPU bar data
  const topCpuItems = useMemo(() => {
    const result = topCpu.data?.data?.result ?? [];
    return result
      .map((s) => ({
        name: s.metric.name || 'unknown',
        namespace: s.metric.exported_namespace || '',
        value: parseFloat(s.value?.[1] ?? '0'),
      }))
      .sort((a, b) => b.value - a.value);
  }, [topCpu.data]);

  // Top Memory bar data
  const topMemItems = useMemo(() => {
    const result = topMem.data?.data?.result ?? [];
    return result
      .map((s) => ({
        name: s.metric.name || 'unknown',
        namespace: s.metric.exported_namespace || '',
        value: parseFloat(s.value?.[1] ?? '0'),
      }))
      .sort((a, b) => b.value - a.value);
  }, [topMem.data]);

  // Cluster CPU chart
  const clusterCpuData = useMemo(() => promRangeToUPlot(clusterCpu.data), [clusterCpu.data]);
  const clusterCpuOpts = useMemo(
    () => buildChartOptions({ labels: ['Cluster CPU %'], yFormatter: fmtPercent, fill: true, colors: ['#6366f1'], height: 180, xMin, xMax }),
    [xMin, xMax],
  );

  // Cluster Memory chart
  const clusterMemData = useMemo(() => {
    const used = clusterMemUsed.data?.data?.result?.[0]?.values ?? [];
    const total = clusterMemTotal.data?.data?.result?.[0]?.values ?? [];
    if (!used.length) return [[], []];
    const ts = new Float64Array(used.map((v: [number, string]) => v[0]));
    return [
      ts,
      new Float64Array(used.map((v: [number, string]) => parseFloat(v[1]) || 0)),
      new Float64Array(total.map((v: [number, string]) => parseFloat(v[1]) || 0)),
    ];
  }, [clusterMemUsed.data, clusterMemTotal.data]);

  const clusterMemOpts = useMemo(
    () => buildChartOptions({ labels: ['Used', 'Total'], yFormatter: fmtBytes, fill: true, colors: ['#a78bfa', '#6366f1'], height: 180, xMin, xMax }),
    [xMin, xMax],
  );

  const handleRefresh = () => {
    topCpu.refetch();
    topMem.refetch();
    clusterCpu.refetch();
    clusterMemUsed.refetch();
    clusterMemTotal.refetch();
  };

  if (!metricsStatus?.available) {
    return null; // Silently hide on dashboard if no metrics backend
  }

  return (
    <div className="card animate-slide-in" style={{ animationDelay: '450ms' }}>
      <div className="card-header flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-primary-400" />
          <h3 className="font-display text-lg font-semibold">Cluster Metrics</h3>
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
          <span className="text-surface-400 text-sm">Loading metrics...</span>
        </div>
      ) : (
        <div className="p-4 space-y-4">
          {/* Top row: bar charts */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <TopBarChart title="Top VMs by CPU" items={topCpuItems} formatter={(v) => fmtPercent(v)} colorKey="cpu" />
            <TopBarChart title="Top VMs by Memory" items={topMemItems} formatter={(v) => fmtBytes(v)} colorKey="mem" />
          </div>

          {/* Bottom row: time series */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
              <h4 className="text-xs font-medium text-surface-400 mb-2">Cluster CPU Usage</h4>
              <UPlotChart options={clusterCpuOpts} data={clusterCpuData as any} />
            </div>
            <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
              <h4 className="text-xs font-medium text-surface-400 mb-2">Cluster Memory Usage</h4>
              <UPlotChart options={clusterMemOpts} data={clusterMemData as any} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function TopBarChart({
  title,
  items,
  formatter,
  colorKey,
}: {
  title: string;
  items: { name: string; namespace: string; value: number }[];
  formatter: (v: number) => string;
  colorKey: 'cpu' | 'mem';
}) {
  const maxValue = Math.max(...items.map((i) => i.value), 1);
  const colors = colorKey === 'cpu'
    ? ['#6366f1', '#818cf8', '#a5b4fc', '#c7d2fe', '#e0e7ff']
    : ['#a78bfa', '#c4b5fd', '#ddd6fe', '#ede9fe', '#f5f3ff'];

  return (
    <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
      <h4 className="text-xs font-medium text-surface-400 mb-3">{title}</h4>
      {items.length === 0 ? (
        <div className="flex items-center gap-2 text-surface-500 text-xs py-4">
          <AlertTriangle className="h-3.5 w-3.5" />
          No running VMs with metrics
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((item, i) => (
            <div key={`${item.namespace}/${item.name}`} className="flex items-center gap-3">
              <span className="text-xs text-surface-300 w-24 truncate" title={`${item.namespace}/${item.name}`}>
                {item.name}
              </span>
              <div className="flex-1 h-5 bg-surface-800 rounded-md overflow-hidden">
                <div
                  className="h-full rounded-md transition-all duration-500"
                  style={{
                    width: `${(item.value / maxValue) * 100}%`,
                    backgroundColor: colors[i % colors.length],
                  }}
                />
              </div>
              <span className="text-xs text-surface-400 w-16 text-right font-mono">
                {formatter(item.value)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
