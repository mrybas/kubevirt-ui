interface Props {
  rows?: number;
  columns?: number;
}

export function LoadingSkeleton({ rows = 5, columns = 4 }: Props) {
  return (
    <div className="card overflow-hidden animate-pulse">
      {/* Header row */}
      <div className="flex gap-4 px-4 py-3 bg-surface-800/50 border-b border-surface-800">
        {Array.from({ length: columns }).map((_, i) => (
          <div
            key={i}
            className="h-3 bg-surface-700 rounded"
            style={{ flex: i === 0 ? '2' : '1' }}
          />
        ))}
      </div>
      {/* Data rows */}
      {Array.from({ length: rows }).map((_, row) => (
        <div
          key={row}
          className="flex gap-4 px-4 py-3.5 border-b border-surface-800 last:border-b-0"
        >
          {Array.from({ length: columns }).map((_, col) => (
            <div
              key={col}
              className="h-3.5 bg-surface-800 rounded"
              style={{
                flex: col === 0 ? '2' : '1',
                opacity: 1 - row * 0.12,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
