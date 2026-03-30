import { useState, useCallback, useMemo, useRef, type ReactNode } from 'react';
import { ArrowUp, ArrowDown, ArrowUpDown, Search, ChevronRight, ChevronDown } from 'lucide-react';
import { Pagination } from './Pagination';
import { LoadingSkeleton } from './LoadingSkeleton';
import { KebabMenu, type MenuItem } from './KebabMenu';
import { useMediaQuery } from '@/hooks/useMediaQuery';

export interface Column<T> {
  key: string;
  header: string;
  accessor: (item: T) => ReactNode;
  sortable?: boolean;
  width?: string;
  className?: string;
  hideOnMobile?: boolean;
}

interface EmptyState {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}

interface PaginationConfig {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (p: number) => void;
  onPageSizeChange: (s: number) => void;
}

interface BulkAction {
  label: string;
  icon?: ReactNode;
  onClick: (items: unknown[]) => void;
  variant?: 'danger' | 'default';
}

interface DataTableProps<T> {
  columns: Column<T>[];
  data: T[];
  loading?: boolean;
  emptyState?: EmptyState;
  selectable?: boolean;
  onSelectionChange?: (items: T[]) => void;
  actions?: (item: T) => MenuItem[];
  bulkActions?: BulkAction[];
  pagination?: PaginationConfig;
  searchable?: boolean;
  searchPlaceholder?: string;
  onSearch?: (query: string) => void;
  onRowClick?: (item: T) => void;
  keyExtractor: (item: T) => string;
  expandable?: (item: T) => ReactNode;
}

type SortDir = 'asc' | 'desc';

