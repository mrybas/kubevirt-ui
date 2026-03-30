import { useState, useMemo } from 'react';
import { RefreshCw, Clock, AlertTriangle, Loader2 } from 'lucide-react';
import UPlotChart from './UPlotChart';
import { useMetricsRange, useMetricsStatus, RANGE_SECONDS, type TimeRange } from '@/hooks/useMetrics';
import {
  promRangeToUPlot,
  cpuChartOptions,
  memoryChartOptions,
  diskIOChartOptions,
  networkIOChartOptions,
  seriesLabel,
} from './chartUtils';

const TIME_RANGES: { label: string; value: TimeRange }[] = [
  { label: '1H', value: '1h' },
  { label: '6H', value: '6h' },
  { label: '24H', value: '24h' },
  { label: '7D', value: '7d' },
];

interface VMMetricsPanelProps {
  vmName: string;
  namespace: string;
}

export default function VMMetricsPanel({ vmName, namespace }: VMMetricsPanelProps) {
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const [refreshCounter, setRefreshCounter] = useState(0);
  const { data: metricsStatus } = useMetricsStatus();

  const enabled = metricsStatus?.available === true;

  const escapePromQL = (s: string) => s.replace(/[\\\"{}]/g, '\\$&');

  // PromQL queries scoped to this VM
  const cpuQuery = `rate(kubevirt_vmi_cpu_usage_seconds_total{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}[2m]) * 100`;
  const memUsedQuery = `kubevirt_vmi_memory_domain_bytes{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"} - kubevirt_vmi_memory_unused_bytes{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}`;
  const memTotalQuery = `kubevirt_vmi_memory_domain_bytes{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}`;
  const diskReadQuery = `sum(rate(kubevirt_vmi_storage_read_traffic_bytes_total{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}[2m]))`;
  const diskWriteQuery = `sum(rate(kubevirt_vmi_storage_write_traffic_bytes_total{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}[2m]))`;
  const netRxQuery = `rate(kubevirt_vmi_network_receive_bytes_total{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}[2m])`;
  const netTxQuery = `rate(kubevirt_vmi_network_transmit_bytes_total{name="${escapePromQL(vmName)}", exported_namespace="${escapePromQL(namespace)}"}[2m])`;

  // Fetch all metrics
  const cpu = useMetricsRange(cpuQuery, timeRange, enabled);
  const memUsed = useMetricsRange(memUsedQuery, timeRange, enabled);
  const memTotal = useMetricsRange(memTotalQuery, timeRange, enabled);
  const diskRead = useMetricsRange(diskReadQuery, timeRange, enabled);
  const diskWrite = useMetricsRange(diskWriteQuery, timeRange, enabled);
  const netRx = useMetricsRange(netRxQuery, timeRange, enabled);
  const netTx = useMetricsRange(netTxQuery, timeRange, enabled);

  const isLoading = cpu.isLoading || memUsed.isLoading;
  const isRefreshing = cpu.isFetching || memUsed.isFetching;

  // Compute time window boundaries for X-axis pinning
  const xMax = useMemo(() => Math.floor(Date.now() / 1000), [timeRange, refreshCounter]);
  const xMin = xMax - RANGE_SECONDS[timeRange];

  // Build chart data
  const cpuData = useMemo(() => promRangeToUPlot(cpu.data), [cpu.data]);
  const cpuOpts = useMemo(
    () => cpuChartOptions(cpu.data?.data?.result?.map((s) => seriesLabel(s.metric)) ?? ['CPU %'], xMin, xMax),
    [cpu.data, xMin, xMax],
  );

  const memData = useMemo(() => {
    // Merge two queries into one aligned dataset
    const used = memUsed.data?.data?.result?.[0]?.values ?? [];
    const total = memTotal.data?.data?.result?.[0]?.values ?? [];
    if (!used.length) return [[], []];
    const ts = new Float64Array(used.map((v) => v[0]));
    const usedVals = new Float64Array(used.map((v) => parseFloat(v[1]) || 0));
    const totalVals = new Float64Array(total.map((v) => parseFloat(v[1]) || 0));
    return [ts, usedVals, totalVals];
  }, [memUsed.data, memTotal.data]);

  const memOpts = useMemo(() => memoryChartOptions(['Used', 'Total'], xMin, xMax), [xMin, xMax]);

  const diskData = useMemo(() => {
    const read = diskRead.data?.data?.result?.[0]?.values ?? [];
    const write = diskWrite.data?.data?.result?.[0]?.values ?? [];
    if (!read.length) return [[], []];
    const ts = new Float64Array(read.map((v) => v[0]));
    return [
      ts,
      new Float64Array(read.map((v) => parseFloat(v[1]) || 0)),
      new Float64Array(write.map((v) => parseFloat(v[1]) || 0)),
    ];
  }, [diskRead.data, diskWrite.data]);

  const diskOpts = useMemo(() => diskIOChartOptions(['Read', 'Write'], xMin, xMax), [xMin, xMax]);

  const netData = useMemo(() => {
    const rx = netRx.data?.data?.result?.[0]?.values ?? [];
    const tx = netTx.data?.data?.result?.[0]?.values ?? [];
    if (!rx.length) return [[], []];
    const ts = new Float64Array(rx.map((v) => v[0]));
    return [
      ts,
      new Float64Array(rx.map((v) => parseFloat(v[1]) || 0)),
      new Float64Array(tx.map((v) => parseFloat(v[1]) || 0)),
    ];
  }, [netRx.data, netTx.data]);

  const netOpts = useMemo(() => networkIOChartOptions(['RX', 'TX'], xMin, xMax), [xMin, xMax]);

  const handleRefresh = () => {
    setRefreshCounter((c) => c + 1);
    cpu.refetch();
    memUsed.refetch();
    memTotal.refetch();
    diskRead.refetch();
    diskWrite.refetch();
    netRx.refetch();
    netTx.refetch();
  };

  if (!metricsStatus?.available) {
    return (
      <div className="flex items-center gap-3 p-6 text-surface-400">
        <AlertTriangle className="h-5 w-5 text-amber-400" />
        <span>Metrics backend not available. No VictoriaMetrics or Prometheus detected in cluster.</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1 bg-surface-800 border border-surface-700 rounded-lg p-0.5">
          {TIME_RANGES.map((r) => (
            <button
              key={r.value}
              onClick={() => setTimeRange(r.value)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${
                timeRange === r.value
                  ? 'bg-primary-500/20 text-primary-300 shadow-sm'
                  : 'text-surface-400 hover:text-surface-200 hover:bg-surface-700'
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {isRefreshing && (
            <Loader2 className="h-4 w-4 animate-spin text-surface-400" />
          )}
          <button
            onClick={handleRefresh}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-surface-400 
                       hover:text-surface-200 bg-surface-800 hover:bg-surface-700 
                       border border-surface-700 rounded-lg transition-all"
            disabled={isRefreshing}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
        </div>
      </div>

      {/* Charts grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="h-6 w-6 animate-spin text-primary-400" />
          <span className="ml-2 text-surface-400">Loading metrics...</span>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <ChartCard title="CPU Usage" icon={<Clock className="h-4 w-4" />}>
            <UPlotChart options={cpuOpts} data={cpuData as any} />
          </ChartCard>

          <ChartCard title="Memory" icon={<Clock className="h-4 w-4" />}>
            <UPlotChart options={memOpts} data={memData as any} />
          </ChartCard>

          <ChartCard title="Disk I/O" icon={<Clock className="h-4 w-4" />}>
            <UPlotChart options={diskOpts} data={diskData as any} />
          </ChartCard>

          <ChartCard title="Network I/O" icon={<Clock className="h-4 w-4" />}>
            <UPlotChart options={netOpts} data={netData as any} />
          </ChartCard>
        </div>
      )}
    </div>
  );
}

function ChartCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        {icon && <span className="text-surface-400">{icon}</span>}
        <h4 className="text-sm font-medium text-surface-200">{title}</h4>
      </div>
      <div className="min-h-[200px]">{children}</div>
    </div>
  );
}
