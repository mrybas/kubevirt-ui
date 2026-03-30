import { useState, useMemo } from 'react';
import { RefreshCw, Loader2, BarChart3 } from 'lucide-react';
import UPlotChart from './UPlotChart';
import { useMetricsRange, useMetricsStatus, type TimeRange } from '@/hooks/useMetrics';
import {
  buildChartOptions,
  fmtBytesPerSec,
  seriesLabel,
} from './chartUtils';

const TIME_RANGES: { label: string; value: TimeRange }[] = [
  { label: '1H', value: '1h' },
  { label: '6H', value: '6h' },
  { label: '24H', value: '24h' },
];

export default function NetworkMetrics() {
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const { data: metricsStatus } = useMetricsStatus();

  const enabled = metricsStatus?.available === true;

  // Kube-OVN interface traffic (aggregate by interface name)
  const rxQuery = 'sum by (hostname) (rate(kube_ovn_interface_rx_bytes[5m]))';
  const txQuery = 'sum by (hostname) (rate(kube_ovn_interface_tx_bytes[5m]))';

  // VM network traffic (per VM)
  const vmRxQuery = 'rate(kubevirt_vmi_network_receive_bytes_total[5m])';
  const vmTxQuery = 'rate(kubevirt_vmi_network_transmit_bytes_total[5m])';

  const ovnRx = useMetricsRange(rxQuery, timeRange, enabled);
  const ovnTx = useMetricsRange(txQuery, timeRange, enabled);
  const vmRx = useMetricsRange(vmRxQuery, timeRange, enabled);
  const vmTx = useMetricsRange(vmTxQuery, timeRange, enabled);

  const isLoading = vmRx.isLoading;
  const isRefreshing = vmRx.isFetching;

  // VM network RX chart (per VM)
  const vmRxChartData = useMemo(() => {
    const result = vmRx.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [vmRx.data]);

  const vmRxLabels = useMemo(
    () => (vmRx.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'name')),
    [vmRx.data],
  );

  const vmRxOpts = useMemo(
    () => buildChartOptions({ labels: vmRxLabels.length ? vmRxLabels : ['RX'], yFormatter: fmtBytesPerSec, height: 200 }),
    [vmRxLabels],
  );

  // VM network TX chart (per VM)
  const vmTxChartData = useMemo(() => {
    const result = vmTx.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [vmTx.data]);

  const vmTxLabels = useMemo(
    () => (vmTx.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'name')),
    [vmTx.data],
  );

  const vmTxOpts = useMemo(
    () => buildChartOptions({ labels: vmTxLabels.length ? vmTxLabels : ['TX'], yFormatter: fmtBytesPerSec, height: 200, colors: ['#f472b6', '#a78bfa', '#f97316', '#34d399'] }),
    [vmTxLabels],
  );

  // OVN interface RX
  const ovnRxChartData = useMemo(() => {
    const result = ovnRx.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [ovnRx.data]);

  const ovnRxLabels = useMemo(
    () => (ovnRx.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'hostname')),
    [ovnRx.data],
  );

  const ovnRxOpts = useMemo(
    () => buildChartOptions({ labels: ovnRxLabels.length ? ovnRxLabels : ['RX'], yFormatter: fmtBytesPerSec, height: 200, colors: ['#22d3ee', '#6366f1', '#34d399', '#f97316'] }),
    [ovnRxLabels],
  );

  // OVN interface TX
  const ovnTxChartData = useMemo(() => {
    const result = ovnTx.data?.data?.result ?? [];
    if (!result.length) return [[], []];
    const ts = new Float64Array(result[0].values?.map((v: [number, string]) => v[0]) ?? []);
    return [ts, ...result.map((s) =>
      new Float64Array((s.values ?? []).map((v: [number, string]) => parseFloat(v[1]) || 0))
    )];
  }, [ovnTx.data]);

  const ovnTxLabels = useMemo(
    () => (ovnTx.data?.data?.result ?? []).map((s) => seriesLabel(s.metric, 'hostname')),
    [ovnTx.data],
  );

  const ovnTxOpts = useMemo(
    () => buildChartOptions({ labels: ovnTxLabels.length ? ovnTxLabels : ['TX'], yFormatter: fmtBytesPerSec, height: 200, colors: ['#f472b6', '#a78bfa', '#f97316', '#22d3ee'] }),
    [ovnTxLabels],
  );

  const handleRefresh = () => {
    ovnRx.refetch();
    ovnTx.refetch();
    vmRx.refetch();
    vmTx.refetch();
  };

  if (!metricsStatus?.available) return null;

  return (
    <div className="card">
      <div className="card-header flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-primary-400" />
          <h3 className="font-display text-lg font-semibold">Network Traffic</h3>
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
          <span className="text-surface-400 text-sm">Loading network metrics...</span>
        </div>
      ) : (
        <div className="p-4 space-y-4">
          {/* VM Network Traffic */}
          <h4 className="text-sm font-medium text-surface-300">VM Network Traffic</h4>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
              <h4 className="text-xs font-medium text-surface-400 mb-2">Receive (per VM)</h4>
              <UPlotChart options={vmRxOpts} data={vmRxChartData as any} />
            </div>
            <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
              <h4 className="text-xs font-medium text-surface-400 mb-2">Transmit (per VM)</h4>
              <UPlotChart options={vmTxOpts} data={vmTxChartData as any} />
            </div>
          </div>

          {/* Kube-OVN Interface Traffic */}
          <h4 className="text-sm font-medium text-surface-300 mt-4">Kube-OVN Interface Traffic</h4>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
              <h4 className="text-xs font-medium text-surface-400 mb-2">Interface RX</h4>
              <UPlotChart options={ovnRxOpts} data={ovnRxChartData as any} />
            </div>
            <div className="bg-surface-900/50 border border-surface-700/50 rounded-xl p-3">
              <h4 className="text-xs font-medium text-surface-400 mb-2">Interface TX</h4>
              <UPlotChart options={ovnTxOpts} data={ovnTxChartData as any} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
