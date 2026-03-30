/**
 * Network Flows page — Hubble flow data with filters and auto-refresh.
 */

import { useState } from 'react';
import {
  RefreshCw,
  Activity,
  AlertTriangle,
  CheckCircle,
  ChevronRight,
  Wifi,
  WifiOff,
} from 'lucide-react';
import clsx from 'clsx';
import { useHubbleFlows, useHubbleStatus } from '../hooks/useHubbleFlows';
import { useNamespaces } from '../hooks/useNamespaces';
import type { HubbleFlow } from '../types/hubble';
import { ActionBar } from '../components/common/ActionBar';
import { DataTable, type Column } from '@/components/common/DataTable';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const isForwarded = verdict === 'FORWARDED';
  const isDropped = verdict === 'DROPPED';
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium',
        isForwarded && 'bg-emerald-500/10 text-emerald-400',
        isDropped && 'bg-red-500/10 text-red-400',
        !isForwarded && !isDropped && 'bg-surface-700 text-surface-400',
      )}
    >
      {isForwarded && <CheckCircle className="w-3 h-3" />}
      {isDropped && <AlertTriangle className="w-3 h-3" />}
      {verdict || 'UNKNOWN'}
    </span>
  );
}

function ExpandedFlowDetail({ flow }: { flow: HubbleFlow }) {
  return (
    <div className="px-4 py-3 bg-surface-900/50 grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
      <div>
        <p className="text-surface-500 mb-0.5">Source IP</p>
        <code className="text-surface-300">{flow.source_ip || '-'}</code>
      </div>
      <div>
        <p className="text-surface-500 mb-0.5">Dest IP</p>
        <code className="text-surface-300">{flow.destination_ip || '-'}</code>
      </div>
      <div>
        <p className="text-surface-500 mb-0.5">Dest Port</p>
        <code className="text-surface-300">{flow.destination_port || '-'}</code>
      </div>
      <div>
        <p className="text-surface-500 mb-0.5">Policy Match</p>
        <span className="text-surface-300">{flow.policy_match || '-'}</span>
      </div>
      <div>
        <p className="text-surface-500 mb-0.5">Summary</p>
        <span className="text-surface-300">{flow.summary || '-'}</span>
      </div>
      {flow.drop_reason && (
        <div>
          <p className="text-surface-500 mb-0.5">Drop Reason</p>
          <span className="text-red-400">{flow.drop_reason}</span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stats Cards
// ---------------------------------------------------------------------------

function StatsCards({ flows }: { flows: HubbleFlow[] }) {
  const forwarded = flows.filter((f) => f.verdict === 'FORWARDED').length;
  const dropped = flows.filter((f) => f.verdict === 'DROPPED').length;
  const dropRate = flows.length > 0 ? Math.round((dropped / flows.length) * 100) : 0;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-4">
        <p className="text-xs text-surface-500">Total Flows</p>
        <p className="text-2xl font-semibold text-surface-100 mt-1">{flows.length}</p>
      </div>
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-4">
        <p className="text-xs text-surface-500">Forwarded</p>
        <p className="text-2xl font-semibold text-emerald-400 mt-1">{forwarded}</p>
      </div>
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-4">
        <p className="text-xs text-surface-500">Dropped</p>
        <p className="text-2xl font-semibold text-red-400 mt-1">{dropped}</p>
      </div>
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-4">
        <p className="text-xs text-surface-500">Drop Rate</p>
        <p
          className={clsx(
            'text-2xl font-semibold mt-1',
            dropRate > 20 ? 'text-red-400' : dropRate > 5 ? 'text-amber-400' : 'text-surface-100',
          )}
        >
          {dropRate}%
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

const TIME_RANGES = [
  { label: '1m', value: '1m' },
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
];

const VERDICTS = [
  { label: 'All', value: '' },
  { label: 'Forwarded', value: 'FORWARDED' },
  { label: 'Dropped', value: 'DROPPED' },
];

const PROTOCOLS = [
  { label: 'All', value: '' },
  { label: 'TCP', value: 'tcp' },
  { label: 'UDP', value: 'udp' },
  { label: 'ICMP', value: 'icmp' },
];

export default function NetworkFlows() {
  const [namespace, setNamespace] = useState('');
  const [pod, setPod] = useState('');
  const [verdict, setVerdict] = useState('');
  const [protocol, setProtocol] = useState('');
  const [since, setSince] = useState('5m');
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [expandedRow, setExpandedRow] = useState<string | null>(null);

  const { data: namespacesData } = useNamespaces();
  const { data: statusData } = useHubbleStatus();

  const params = {
    namespace: namespace || undefined,
    pod: pod || undefined,
    verdict: verdict || undefined,
    protocol: protocol || undefined,
    since,
    limit: 200,
  };

  const { data, isLoading, refetch, isFetching } = useHubbleFlows(params, autoRefresh);

  const flows = data?.flows ?? [];

  // Add stable index-based keys to flows for expand tracking
  const indexedFlows = flows.map((f, i) => ({ ...f, _idx: i }));

  const columns: Column<HubbleFlow & { _idx: number }>[] = [
    {
      key: 'time',
      header: 'Time',
      accessor: (f) => (
        <span className="font-mono text-xs text-surface-400">{formatTime(f.time)}</span>
      ),
    },
    {
      key: 'source',
      header: 'Source',
      accessor: (f) => (
        <div className="text-xs">
          {f.source_namespace && (
            <span className="text-surface-500">{f.source_namespace}/</span>
          )}
          <span className="text-surface-200 font-mono">
            {f.source_pod || f.source_ip || '-'}
          </span>
        </div>
      ),
    },
    {
      key: 'arrow',
      header: '',
      accessor: () => <ChevronRight className="w-3.5 h-3.5 text-surface-600" />,
    },
    {
      key: 'destination',
      header: 'Destination',
      accessor: (f) => (
        <div className="text-xs">
          {f.destination_namespace && (
            <span className="text-surface-500">{f.destination_namespace}/</span>
          )}
          <span className="text-surface-200 font-mono">
            {f.destination_pod || f.destination_ip || '-'}
            {f.destination_port ? `:${f.destination_port}` : ''}
          </span>
        </div>
      ),
    },
    {
      key: 'protocol',
      header: 'Protocol',
      hideOnMobile: true,
      accessor: (f) => (
        <span className="text-xs text-surface-400 font-mono">{f.protocol || '-'}</span>
      ),
    },
    {
      key: 'verdict',
      header: 'Verdict',
      accessor: (f) => <VerdictBadge verdict={f.verdict} />,
    },
    {
      key: 'drop_reason',
      header: 'Drop Reason',
      hideOnMobile: true,
      accessor: (f) =>
        f.drop_reason ? (
          <span className="text-xs text-red-400 font-mono truncate max-w-xs block">
            {f.drop_reason}
          </span>
        ) : (
          <span className="text-xs text-surface-600">-</span>
        ),
    },
  ];

  return (
    <div className="space-y-6">
      <ActionBar title="Network Flows" subtitle="Real-time Hubble network flow monitoring">
        {/* Hubble status badge */}
        {statusData && (
          <div
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border',
              statusData.available
                ? 'border-emerald-800/30 bg-emerald-900/10 text-emerald-400'
                : 'border-red-800/30 bg-red-900/10 text-red-400',
            )}
          >
            {statusData.available ? (
              <Wifi className="w-3.5 h-3.5" />
            ) : (
              <WifiOff className="w-3.5 h-3.5" />
            )}
            {statusData.available ? 'Hubble available' : 'Hubble unavailable'}
          </div>
        )}

        {/* Auto-refresh toggle */}
        <button
          onClick={() => setAutoRefresh((v) => !v)}
          className={clsx(
            'flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-colors',
            autoRefresh
              ? 'border-primary-500 bg-primary-500/10 text-primary-400'
              : 'border-surface-700 bg-surface-800 text-surface-300 hover:border-surface-600',
          )}
        >
          <Activity className={clsx('w-4 h-4', autoRefresh && 'animate-pulse')} />
          {autoRefresh ? 'Auto-refresh ON' : 'Auto-refresh'}
        </button>

        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="btn-secondary"
          title="Refresh"
        >
          <RefreshCw className={clsx('h-4 w-4', isFetching && 'animate-spin')} />
        </button>
      </ActionBar>

      {/* Filters */}
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-4">
        <div className="flex flex-wrap gap-4 items-end">
          {/* Namespace */}
          <div className="flex-1 min-w-32">
            <label className="block text-xs text-surface-400 mb-1">Namespace</label>
            <select
              value={namespace}
              onChange={(e) => setNamespace(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm focus:outline-none focus:border-primary-500"
            >
              <option value="">All namespaces</option>
              {(namespacesData?.items ?? []).map((ns) => (
                <option key={ns.name} value={ns.name}>
                  {ns.name}
                </option>
              ))}
            </select>
          </div>

          {/* Pod filter */}
          <div className="flex-1 min-w-32">
            <label className="block text-xs text-surface-400 mb-1">Pod (optional)</label>
            <input
              type="text"
              value={pod}
              onChange={(e) => setPod(e.target.value)}
              placeholder="pod-name"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 text-sm focus:outline-none focus:border-primary-500 font-mono"
            />
          </div>

          {/* Verdict */}
          <div>
            <label className="block text-xs text-surface-400 mb-1">Verdict</label>
            <div className="flex gap-1">
              {VERDICTS.map((v) => (
                <button
                  key={v.value}
                  onClick={() => setVerdict(v.value)}
                  className={clsx(
                    'px-3 py-2 text-xs rounded-lg border transition-colors',
                    verdict === v.value
                      ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                      : 'border-surface-700 text-surface-400 hover:border-surface-600',
                  )}
                >
                  {v.label}
                </button>
              ))}
            </div>
          </div>

          {/* Protocol */}
          <div>
            <label className="block text-xs text-surface-400 mb-1">Protocol</label>
            <div className="flex gap-1">
              {PROTOCOLS.map((p) => (
                <button
                  key={p.value}
                  onClick={() => setProtocol(p.value)}
                  className={clsx(
                    'px-3 py-2 text-xs rounded-lg border transition-colors',
                    protocol === p.value
                      ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                      : 'border-surface-700 text-surface-400 hover:border-surface-600',
                  )}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          {/* Time range */}
          <div>
            <label className="block text-xs text-surface-400 mb-1">Time range</label>
            <div className="flex gap-1">
              {TIME_RANGES.map((t) => (
                <button
                  key={t.value}
                  onClick={() => setSince(t.value)}
                  className={clsx(
                    'px-3 py-2 text-xs rounded-lg border transition-colors',
                    since === t.value
                      ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                      : 'border-surface-700 text-surface-400 hover:border-surface-600',
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Stats */}
      {flows.length > 0 && <StatsCards flows={flows} />}

      {/* Flow table */}
      <DataTable
        columns={columns}
        data={indexedFlows}
        loading={isLoading}
        keyExtractor={(f) => String(f._idx)}
        expandable={(f) =>
          expandedRow === String(f._idx) ? <ExpandedFlowDetail flow={f} /> : null
        }
        onRowClick={(f) => {
          const key = String(f._idx);
          setExpandedRow((prev) => (prev === key ? null : key));
        }}
        emptyState={{
          icon: <Activity className="h-16 w-16" />,
          title: 'No flows found',
          description: 'Try adjusting filters or generating some traffic.',
          action: (
            <button onClick={() => refetch()} className="btn-secondary">
              <RefreshCw className="h-4 w-4" />
              Refresh
            </button>
          ),
        }}
      />
    </div>
  );
}
