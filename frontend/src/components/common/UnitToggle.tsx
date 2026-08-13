import clsx from 'clsx';
import type { ByteUnit } from '@/utils/quantity';

/**
 * Mi/Gi, as two buttons — a two-option select is a worse control than a pair.
 *
 * Memory and storage are entered as a number plus a unit everywhere they are
 * entered at all: a raw byte count is unreadable in a field and unusable on a
 * slider.
 */
export function UnitToggle({
  value, onChange, label,
}: {
  value: ByteUnit;
  onChange: (u: ByteUnit) => void;
  label: string;
}) {
  return (
    <div
      className="flex shrink-0 rounded-lg border border-surface-600 overflow-hidden"
      role="group"
      aria-label={label}
    >
      {(['Mi', 'Gi'] as ByteUnit[]).map(u => (
        <button
          key={u}
          type="button"
          onClick={() => onChange(u)}
          aria-pressed={value === u}
          className={clsx(
            'px-2 text-xs',
            value === u
              ? 'bg-primary-600 text-white'
              : 'bg-surface-900 text-surface-400 hover:text-surface-200',
          )}
        >
          {u}
        </button>
      ))}
    </div>
  );
}
