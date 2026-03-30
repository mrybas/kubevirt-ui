import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';

interface PaginationProps {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  perPage?: number;
  onPerPageChange?: (perPage: number) => void;
  total?: number;
}

const PER_PAGE_OPTIONS = [25, 50, 100];

function pageWindow(current: number, total: number): (number | '...')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | '...')[] = [1];
  if (current > 3) pages.push('...');
  for (let p = Math.max(2, current - 1); p <= Math.min(total - 1, current + 1); p++) {
    pages.push(p);
  }
  if (current < total - 2) pages.push('...');
  pages.push(total);
  return pages;
}

export function Pagination({
  page,
  totalPages,
  onPageChange,
  perPage,
  onPerPageChange,
  total,
}: PaginationProps) {
  if (totalPages <= 1 && !onPerPageChange) return null;

  const start = total && perPage ? (page - 1) * perPage + 1 : null;
  const end = total && perPage ? Math.min(page * perPage, total) : null;

  return (
    <div className="flex items-center justify-between px-4 py-3 border-t border-surface-800">
      {/* Left: showing info + per-page */}
      <div className="flex items-center gap-4">
        {total != null && start != null && end != null && (
          <span className="text-sm text-surface-400">
            Showing {start}–{end} of {total}
          </span>
        )}
        {onPerPageChange && perPage != null && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-surface-500">Per page:</span>
            <div className="flex gap-1">
              {PER_PAGE_OPTIONS.map(opt => (
                <button
                  key={opt}
                  onClick={() => onPerPageChange(opt)}
                  className={`px-2 py-1 text-xs rounded transition-colors ${
                    perPage === opt
                      ? 'bg-primary-600 text-white'
                      : 'text-surface-400 hover:bg-surface-700 hover:text-surface-200'
                  }`}
                >
                  {opt}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Right: page navigation */}
      {totalPages > 1 && (
        <div className="flex items-center gap-1">
          <button
            onClick={() => onPageChange(1)}
            disabled={page === 1}
            className="p-1.5 rounded text-surface-400 hover:bg-surface-700 hover:text-surface-200 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            title="First page"
          >
            <ChevronsLeft className="h-4 w-4" />
          </button>
          <button
            onClick={() => onPageChange(page - 1)}
            disabled={page === 1}
            className="p-1.5 rounded text-surface-400 hover:bg-surface-700 hover:text-surface-200 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            title="Previous page"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>

          {pageWindow(page, totalPages).map((p, idx) =>
            p === '...' ? (
              <span key={`ellipsis-${idx}`} className="px-2 text-surface-500 text-sm">…</span>
            ) : (
              <button
                key={p}
                onClick={() => onPageChange(p)}
                className={`min-w-[2rem] h-8 px-2 text-sm rounded transition-colors ${
                  p === page
                    ? 'bg-primary-600 text-white font-medium'
                    : 'text-surface-400 hover:bg-surface-700 hover:text-surface-200'
                }`}
              >
                {p}
              </button>
            )
          )}

          <button
            onClick={() => onPageChange(page + 1)}
            disabled={page === totalPages}
            className="p-1.5 rounded text-surface-400 hover:bg-surface-700 hover:text-surface-200 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            title="Next page"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
          <button
            onClick={() => onPageChange(totalPages)}
            disabled={page === totalPages}
            className="p-1.5 rounded text-surface-400 hover:bg-surface-700 hover:text-surface-200 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            title="Last page"
          >
            <ChevronsRight className="h-4 w-4" />
          </button>
        </div>
      )}
    </div>
  );
}
