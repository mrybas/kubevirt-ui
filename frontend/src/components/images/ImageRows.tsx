/**
 * Row rendering for the Storage page's Images table.
 *
 * Two origins:
 * - "cluster": a real disk. It may carry `catalog_ref` as provenance from
 *   Harbor, but that is not a reason to show it twice — the backend has
 *   already merged the catalogue entry into this row.
 * - "catalog": a Harbor artifact that has not been materialised into a disk
 *   yet. Booting from it means an import runs first, so it is never shown as
 *   an immediately-usable "Ready" disk — its action is "Create disk", not
 *   "use".
 *
 * When `catalogAvailable` is false (Harbor is down, or the caller's token was
 * rejected) this renders a non-blocking `role="status"` banner ABOVE the
 * rows, never in place of them. The cluster rows are still complete and
 * correct, and a registry outage must not stop someone booting a VM from a
 * disk they already have.
 */
import clsx from 'clsx';
import {
  AlertTriangle,
  CheckCircle2,
  CircleDot,
  Clock,
  Cloud,
  Database,
  Image as ImageIcon,
  Layers,
  Monitor,
  Trash2,
} from 'lucide-react';

export interface ImageRowItem {
  name: string;
  namespace?: string;
  display_name?: string;
  status: string;
  origin?: 'cluster' | 'catalog';
  catalog_ref?: string | null;
  size?: string | null;
  used_by?: string[] | null;
  environment?: string;
  scope?: string;
  disk_type?: 'image' | 'data';
  error_message?: string;
}

type DisplayStatus = 'Ready' | 'InUse' | 'Pending' | 'Error' | 'Catalog';

/** Same mapping Storage.tsx used, plus the new catalog-origin case. */
function displayStatus(item: ImageRowItem): DisplayStatus {
  if (item.origin === 'catalog') return 'Catalog';
  const status = (item.status || '').toLowerCase();
  if (status === 'error' || status === 'failed') return 'Error';
  if (item.used_by && item.used_by.length > 0) return 'InUse';
  if (status === 'succeeded' || status === 'ready') return 'Ready';
  if (
    status.includes('progress') ||
    status.includes('pending') ||
    status.includes('scheduled') ||
    status === 'waitforfirstconsumer'
  ) {
    return 'Pending';
  }
  return 'Ready';
}

const STATUS_STYLES: Record<DisplayStatus, string> = {
  Ready: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
  InUse: 'bg-primary-500/10 text-primary-400 border-primary-500/30',
  Pending: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  Error: 'bg-red-500/10 text-red-400 border-red-500/30',
  Catalog: 'bg-surface-500/10 text-surface-300 border-surface-500/30',
};

function StatusIcon({ status }: { status: DisplayStatus }) {
  switch (status) {
    case 'Ready':
      return <CheckCircle2 className="h-4 w-4 text-emerald-400" />;
    case 'InUse':
      return <CircleDot className="h-4 w-4 text-primary-400" />;
    case 'Pending':
      return <Clock className="h-4 w-4 text-amber-400" />;
    case 'Error':
      return <AlertTriangle className="h-4 w-4 text-red-400" />;
    case 'Catalog':
      return <Cloud className="h-4 w-4 text-surface-300" />;
  }
}

function StatusBadge({ status }: { status: DisplayStatus }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border',
        STATUS_STYLES[status]
      )}
    >
      <StatusIcon status={status} />
      {status === 'Catalog' ? 'In catalog' : status}
    </span>
  );
}

export interface ImageRowsProps {
  items: ImageRowItem[];
  catalogAvailable: boolean;
  /** Number of columns in the surrounding <table>, for the banner's colSpan. */
  colSpan?: number;
  onRowClick?: (item: ImageRowItem) => void;
  onDelete?: (item: ImageRowItem) => void;
  /** Called from the "Create disk" action on a catalogue-origin row. */
  onCreateFromCatalog?: (item: ImageRowItem) => void;
}

