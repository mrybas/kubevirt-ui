/**
 * Custom styled dropdown select component.
 * Replaces native <select> with a styled dropdown matching the app design.
 */

import { useState, useRef, useEffect } from 'react';
import { ChevronDown } from 'lucide-react';
import clsx from 'clsx';

export interface SelectOption {
  value: string;
  label: string;
  group?: string;
}

interface CustomSelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}

export function CustomSelect({
  value,
  onChange,
  options,
  placeholder = 'Select...',
  className,
  disabled = false,
}: CustomSelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [search, setSearch] = useState('');
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Close on click outside
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setIsOpen(false);
        setSearch('');
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  // Focus search when opened
  useEffect(() => {
    if (isOpen && inputRef.current) {
      inputRef.current.focus();
    }
  }, [isOpen]);

  const selectedOption = options.find((o) => o.value === value);
  const displayLabel = selectedOption?.label || placeholder;

  // Filter options by search
  const filtered = search
    ? options.filter((o) => o.label.toLowerCase().includes(search.toLowerCase()))
    : options;

  // Group options
  const groups = new Map<string, SelectOption[]>();
  const ungrouped: SelectOption[] = [];
  for (const opt of filtered) {
    if (opt.group) {
      if (!groups.has(opt.group)) groups.set(opt.group, []);
      groups.get(opt.group)!.push(opt);
    } else {
      ungrouped.push(opt);
    }
  }

  const handleSelect = (val: string) => {
    onChange(val);
    setIsOpen(false);
    setSearch('');
  };

  return (
    <div ref={ref} className={clsx('relative', className)}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => !disabled && setIsOpen(!isOpen)}
        className={clsx(
          'flex items-center justify-between w-full px-3 py-2 text-sm rounded-lg border transition-colors text-left',
          'bg-surface-800 border-surface-700 text-surface-100',
          'hover:border-surface-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50 focus:border-primary-500',
          disabled && 'opacity-50 cursor-not-allowed',
          isOpen && 'ring-2 ring-primary-500/50 border-primary-500'
        )}
      >
        <span className={clsx(!selectedOption && 'text-surface-500')}>
          {displayLabel}
        </span>
        <ChevronDown className={clsx('h-4 w-4 text-surface-400 transition-transform', isOpen && 'rotate-180')} />
      </button>

      {isOpen && (
        <div className="absolute z-50 mt-1 w-full bg-surface-800 border border-surface-700 rounded-lg shadow-xl overflow-hidden">
          {/* Search input (show only if > 5 options) */}
          {options.length > 5 && (
            <div className="p-2 border-b border-surface-700">
              <input
                ref={inputRef}
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search..."
                className="w-full px-2 py-1.5 text-sm bg-surface-900 border border-surface-600 rounded text-surface-100 placeholder-surface-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
              />
            </div>
          )}

          <div className="max-h-60 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-3 py-2 text-sm text-surface-500">No options</div>
            ) : (
              <>
                {/* Ungrouped options */}
                {ungrouped.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => handleSelect(opt.value)}
                    className={clsx(
                      'w-full text-left px-3 py-2 text-sm transition-colors',
                      opt.value === value
                        ? 'bg-primary-500/10 text-primary-400'
                        : 'text-surface-300 hover:bg-surface-700 hover:text-surface-100'
                    )}
                  >
                    {opt.label}
                  </button>
                ))}

                {/* Grouped options */}
                {[...groups.entries()].map(([group, opts]) => (
                  <div key={group}>
                    <div className="px-3 py-1.5 text-xs font-semibold text-surface-500 uppercase tracking-wider bg-surface-900/50">
                      {group}
                    </div>
                    {opts.map((opt) => (
                      <button
                        key={opt.value}
                        type="button"
                        onClick={() => handleSelect(opt.value)}
                        className={clsx(
                          'w-full text-left px-3 py-2 text-sm pl-5 transition-colors',
                          opt.value === value
                            ? 'bg-primary-500/10 text-primary-400'
                            : 'text-surface-300 hover:bg-surface-700 hover:text-surface-100'
                        )}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
