/**
 * Backups page — Velero backups/schedules, VM snapshots, storage info.
 *
 * Sections:
 *   1. Backup Schedules  (Velero Schedules + VM Snapshot CronJobs)
 *   2. Recent Backups    (Velero Backups — manual + scheduled)
 *   3. VM Snapshots      (cluster-wide cross-VM view)
 *   4. Storage Info      (Velero BackupStorageLocations)
 */

import { useState } from 'react';
import {
  Archive,
  RefreshCw,
  Plus,
  Trash2,
  RotateCcw,
  Pause,
  Play,
  AlertTriangle,
  CheckCircle,
  XCircle,
  Loader2,
  Database,
  Clock,
  Camera,
  CalendarClock,
  HardDrive,
  Info,
} from 'lucide-react';
import clsx from 'clsx';

import { ActionBar } from '../components/common/ActionBar';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { Modal } from '@/components/common/Modal';

import {
  useVeleroBackups,
  useCreateVeleroBackup,
  useDeleteVeleroBackup,
  useRestoreVeleroBackup,
  useVeleroSchedules,
  useCreateVeleroSchedule,
  useDeleteVeleroSchedule,
  usePatchVeleroSchedule,
  useVeleroStorageLocations,
  useCreateStorageLocation,
  useDeleteStorageLocation,
  useAllVMSnapshots,
  useAllSnapshotSchedules,
  useCreateSnapshotSchedule,
  useDeleteSnapshotSchedule,
  usePatchSnapshotSchedule,
} from '../hooks/useVelero';
import { useDeleteVMSnapshot, useRestoreVMSnapshot } from '../hooks/useVMs';

import type { VeleroBackup, VeleroSchedule } from '../types/velero';
import type { VMSnapshotInfo } from '../types/vm';
import type { ScheduleInfo } from '../api/schedules';

// ── Helpers ───────────────────────────────────────────────────────────────────