export function DataTable<T>({
  columns,
  data,
  loading,
  emptyState,
  selectable,
  onSelectionChange,
  actions,
  bulkActions,
  pagination,
  searchable,
  searchPlaceholder = 'Search...',
  onSearch,
  onRowClick,
  keyExtractor,
  expandable,
}: DataTableProps<T>) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [searchQuery, setSearchQuery] = useState('');
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const isMobile = useMediaQuery('(max-width: 768px)');
  const visibleColumns = isMobile ? columns.filter(c => !c.hideOnMobile) : columns;

  const handleSearch = useCallback((value: string) => {
    setSearchQuery(value);
    if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    searchTimeoutRef.current = setTimeout(() => onSearch?.(value), 300);
  }, [onSearch]);

  const toggleSort = useCallback((key: string) => {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }, [sortKey]);

  const toggleSelect = useCallback((key: string, _item: T) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      const selectedItems = data.filter(d => next.has(keyExtractor(d)));
      onSelectionChange?.(selectedItems);
      return next;
    });
  }, [data, keyExtractor, onSelectionChange]);

  const toggleAll = useCallback(() => {
    if (selected.size === data.length) {
      setSelected(new Set());
      onSelectionChange?.([]);
    } else {
      const all = new Set(data.map(keyExtractor));
      setSelected(all);
      onSelectionChange?.([...data]);
    }
  }, [data, keyExtractor, onSelectionChange, selected.size]);

  const toggleExpand = useCallback((key: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }, []);

  const selectedItems = useMemo(() =>
    data.filter(d => selected.has(keyExtractor(d))),
    [data, selected, keyExtractor]
  );

  if (loading) {
    return <LoadingSkeleton rows={8} columns={visibleColumns.length} />;
  }

  const totalPages = pagination ? Math.ceil(pagination.total / pagination.pageSize) : 1;

  return (
    <div className="relative">
      {/* Search bar */}
      {searchable && (
        <div className="mb-4">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-surface-500" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => handleSearch(e.target.value)}
              placeholder={searchPlaceholder}
              className="w-full pl-10 pr-4 py-2 bg-surface-800/50 border border-surface-700 rounded-lg text-sm text-surface-200 placeholder-surface-500 focus:outline-none focus:border-primary-500 transition-colors"
            />
          </div>
        </div>
      )}

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-surface-800 bg-surface-800/30">
                {expandable && <th className="w-8 px-2" />}
                {selectable && (
                  <th className="w-10 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={data.length > 0 && selected.size === data.length}
                      onChange={toggleAll}
                      className="rounded border-surface-600 bg-surface-800 text-primary-600 focus:ring-primary-500 focus:ring-offset-0"
                    />
                  </th>
                )}
                {visibleColumns.map(col => (
                  <th
                    key={col.key}
                    className={`px-4 py-3 text-left text-xs font-semibold text-surface-400 uppercase tracking-wider ${col.sortable ? 'cursor-pointer select-none hover:text-surface-200' : ''} ${col.className ?? ''}`}
                    style={col.width ? { width: col.width } : undefined}
                    onClick={col.sortable ? () => toggleSort(col.key) : undefined}
                  >
                    <span className="flex items-center gap-1">
                      {col.header}
                      {col.sortable && (
                        sortKey === col.key
                          ? sortDir === 'asc' ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />
                          : <ArrowUpDown className="h-3 w-3 opacity-30" />
                      )}
                    </span>
                  </th>
                ))}
                {actions && <th className="w-10 px-2" />}
              </tr>
            </thead>
            <tbody>
              {data.length === 0 && emptyState ? (
                <tr>
                  <td
                    colSpan={visibleColumns.length + (selectable ? 1 : 0) + (actions ? 1 : 0) + (expandable ? 1 : 0)}
                    className="py-16"
                  >
                    <div className="flex flex-col items-center gap-3 text-center">
                      <div className="text-surface-500">{emptyState.icon}</div>
                      <div>
                        <p className="text-surface-200 font-medium">{emptyState.title}</p>
                        <p className="text-sm text-surface-500 mt-1">{emptyState.description}</p>
                      </div>
                      {emptyState.action}
                    </div>
                  </td>
                </tr>
              ) : (
                data.map(item => {
                  const key = keyExtractor(item);
                  const isExpanded = expanded.has(key);
                  return (
                    <TableRow
                      key={key}
                      item={item}
                      itemKey={key}
                      columns={visibleColumns}
                      selectable={selectable}
                      selected={selected.has(key)}
                      onSelect={() => toggleSelect(key, item)}
                      actions={actions}
                      onRowClick={onRowClick}
                      expandable={expandable}
                      isExpanded={isExpanded}
                      onToggleExpand={() => toggleExpand(key)}
                      isMobile={isMobile}
                    />
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {pagination && (
          <Pagination
            page={pagination.page}
            totalPages={totalPages}
            onPageChange={pagination.onPageChange}
            perPage={pagination.pageSize}
            onPerPageChange={pagination.onPageSizeChange}
            total={pagination.total}
          />
        )}
      </div>

      {/* Bulk actions bar */}
      {selectable && bulkActions && selectedItems.length > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-surface-800 border border-surface-700 rounded-xl px-5 py-3 shadow-2xl">
          <span className="text-sm text-surface-300 font-medium">
            {selectedItems.length} selected
          </span>
          <div className="w-px h-5 bg-surface-700" />
          {bulkActions.map(action => (
            <button
              key={action.label}
              onClick={() => action.onClick(selectedItems)}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                action.variant === 'danger'
                  ? 'text-red-400 hover:bg-red-500/10'
                  : 'text-surface-200 hover:bg-surface-700'
              }`}
            >
              {action.icon && <span className="h-4 w-4">{action.icon}</span>}
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Extracted row component to keep JSX readable
function TableRow<T>({
  item,
  itemKey: _itemKey,
  columns,
  selectable,
  selected,
  onSelect,
  actions,
  onRowClick,
  expandable,
  isExpanded,
  onToggleExpand,
  isMobile,
}: {
  item: T;
  itemKey: string;
  columns: Column<T>[];
  selectable?: boolean;
  selected: boolean;
  onSelect: () => void;
  actions?: (item: T) => MenuItem[];
  onRowClick?: (item: T) => void;
  expandable?: (item: T) => ReactNode;
  isExpanded: boolean;
  onToggleExpand: () => void;
  isMobile?: boolean;
}) {
  const expandedContent = expandable?.(item);
  const hasExpand = !!expandable && !!expandedContent;

  return (
    <>
      <tr
        className={`h-10 border-b border-surface-800 last:border-b-0 transition-colors ${
          onRowClick ? 'cursor-pointer' : ''
        } ${selected ? 'bg-primary-600/10' : 'hover:bg-surface-800/50'}`}
        onClick={() => onRowClick?.(item)}
      >
        {expandable && (
          <td className="px-2">
            {hasExpand && (
              <button
                onClick={(e) => { e.stopPropagation(); onToggleExpand(); }}
                className="p-1 rounded text-surface-400 hover:text-surface-200 transition-colors"
              >
                {isExpanded
                  ? <ChevronDown className="h-4 w-4" />
                  : <ChevronRight className="h-4 w-4" />
                }
              </button>
            )}
          </td>
        )}
        {selectable && (
          <td className="px-4" onClick={e => e.stopPropagation()}>
            <input
              type="checkbox"
              checked={selected}
              onChange={onSelect}
              className="rounded border-surface-600 bg-surface-800 text-primary-600 focus:ring-primary-500 focus:ring-offset-0"
            />
          </td>
        )}
        {columns.map(col => (
          <td
            key={col.key}
            className={`${isMobile ? 'px-2 py-1.5 text-xs' : 'px-4 py-2 text-sm'} text-surface-200 ${col.className ?? ''}`}
            style={col.width ? { width: col.width } : undefined}
          >
            {col.accessor(item)}
          </td>
        ))}
        {actions && (
          <td className="px-2" onClick={e => e.stopPropagation()}>
            <KebabMenu items={actions(item)} />
          </td>
        )}
      </tr>
      {isExpanded && hasExpand && (
        <tr className="bg-surface-900/50">
          <td
            colSpan={columns.length + (selectable ? 1 : 0) + (actions ? 1 : 0) + 1}
            className="px-8 py-4"
          >
            {expandedContent}
          </td>
        </tr>
      )}
    </>
  );
}
