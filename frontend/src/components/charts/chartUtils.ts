import uPlot from 'uplot';
import type { PromQLResult } from '@/api/metrics';

// Color palette for chart series (dark-theme friendly)
export const CHART_COLORS = [
  '#6366f1', // indigo-500
  '#22d3ee', // cyan-400
  '#f97316', // orange-500
  '#a78bfa', // violet-400
  '#34d399', // emerald-400
  '#f472b6', // pink-400
  '#facc15', // yellow-400
  '#60a5fa', // blue-400
];

// Shared axis/grid styling for dark theme
const GRID_STROKE = 'rgba(148, 163, 184, 0.08)';
const TICK_STROKE = 'rgba(148, 163, 184, 0.15)';
const AXIS_FONT = '11px Inter, system-ui, sans-serif';
const AXIS_LABEL_COLOR = 'rgba(148, 163, 184, 0.6)';

// ---------------------------------------------------------------------------
// Data transform helpers
// ---------------------------------------------------------------------------

/**
 * Convert PromQL range result into uPlot AlignedData.
 * Returns [timestamps, ...series_values].
 */
export function promRangeToUPlot(result: PromQLResult | undefined): uPlot.AlignedData {
  if (!result?.data?.result?.length) return [[], []];

  const series = result.data.result;
  // Use the first series' timestamps as the reference
  const timestamps = series[0].values?.map((v) => v[0]) ?? [];
  const data: uPlot.AlignedData = [
    new Float64Array(timestamps),
    ...series.map((s) =>
      new Float64Array(
        (s.values ?? []).map((v) => parseFloat(v[1]) || 0)
      )
    ),
  ];
  return data;
}

/**
 * Extract label from PromQL series metric for legend display.
 */
export function seriesLabel(
  metric: Record<string, string>,
  labelKey: string = 'name',
  fallbackKeys: string[] = ['instance', 'pod', 'node'],
): string {
  if (metric[labelKey]) return metric[labelKey];
  for (const k of fallbackKeys) {
    if (metric[k]) return metric[k];
  }
  return JSON.stringify(metric);
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

export function fmtBytes(val: number): string {
  if (val >= 1e9) return (val / 1e9).toFixed(1) + ' GB';
  if (val >= 1e6) return (val / 1e6).toFixed(1) + ' MB';
  if (val >= 1e3) return (val / 1e3).toFixed(1) + ' KB';
  return val.toFixed(0) + ' B';
}

export function fmtBytesPerSec(val: number): string {
  if (val >= 1e9) return (val / 1e9).toFixed(1) + ' GB/s';
  if (val >= 1e6) return (val / 1e6).toFixed(1) + ' MB/s';
  if (val >= 1e3) return (val / 1e3).toFixed(1) + ' KB/s';
  return val.toFixed(0) + ' B/s';
}

export function fmtPercent(val: number): string {
  return val.toFixed(1) + '%';
}

export function fmtIOPS(val: number): string {
  if (val >= 1e3) return (val / 1e3).toFixed(1) + 'K';
  return val.toFixed(0);
}

export function fmtShortTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// ---------------------------------------------------------------------------
// Chart option builders
// ---------------------------------------------------------------------------

function baseAxes(yFormatter: (v: number) => string): uPlot.Axis[] {
  return [
    {
      // X axis (time)
      stroke: AXIS_LABEL_COLOR,
      font: AXIS_FONT,
      grid: { stroke: GRID_STROKE, width: 1 },
      ticks: { stroke: TICK_STROKE, width: 1 },
    },
    {
      // Y axis
      stroke: AXIS_LABEL_COLOR,
      font: AXIS_FONT,
      grid: { stroke: GRID_STROKE, width: 1 },
      ticks: { stroke: TICK_STROKE, width: 1 },
      values: (_u: uPlot, vals: number[]) => vals.map((v) => yFormatter(v)),
      size: 70,
    },
  ];
}

function baseSeries(
  labels: string[],
  colors: string[],
  fill?: boolean,
): uPlot.Series[] {
  return [
    {}, // timestamp series (no config needed)
    ...labels.map((label, i) => ({
      label,
      stroke: colors[i % colors.length],
      width: 2,
      fill: fill ? colors[i % colors.length] + '18' : undefined,
    })),
  ];
}

export interface ChartConfig {
  title?: string;
  height?: number;
  labels: string[];
  colors?: string[];
  yFormatter: (val: number) => string;
  fill?: boolean;
  xMin?: number;
  xMax?: number;
}

export function buildChartOptions(cfg: ChartConfig): uPlot.Options {
  const colors = cfg.colors ?? CHART_COLORS;
  const axes = baseAxes(cfg.yFormatter);

  // Pin X-axis to the requested time window so 7D actually shows 7 days
  const scales: uPlot.Options['scales'] = {};
  if (cfg.xMin != null && cfg.xMax != null) {
    scales['x'] = { min: cfg.xMin, max: cfg.xMax };
  }

  return {
    width: 400, // will be overridden by container
    height: cfg.height ?? 200,
    cursor: {
      drag: { x: false, y: false },
    },
    legend: {
      show: true,
      live: false,
    },
    scales,
    axes,
    series: baseSeries(cfg.labels, colors, cfg.fill),
  };
}

// ---------------------------------------------------------------------------
// Predefined chart configs for KubeVirt metrics
// ---------------------------------------------------------------------------

export function cpuChartOptions(seriesLabels: string[], xMin?: number, xMax?: number): uPlot.Options {
  return buildChartOptions({
    labels: seriesLabels,
    yFormatter: fmtPercent,
    colors: ['#6366f1', '#22d3ee'],
    xMin, xMax,
  });
}

export function memoryChartOptions(seriesLabels: string[], xMin?: number, xMax?: number): uPlot.Options {
  return buildChartOptions({
    labels: seriesLabels,
    yFormatter: fmtBytes,
    fill: true,
    colors: ['#6366f1', '#a78bfa', '#22d3ee'],
    xMin, xMax,
  });
}

export function diskIOChartOptions(seriesLabels: string[], xMin?: number, xMax?: number): uPlot.Options {
  return buildChartOptions({
    labels: seriesLabels,
    yFormatter: fmtBytesPerSec,
    colors: ['#34d399', '#f97316'],
    xMin, xMax,
  });
}

export function networkIOChartOptions(seriesLabels: string[], xMin?: number, xMax?: number): uPlot.Options {
  return buildChartOptions({
    labels: seriesLabels,
    yFormatter: fmtBytesPerSec,
    colors: ['#60a5fa', '#f472b6'],
    xMin, xMax,
  });
}