export function ImageRows({
  items,
  catalogAvailable,
  colSpan = 7,
  onRowClick,
  onDelete,
  onCreateFromCatalog,
}: ImageRowsProps) {
  return (
    <>
      {!catalogAvailable && (
        <tr>
          <td colSpan={colSpan} className="p-0">
            <div
              role="status"
              className="flex items-center gap-2 px-4 py-2 text-sm text-amber-300 bg-amber-500/10 border-b border-amber-500/20"
            >
              <AlertTriangle className="h-4 w-4 shrink-0" />
              The image catalog could not be reached. Disks you already have are
              shown below; catalog-only images may be missing until it recovers.
            </div>
          </td>
        </tr>
      )}
      {items.map((item) => {
        const status = displayStatus(item);
        const isCatalog = item.origin === 'catalog';
        const vmCount = item.used_by?.length ?? 0;
        return (
          <tr
            key={`${item.namespace ?? ''}-${item.name}`}
            data-testid={isCatalog ? 'origin-catalog' : 'origin-cluster'}
            className={clsx('hover:bg-surface-800/30', !isCatalog && onRowClick && 'cursor-pointer')}
            onClick={!isCatalog && onRowClick ? () => onRowClick(item) : undefined}
          >
            <td className="table-cell">
              <div className="flex items-center gap-3">
                <div className={clsx('p-2 rounded-lg', isCatalog ? 'bg-surface-700' : 'bg-amber-500/10')}>
                  {isCatalog ? (
                    <Cloud className="h-4 w-4 text-surface-300" />
                  ) : item.disk_type === 'data' ? (
                    <Database className="h-4 w-4 text-blue-400" />
                  ) : (
                    <ImageIcon className="h-4 w-4 text-amber-400" />
                  )}
                </div>
                <div>
                  <p className="font-medium text-surface-100">{item.display_name || item.name}</p>
                  {item.display_name && item.display_name !== item.name && (
                    <p className="text-xs text-surface-500">{item.name}</p>
                  )}
                </div>
              </div>
            </td>
            <td className="table-cell">
              <span className="text-surface-400 text-xs font-mono">{item.namespace || '-'}</span>
            </td>
            <td className="table-cell">
              <span className="text-surface-300 text-sm">{item.size || '-'}</span>
            </td>
            <td className="table-cell">
              <div title={status === 'Error' && item.error_message ? item.error_message : undefined}>
                <StatusBadge status={status} />
              </div>
            </td>
            <td className="table-cell">
              {vmCount > 0 ? (
                <span className="inline-flex items-center gap-1.5 text-sm text-primary-400">
                  <Monitor className="h-3.5 w-3.5" />
                  {vmCount} VM{vmCount > 1 ? 's' : ''}
                </span>
              ) : (
                <span className="text-surface-500 text-sm">-</span>
              )}
            </td>
            <td className="table-cell">
              {/* The Scope column, as Storage.tsx rendered it before the
                  Images tab moved here. A project-scoped disk gets the cyan
                  "All envs" badge; only an environment-scoped one shows its
                  environment name. Collapsing both into `item.environment`
                  quietly removed that badge from the Images tab while the
                  Data Disks tab beside it kept showing it — the same disk,
                  two tabs, two different answers about where it lives, with
                  the flag OFF and Harbor nowhere in the picture. */}
              {isCatalog ? (
                <span className="text-surface-400 text-xs">Catalog</span>
              ) : item.scope === 'project' ? (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-cyan-500/10 text-cyan-400">
                  <Layers className="h-3 w-3" />
                  All envs
                </span>
              ) : (
                <span className="text-surface-400 text-xs">{item.environment || 'env'}</span>
              )}
            </td>
            <td className="table-cell text-right">
              {isCatalog ? (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onCreateFromCatalog?.(item);
                  }}
                  className="btn-secondary !py-1 !px-2.5 text-xs"
                  title="Import this catalog image and create a disk from it"
                >
                  Create disk
                </button>
              ) : (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete?.(item);
                  }}
                  disabled={status === 'InUse'}
                  className={clsx(
                    'p-1.5 rounded-lg',
                    status === 'InUse'
                      ? 'text-surface-600 cursor-not-allowed'
                      : 'text-surface-400 hover:text-red-400 hover:bg-red-500/10'
                  )}
                  title={status === 'InUse' ? 'Cannot delete - in use' : 'Delete'}
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              )}
            </td>
          </tr>
        );
      })}
    </>
  );
}