function ago(ts: string) {
  if (!ts) return '—';
  const ms = Date.now() - new Date(ts).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function fmt(ts: string) {
  if (!ts) return '—';
  return new Date(ts).toLocaleString();
}

function PhaseBadge({ phase }: { phase: string }) {
  const p = phase?.toLowerCase() ?? '';
  const classes = clsx(
    'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium',
    p === 'completed' && 'bg-emerald-500/10 text-emerald-400',
    p === 'failed' && 'bg-red-500/10 text-red-400',
    (p === 'inprogress' || p === 'running') && 'bg-amber-500/10 text-amber-400',
    p === 'enabled' && 'bg-emerald-500/10 text-emerald-400',
    !['completed', 'failed', 'inprogress', 'running', 'enabled'].includes(p) &&
      'bg-surface-700 text-surface-400',
  );
  const Icon =
    p === 'completed' || p === 'enabled'
      ? CheckCircle
      : p === 'failed'
        ? XCircle
        : p === 'inprogress' || p === 'running'
          ? Loader2
          : Clock;
  return (
    <span className={classes}>
      <Icon className={clsx('w-3 h-3', (p === 'inprogress' || p === 'running') && 'animate-spin')} />
      {phase || 'Unknown'}
    </span>
  );
}

// ── Cron Builder ──────────────────────────────────────────────────────────────

const CRON_PRESETS = [
  { label: 'Daily 2:00', value: '0 2 * * *' },
  { label: 'Weekly Sun', value: '0 2 * * 0' },
  { label: 'Every hour', value: '0 * * * *' },
  { label: 'Every 6h', value: '0 */6 * * *' },
];

function CronBuilder({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <label className="block text-xs text-surface-300 mb-1">Schedule (cron)</label>
      <div className="flex flex-wrap gap-1.5 mb-2">
        {CRON_PRESETS.map((p) => (
          <button
            key={p.value}
            type="button"
            onClick={() => onChange(p.value)}
            className={clsx(
              'px-2.5 py-1 rounded-md text-xs border transition-colors',
              value === p.value
                ? 'bg-primary-500/20 border-primary-500/50 text-primary-300'
                : 'bg-surface-800 border-surface-700 text-surface-400 hover:border-surface-600',
            )}
          >
            {p.label}
          </button>
        ))}
      </div>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0 2 * * * (cron expression)"
        className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
      />
    </div>
  );
}

// ── Create Schedule Modal ─────────────────────────────────────────────────────

type ScheduleType = 'velero' | 'snapshot';

interface CreateScheduleState {
  type: ScheduleType;
  name: string;
  schedule: string;
  // Velero fields
  included_namespaces: string;
  label_selector: string;
  snapshot_volumes: boolean;
  ttl_days: string;
  // Snapshot fields
  vm_namespace: string;
  vm_name: string;
}

const DEFAULT_SCHEDULE_STATE: CreateScheduleState = {
  type: 'velero',
  name: '',
  schedule: '0 2 * * *',
  included_namespaces: '',
  label_selector: '',
  snapshot_volumes: true,
  ttl_days: '30',
  vm_namespace: '',
  vm_name: '',
};

function CreateScheduleModal({
  onClose,
  onCreateVelero,
  onCreateSnapshot,
  isCreating,
}: {
  onClose: () => void;
  onCreateVelero: (data: {
    name: string;
    schedule: string;
    included_namespaces: string[];
    label_selector: string;
    snapshot_volumes: boolean;
    ttl: string;
  }) => void;
  onCreateSnapshot: (data: {
    namespace: string;
    vm_name: string;
    vm_namespace: string;
    name: string;
    schedule: string;
  }) => void;
  isCreating: boolean;
}) {
  const [state, setState] = useState<CreateScheduleState>(DEFAULT_SCHEDULE_STATE);
  const set = (patch: Partial<CreateScheduleState>) => setState((s) => ({ ...s, ...patch }));

  const valid =
    state.name.length > 0 &&
    state.schedule.length > 0 &&
    (state.type === 'velero' || (state.vm_namespace.length > 0 && state.vm_name.length > 0));

  const handleSubmit = () => {
    if (state.type === 'velero') {
      onCreateVelero({
        name: state.name,
        schedule: state.schedule,
        included_namespaces: state.included_namespaces
          ? state.included_namespaces.split(',').map((s) => s.trim()).filter(Boolean)
          : [],
        label_selector: state.label_selector,
        snapshot_volumes: state.snapshot_volumes,
        ttl: `${parseInt(state.ttl_days, 10) * 24}h`,
      });
    } else {
      onCreateSnapshot({
        namespace: state.vm_namespace,
        vm_name: state.vm_name,
        vm_namespace: state.vm_namespace,
        name: state.name,
        schedule: state.schedule,
      });
    }
  };

  return (
    <Modal isOpen onClose={onClose} title="Create Backup Schedule" size="lg">
      <div className="space-y-4">
        {/* Type selector */}
        <div>
          <label className="block text-xs text-surface-300 mb-1.5">Schedule Type</label>
          <div className="flex gap-2">
            {(['velero', 'snapshot'] as ScheduleType[]).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => set({ type: t })}
                className={clsx(
                  'flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors',
                  state.type === t
                    ? 'bg-primary-500/20 border-primary-500/50 text-primary-300'
                    : 'bg-surface-800 border-surface-700 text-surface-400 hover:border-surface-600',
                )}
              >
                {t === 'velero' ? 'Full Backup (Velero)' : 'VM Snapshot'}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="col-span-2">
            <label className="block text-xs text-surface-300 mb-1">Schedule Name *</label>
            <input
              type="text"
              value={state.name}
              onChange={(e) => set({ name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') })}
              placeholder="my-backup-schedule"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            />
          </div>
        </div>

        <CronBuilder value={state.schedule} onChange={(v) => set({ schedule: v })} />

        {state.type === 'velero' ? (
          <>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-surface-300 mb-1">Namespaces (comma-separated)</label>
                <input
                  type="text"
                  value={state.included_namespaces}
                  onChange={(e) => set({ included_namespaces: e.target.value })}
                  placeholder="default, production (empty = all)"
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-surface-300 mb-1">Label Filter</label>
                <input
                  type="text"
                  value={state.label_selector}
                  onChange={(e) => set({ label_selector: e.target.value })}
                  placeholder="app=myapp,env=prod"
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-surface-300 mb-1">Retention (days)</label>
                <input
                  type="number"
                  min="1"
                  value={state.ttl_days}
                  onChange={(e) => set({ ttl_days: e.target.value })}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
                />
              </div>
              <div className="flex items-center gap-2 pt-5">
                <input
                  type="checkbox"
                  id="snap-vols"
                  checked={state.snapshot_volumes}
                  onChange={(e) => set({ snapshot_volumes: e.target.checked })}
                  className="w-4 h-4 rounded border-surface-600"
                />
                <label htmlFor="snap-vols" className="text-sm text-surface-300">Snapshot volumes</label>
              </div>
            </div>
          </>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-surface-300 mb-1">VM Namespace *</label>
              <input
                type="text"
                value={state.vm_namespace}
                onChange={(e) => set({ vm_namespace: e.target.value })}
                placeholder="default"
                className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-surface-300 mb-1">VM Name *</label>
              <input
                type="text"
                value={state.vm_name}
                onChange={(e) => set({ vm_name: e.target.value })}
                placeholder="my-vm"
                className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              />
            </div>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={handleSubmit}
            disabled={!valid || isCreating}
            className="btn-primary"
          >
            {isCreating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            Create Schedule
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Create Backup Modal ───────────────────────────────────────────────────────

function CreateBackupModal({
  onClose,
  onCreate,
  isCreating,
}: {
  onClose: () => void;
  onCreate: (data: {
    name: string;
    included_namespaces: string[];
    label_selector: string;
    snapshot_volumes: boolean;
    ttl: string;
  }) => void;
  isCreating: boolean;
}) {
  const [name, setName] = useState('');
  const [namespaces, setNamespaces] = useState('');
  const [labels, setLabels] = useState('');
  const [ttlDays, setTtlDays] = useState('30');
  const [snapVols, setSnapVols] = useState(true);

  return (
    <Modal isOpen onClose={onClose} title="Create Manual Backup" size="lg">
      <div className="space-y-4">
        <div>
          <label className="block text-xs text-surface-300 mb-1">Backup Name *</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
            placeholder="my-backup-20240101"
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-surface-300 mb-1">Namespaces (comma-separated)</label>
            <input
              type="text"
              value={namespaces}
              onChange={(e) => setNamespaces(e.target.value)}
              placeholder="default, production (empty = all)"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs text-surface-300 mb-1">Label Filter</label>
            <input
              type="text"
              value={labels}
              onChange={(e) => setLabels(e.target.value)}
              placeholder="app=myapp"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-surface-300 mb-1">Retention (days)</label>
            <input
              type="number"
              min="1"
              value={ttlDays}
              onChange={(e) => setTtlDays(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
            />
          </div>
          <div className="flex items-center gap-2 pt-5">
            <input
              type="checkbox"
              id="bk-snap-vols"
              checked={snapVols}
              onChange={(e) => setSnapVols(e.target.checked)}
              className="w-4 h-4 rounded border-surface-600"
            />
            <label htmlFor="bk-snap-vols" className="text-sm text-surface-300">Snapshot volumes</label>
          </div>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={() =>
              onCreate({
                name,
                included_namespaces: namespaces
                  ? namespaces.split(',').map((s) => s.trim()).filter(Boolean)
                  : [],
                label_selector: labels,
                snapshot_volumes: snapVols,
                ttl: `${parseInt(ttlDays, 10) * 24}h`,
              })
            }
            disabled={!name || isCreating}
            className="btn-primary"
          >
            {isCreating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Archive className="w-4 h-4" />}
            Start Backup
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Restore Backup Modal ──────────────────────────────────────────────────────

function RestoreBackupModal({
  backup,
  onClose,
  onRestore,
  isRestoring,
}: {
  backup: VeleroBackup;
  onClose: () => void;
  onRestore: (restorePvs: boolean) => void;
  isRestoring: boolean;
}) {
  const [restorePvs, setRestorePvs] = useState(true);
  return (
    <Modal isOpen onClose={onClose} title={`Restore: ${backup.name}`} size="md">
      <div className="space-y-4">
        <div className="flex items-start gap-3 p-3 bg-amber-900/10 border border-amber-800/30 rounded-lg">
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <p className="text-sm text-amber-300">
            This will create a Velero Restore from backup <strong>{backup.name}</strong>. Existing resources may be overwritten.
          </p>
        </div>
        <div>
          <p className="text-xs text-surface-500 mb-1">Namespaces to restore</p>
          <p className="text-sm text-surface-200">
            {backup.included_namespaces.length > 0 ? backup.included_namespaces.join(', ') : 'All namespaces'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="restore-pvs"
            checked={restorePvs}
            onChange={(e) => setRestorePvs(e.target.checked)}
            className="w-4 h-4 rounded border-surface-600"
          />
          <label htmlFor="restore-pvs" className="text-sm text-surface-300">Restore persistent volumes</label>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={() => onRestore(restorePvs)}
            disabled={isRestoring}
            className="px-4 py-2 bg-amber-600 hover:bg-amber-500 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-2"
          >
            {isRestoring ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4" />}
            Restore
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Section header helper ─────────────────────────────────────────────────────

function SectionHeader({
  icon: Icon,
  title,
  count,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  count?: number;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between mb-3">
      <h2 className="text-sm font-semibold text-surface-300 flex items-center gap-2">
        <Icon className="w-4 h-4 text-primary-400" />
        {title}
        {count !== undefined && (
          <span className="text-xs font-normal text-surface-500">({count})</span>
        )}
      </h2>
      {children}
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Backups() {
  // Velero backups
  const { data: veleroBackups = [], isLoading: backupsLoading, refetch: refetchBackups } = useVeleroBackups();
  const createBackup = useCreateVeleroBackup();
  const deleteBackup = useDeleteVeleroBackup();
  const restoreBackup = useRestoreVeleroBackup();

  // Velero schedules
  const { data: veleroSchedules = [], isLoading: vSchedulesLoading, refetch: refetchVSchedules } = useVeleroSchedules();
  const createVeleroSchedule = useCreateVeleroSchedule();
  const deleteVeleroSchedule = useDeleteVeleroSchedule();
  const patchVeleroSchedule = usePatchVeleroSchedule();
  const createStorage = useCreateStorageLocation();
  const deleteStorage = useDeleteStorageLocation();

  // VM snapshot schedules (CronJobs)
  const { schedules: snapshotSchedules, isLoading: snapSchedulesLoading, refetch: refetchSnapSchedules } = useAllSnapshotSchedules();
  const createSnapshotSchedule = useCreateSnapshotSchedule();
  const deleteSnapshotSchedule = useDeleteSnapshotSchedule();
  const patchSnapshotSchedule = usePatchSnapshotSchedule();

  // VM snapshots (cluster-wide)
  const { snapshots: allSnapshots, isLoading: snapshotsLoading, refetch: refetchSnapshots } = useAllVMSnapshots();
  const deleteSnapshot = useDeleteVMSnapshot();
  const restoreSnapshot = useRestoreVMSnapshot();

  // Storage locations
  const { data: storageLocations = [], isLoading: storageLoading, refetch: refetchStorage } = useVeleroStorageLocations();

  // Modal state
  const [showCreateSchedule, setShowCreateSchedule] = useState(false);
  const [showCreateBackup, setShowCreateBackup] = useState(false);
  const [restoreBackupItem, setRestoreBackupItem] = useState<VeleroBackup | null>(null);
  const [deleteConfirmBackup, setDeleteConfirmBackup] = useState<string | null>(null);
  const [restoreSnapshotItem, setRestoreSnapshotItem] = useState<VMSnapshotInfo | null>(null);
  const [showAddStorage, setShowAddStorage] = useState(false);

  const refetchAll = () => {
    refetchBackups();
    refetchVSchedules();
    refetchSnapSchedules();
    refetchSnapshots();
    refetchStorage();
  };

  const isLoading = backupsLoading || vSchedulesLoading || snapSchedulesLoading || snapshotsLoading || storageLoading;

  // ── Section 1: Backup Schedules ─────────────────────────────────────────────

  // Combined schedule rows: Velero schedules + VM snapshot CronJobs
  type ScheduleRow =
    | { kind: 'velero'; data: VeleroSchedule }
    | { kind: 'snapshot'; data: ScheduleInfo };

  const scheduleRows: ScheduleRow[] = [
    ...veleroSchedules.map((s): ScheduleRow => ({ kind: 'velero', data: s })),
    ...snapshotSchedules.map((s): ScheduleRow => ({ kind: 'snapshot', data: s })),
  ];

  const scheduleColumns: Column<ScheduleRow>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (row) => (
        <span className="font-mono font-medium text-surface-100">
          {row.kind === 'velero' ? row.data.name : row.data.name}
        </span>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      accessor: (row) => (
        <span
          className={clsx(
            'px-2 py-0.5 rounded-full text-xs font-medium',
            row.kind === 'velero'
              ? 'bg-blue-500/10 text-blue-400'
              : 'bg-purple-500/10 text-purple-400',
          )}
        >
          {row.kind === 'velero' ? 'Velero' : 'Snapshot'}
        </span>
      ),
    },
    {
      key: 'target',
      header: 'Target',
      hideOnMobile: true,
      accessor: (row) => {
        if (row.kind === 'velero') {
          const ns = row.data.included_namespaces;
          return (
            <span className="text-surface-400 text-xs">
              {ns.length > 0 ? ns.join(', ') : 'All namespaces'}
            </span>
          );
        }
        return (
          <span className="text-surface-400 text-xs font-mono">
            {row.data.vm_namespace}/{row.data.vm_name}
          </span>
        );
      },
    },
    {
      key: 'schedule',
      header: 'Schedule',
      hideOnMobile: true,
      accessor: (row) => (
        <span className="font-mono text-xs text-surface-300">
          {row.kind === 'velero' ? row.data.schedule : row.data.schedule}
        </span>
      ),
    },
    {
      key: 'last_run',
      header: 'Last Run',
      hideOnMobile: true,
      accessor: (row) => {
        const ts = row.kind === 'velero' ? row.data.last_backup : row.data.last_schedule_time;
        return <span className="text-surface-500 text-xs">{ago(ts ?? '')}</span>;
      },
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (row) => {
        const paused = row.kind === 'velero' ? row.data.paused : row.data.suspended;
        return paused ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-surface-700 text-surface-400">
            <Pause className="w-3 h-3" />
            Paused
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-emerald-500/10 text-emerald-400">
            <CheckCircle className="w-3 h-3" />
            Active
          </span>
        );
      },
    },
  ];

  const getScheduleActions = (row: ScheduleRow): MenuItem[] => {
    const paused = row.kind === 'velero' ? row.data.paused : row.data.suspended;
    return [
      {
        label: paused ? 'Resume' : 'Pause',
        icon: paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />,
        onClick: () => {
          if (row.kind === 'velero') {
            patchVeleroSchedule.mutate({ name: row.data.name, paused: !paused });
          } else {
            patchSnapshotSchedule.mutate({
              namespace: row.data.namespace,
              name: row.data.name,
              suspend: !paused,
            });
          }
        },
      },
      {
        label: 'Delete',
        icon: <Trash2 className="h-4 w-4" />,
        onClick: () => {
          if (row.kind === 'velero') {
            deleteVeleroSchedule.mutate(row.data.name);
          } else {
            deleteSnapshotSchedule.mutate({ namespace: row.data.namespace, name: row.data.name });
          }
        },
        variant: 'danger',
      },
    ];
  };

  // ── Section 2: Recent Backups ───────────────────────────────────────────────

  const backupColumns: Column<VeleroBackup>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (b) => <span className="font-mono font-medium text-surface-100">{b.name}</span>,
    },
    {
      key: 'namespaces',
      header: 'Namespaces',
      hideOnMobile: true,
      accessor: (b) => (
        <span className="text-surface-400 text-xs">
          {b.included_namespaces.length > 0 ? b.included_namespaces.join(', ') : 'All'}
        </span>
      ),
    },
    {
      key: 'items',
      header: 'Items',
      hideOnMobile: true,
      accessor: (b) => (
        <span className="text-surface-400 text-xs">
          {b.items_backed_up}/{b.total_items}
        </span>
      ),
    },
    {
      key: 'phase',
      header: 'Phase',
      accessor: (b) => <PhaseBadge phase={b.phase} />,
    },
    {
      key: 'age',
      header: 'Age',
      hideOnMobile: true,
      accessor: (b) => <span className="text-surface-500 text-xs">{ago(b.creation_time)}</span>,
    },
    {
      key: 'expiry',
      header: 'Expires',
      hideOnMobile: true,
      accessor: (b) => <span className="text-surface-500 text-xs">{fmt(b.expiration)}</span>,
    },
  ];

  const getBackupActions = (b: VeleroBackup): MenuItem[] => [
    ...(b.phase === 'Completed'
      ? [
          {
            label: 'Restore',
            icon: <RotateCcw className="h-4 w-4" />,
            onClick: () => setRestoreBackupItem(b),
          },
        ]
      : []),
    {
      label: 'Delete',
      icon: <Trash2 className="h-4 w-4" />,
      onClick: () => setDeleteConfirmBackup(b.name),
      variant: 'danger',
    },
  ];

  // ── Section 3: VM Snapshots ─────────────────────────────────────────────────

  const snapshotColumns: Column<VMSnapshotInfo>[] = [
    {
      key: 'name',
      header: 'Snapshot',
      sortable: true,
      accessor: (s) => <span className="font-mono font-medium text-surface-100">{s.name}</span>,
    },
    {
      key: 'vm',
      header: 'VM',
      accessor: (s) => <span className="text-surface-300 text-sm">{s.vm_name}</span>,
    },
    {
      key: 'namespace',
      header: 'Namespace',
      hideOnMobile: true,
      accessor: (s) => <span className="text-surface-400 text-xs font-mono">{s.namespace}</span>,
    },
    {
      key: 'ready',
      header: 'Ready',
      accessor: (s) =>
        s.ready ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-emerald-500/10 text-emerald-400">
            <CheckCircle className="w-3 h-3" />
            Ready
          </span>
        ) : s.error ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-red-500/10 text-red-400">
            <XCircle className="w-3 h-3" />
            Error
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-amber-500/10 text-amber-400">
            <Loader2 className="w-3 h-3 animate-spin" />
            {s.phase}
          </span>
        ),
    },
    {
      key: 'age',
      header: 'Age',
      hideOnMobile: true,
      accessor: (s) => <span className="text-surface-500 text-xs">{ago(s.creation_time)}</span>,
    },
  ];

  const getSnapshotActions = (s: VMSnapshotInfo): MenuItem[] => [
    ...(s.ready
      ? [
          {
            label: 'Restore',
            icon: <RotateCcw className="h-4 w-4" />,
            onClick: () => setRestoreSnapshotItem(s),
          },
        ]
      : []),
    {
      label: 'Delete',
      icon: <Trash2 className="h-4 w-4" />,
      onClick: () =>
        deleteSnapshot.mutate({ namespace: s.namespace, vmName: s.vm_name, snapshotName: s.name }),
      variant: 'danger',
    },
  ];

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">
      <ActionBar title="Backups" subtitle="Velero backups, VM snapshots, and backup schedules">
        <button onClick={refetchAll} disabled={isLoading} className="btn-secondary" title="Refresh">
          <RefreshCw className={clsx('h-4 w-4', isLoading && 'animate-spin')} />
        </button>
      </ActionBar>

      {/* ── 1. Backup Schedules ── */}
      <div>
        <SectionHeader icon={CalendarClock} title="Backup Schedules" count={scheduleRows.length}>
          <button
            onClick={() => setShowCreateSchedule(true)}
            className="btn-secondary text-xs flex items-center gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" />
            Create Schedule
          </button>
        </SectionHeader>
        <DataTable
          columns={scheduleColumns}
          data={scheduleRows}
          loading={vSchedulesLoading || snapSchedulesLoading}
          keyExtractor={(row) =>
            row.kind === 'velero'
              ? `v-${row.data.name}`
              : `s-${row.data.namespace}-${row.data.name}`
          }
          actions={getScheduleActions}
          emptyState={{
            icon: <CalendarClock className="h-10 w-10" />,
            title: 'No backup schedules',
            description: 'Create a schedule to run automated backups or VM snapshots.',
          }}
        />
      </div>

      {/* ── 2. Recent Backups ── */}
      <div>
        <SectionHeader icon={Archive} title="Recent Backups" count={veleroBackups.length}>
          <button
            onClick={() => setShowCreateBackup(true)}
            className="btn-secondary text-xs flex items-center gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" />
            Manual Backup
          </button>
        </SectionHeader>
        <DataTable
          columns={backupColumns}
          data={veleroBackups}
          loading={backupsLoading}
          keyExtractor={(b) => b.name}
          actions={getBackupActions}
          emptyState={{
            icon: <Archive className="h-10 w-10" />,
            title: 'No backups found',
            description: 'No Velero backups exist yet. Create a manual backup or set up a schedule.',
          }}
        />
      </div>

      {/* ── 3. VM Snapshots ── */}
      <div>
        <SectionHeader icon={Camera} title="VM Snapshots" count={allSnapshots.length} />
        <DataTable
          columns={snapshotColumns}
          data={allSnapshots}
          loading={snapshotsLoading}
          keyExtractor={(s) => `${s.namespace}/${s.name}`}
          actions={getSnapshotActions}
          emptyState={{
            icon: <Camera className="h-10 w-10" />,
            title: 'No VM snapshots',
            description: 'No VirtualMachineSnapshot objects found across the cluster.',
          }}
        />
      </div>

      {/* ── 4. Storage Info ── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <SectionHeader icon={HardDrive} title="Storage Locations" count={storageLocations.length} />
          <button onClick={() => setShowAddStorage(true)} className="btn-secondary text-xs flex items-center gap-1.5">
            <Plus className="w-3.5 h-3.5" />
            Add Storage
          </button>
        </div>
        {storageLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-surface-400" />
          </div>
        ) : storageLocations.length === 0 ? (
          <div className="bg-surface-800 border border-surface-700 rounded-xl p-6 text-center text-surface-500">
            <Database className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p>No backup storage configured</p>
            <p className="text-xs mt-1">Add a storage location (S3/MinIO) to enable backups.</p>
            <button onClick={() => setShowAddStorage(true)} className="btn-primary mt-3 text-xs">
              <Plus className="w-3.5 h-3.5" />
              Add Storage Location
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {storageLocations.map((loc) => (
              <div
                key={loc.name}
                className="bg-surface-800 border border-surface-700 rounded-xl p-5 space-y-3"
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-2">
                    <Database className="w-4 h-4 text-primary-400 shrink-0" />
                    <span className="font-mono font-medium text-surface-100">{loc.name}</span>
                    {loc.default && (
                      <span className="px-1.5 py-0.5 rounded text-xs bg-primary-500/10 text-primary-400">
                        default
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <PhaseBadge phase={loc.phase || 'Unknown'} />
                    <button
                      onClick={() => deleteStorage.mutate(loc.name)}
                      className="p-1 text-surface-500 hover:text-red-400 transition-colors"
                      title="Delete storage location"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <p className="text-xs text-surface-500">Provider</p>
                    <p className="text-surface-300 font-mono">{loc.provider || '—'}</p>
                  </div>
                  <div>
                    <p className="text-xs text-surface-500">Bucket</p>
                    <p className="text-surface-300 font-mono">{loc.bucket || '—'}</p>
                  </div>
                  <div>
                    <p className="text-xs text-surface-500">Access Mode</p>
                    <p className="text-surface-300">{loc.access_mode || '—'}</p>
                  </div>
                  <div>
                    <p className="text-xs text-surface-500">Last Synced</p>
                    <p className="text-surface-300 text-xs">{ago(loc.last_synced)}</p>
                  </div>
                </div>
                {loc.prefix && (
                  <div className="flex items-center gap-1.5 text-xs text-surface-500">
                    <Info className="w-3 h-3" />
                    Prefix: <code className="font-mono text-surface-400">{loc.prefix}</code>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Modals ── */}

      {showCreateSchedule && (
        <CreateScheduleModal
          onClose={() => setShowCreateSchedule(false)}
          onCreateVelero={async (data) => {
            await createVeleroSchedule.mutateAsync(data);
            setShowCreateSchedule(false);
          }}
          onCreateSnapshot={async (data) => {
            await createSnapshotSchedule.mutateAsync({
              namespace: data.namespace,
              data: {
                name: data.name,
                action: 'snapshot',
                schedule: data.schedule,
                vm_name: data.vm_name,
                vm_namespace: data.vm_namespace,
              },
            });
            setShowCreateSchedule(false);
          }}
          isCreating={createVeleroSchedule.isPending || createSnapshotSchedule.isPending}
        />
      )}

      {showCreateBackup && (
        <CreateBackupModal
          onClose={() => setShowCreateBackup(false)}
          onCreate={async (data) => {
            await createBackup.mutateAsync(data);
            setShowCreateBackup(false);
          }}
          isCreating={createBackup.isPending}
        />
      )}

      {restoreBackupItem && (
        <RestoreBackupModal
          backup={restoreBackupItem}
          onClose={() => setRestoreBackupItem(null)}
          onRestore={async (restorePvs) => {
            await restoreBackup.mutateAsync({
              backupName: restoreBackupItem.name,
              data: { restore_pvs: restorePvs },
            });
            setRestoreBackupItem(null);
          }}
          isRestoring={restoreBackup.isPending}
        />
      )}

      {deleteConfirmBackup && (
        <Modal
          isOpen
          onClose={() => setDeleteConfirmBackup(null)}
          title="Delete Backup"
          size="sm"
        >
          <div className="space-y-4">
            <p className="text-sm text-surface-300">
              Delete backup <strong className="font-mono text-surface-100">{deleteConfirmBackup}</strong>?
              This will submit a DeleteBackupRequest to Velero.
            </p>
            <div className="flex justify-end gap-3">
              <button onClick={() => setDeleteConfirmBackup(null)} className="btn-secondary">
                Cancel
              </button>
              <button
                onClick={() => {
                  deleteBackup.mutate(deleteConfirmBackup);
                  setDeleteConfirmBackup(null);
                }}
                disabled={deleteBackup.isPending}
                className="flex items-center gap-2 px-4 py-2 bg-red-900/20 hover:bg-red-900/40 border border-red-800/40 text-red-400 rounded-lg text-sm transition-colors"
              >
                <Trash2 className="w-4 h-4" />
                Delete
              </button>
            </div>
          </div>
        </Modal>
      )}

      {restoreSnapshotItem && (
        <Modal
          isOpen
          onClose={() => setRestoreSnapshotItem(null)}
          title="Restore VM Snapshot"
          size="md"
        >
          <div className="space-y-4">
            <div className="flex items-start gap-3 p-3 bg-amber-900/10 border border-amber-800/30 rounded-lg">
              <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
              <p className="text-sm text-amber-300">
                This will stop VM <strong>{restoreSnapshotItem.vm_name}</strong> in namespace{' '}
                <strong>{restoreSnapshotItem.namespace}</strong>, restore from snapshot{' '}
                <strong>{restoreSnapshotItem.name}</strong>, then restart the VM.
              </p>
            </div>
            <div className="flex justify-end gap-3">
              <button onClick={() => setRestoreSnapshotItem(null)} className="btn-secondary">
                Cancel
              </button>
              <button
                onClick={() => {
                  restoreSnapshot.mutate({
                    namespace: restoreSnapshotItem.namespace,
                    vmName: restoreSnapshotItem.vm_name,
                    snapshotName: restoreSnapshotItem.name,
                  });
                  setRestoreSnapshotItem(null);
                }}
                disabled={restoreSnapshot.isPending}
                className="px-4 py-2 bg-amber-600 hover:bg-amber-500 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-2"
              >
                {restoreSnapshot.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RotateCcw className="w-4 h-4" />
                )}
                Restore
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Add Storage Location Modal */}
      {showAddStorage && (
        <AddStorageModal
          onClose={() => setShowAddStorage(false)}
          onCreate={async (data) => {
            await createStorage.mutateAsync(data);
            setShowAddStorage(false);
          }}
          isPending={createStorage.isPending}
          error={createStorage.error}
        />
      )}
    </div>
  );
}

// ── Add Storage Location Modal ──────────────────────────────────────────────

function AddStorageModal({
  onClose,
  onCreate,
  isPending,
  error,
}: {
  onClose: () => void;
  onCreate: (data: any) => Promise<void>;
  isPending: boolean;
  error: Error | null;
}) {
  const [name, setName] = useState('default');
  const [provider, setProvider] = useState('aws');
  const [bucket, setBucket] = useState('velero');
  const [prefix, setPrefix] = useState('');
  const [region, setRegion] = useState('minio');
  const [s3Url, setS3Url] = useState('http://minio.o0-minio.svc:9000');
  const [forcePathStyle, setForcePathStyle] = useState(true);
  const [credSecret, setCredSecret] = useState('cloud-credentials');
  const [credKey, setCredKey] = useState('cloud');
  const [preset, setPreset] = useState<'minio' | 'aws' | 'custom'>('minio');

  const applyPreset = (p: 'minio' | 'aws' | 'custom') => {
    setPreset(p);
    if (p === 'minio') {
      setProvider('aws');
      setRegion('minio');
      setS3Url('http://minio.o0-minio.svc:9000');
      setForcePathStyle(true);
    } else if (p === 'aws') {
      setProvider('aws');
      setRegion('us-east-1');
      setS3Url('');
      setForcePathStyle(false);
    }
  };

  return (
    <Modal isOpen onClose={onClose} title="Add Backup Storage Location" size="lg">
      <div className="space-y-4">
        {/* Presets */}
        <div className="flex gap-2">
          {[
            { id: 'minio' as const, label: 'MinIO (in-cluster)', desc: 'S3-compatible, default for lab' },
            { id: 'aws' as const, label: 'AWS S3', desc: 'Amazon S3 bucket' },
            { id: 'custom' as const, label: 'Custom S3', desc: 'Any S3-compatible storage' },
          ].map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => applyPreset(p.id)}
              className={clsx(
                'flex-1 p-3 rounded-lg border text-left text-sm transition-colors',
                preset === p.id
                  ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                  : 'border-surface-700 text-surface-400 hover:border-surface-600',
              )}
            >
              <p className="font-medium text-surface-200">{p.label}</p>
              <p className="text-xs mt-0.5">{p.desc}</p>
            </button>
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-surface-400 mb-1">Name</label>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
          <div>
            <label className="block text-xs text-surface-400 mb-1">Bucket</label>
            <input type="text" value={bucket} onChange={(e) => setBucket(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
        </div>

        {preset !== 'aws' && (
          <div>
            <label className="block text-xs text-surface-400 mb-1">S3 Endpoint URL</label>
            <input type="text" value={s3Url} onChange={(e) => setS3Url(e.target.value)}
              placeholder="http://minio.o0-minio.svc:9000"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
        )}

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-surface-400 mb-1">Region</label>
            <input type="text" value={region} onChange={(e) => setRegion(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
          <div>
            <label className="block text-xs text-surface-400 mb-1">Prefix (optional)</label>
            <input type="text" value={prefix} onChange={(e) => setPrefix(e.target.value)}
              placeholder="backups/"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-surface-400 mb-1">Credentials Secret</label>
            <input type="text" value={credSecret} onChange={(e) => setCredSecret(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
          <div>
            <label className="block text-xs text-surface-400 mb-1">Secret Key</label>
            <input type="text" value={credKey} onChange={(e) => setCredKey(e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 font-mono text-sm focus:outline-none focus:border-primary-500" />
          </div>
        </div>

        <div className="p-3 bg-blue-900/10 border border-blue-800/20 rounded-lg flex items-start gap-2">
          <Info className="w-4 h-4 text-blue-400 shrink-0 mt-0.5" />
          <p className="text-xs text-blue-300/80">
            Credentials secret must exist in the Velero namespace with S3 access key and secret key.
            For MinIO: <code className="font-mono">kubectl create secret generic cloud-credentials --from-file=cloud=credentials-file -n velero-namespace</code>
          </p>
        </div>

        {error && (
          <div className="p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
            <p className="text-sm text-red-400">{error.message}</p>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={() => onCreate({
              name,
              provider,
              bucket,
              prefix: prefix || undefined,
              region,
              s3_url: s3Url || undefined,
              s3_force_path_style: forcePathStyle,
              credential_secret: credSecret,
              credential_key: credKey,
              default: true,
            })}
            disabled={!bucket || !name || isPending}
            className="btn-primary"
          >
            {isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            Create Storage Location
          </button>
        </div>
      </div>
    </Modal>
  );
}
