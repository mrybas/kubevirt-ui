/**
 * GroupPicker — LDAP-backed autocomplete for selecting groups.
 *
 * Renders selected groups as removable chips + an input field.
 * Debounces LDAP search by 300 ms.
 *
 * Free-text fallback (LDAP unreachable or no match):
 *   - "Group not found? You can still enter the exact name." hint is shown.
 *   - Press Enter or click "Add exact name" to add the raw string as a chip.
 *   - Only validation: non-empty, not already selected.
 */

import { useState, useRef, useEffect } from 'react';
import { Search, X, Loader2, AlertCircle, CornerDownLeft } from 'lucide-react';
import clsx from 'clsx';
import { useLdapGroupSearch } from '@/hooks/useLdap';

interface GroupPickerProps {
  /** Currently selected group names */
  groups: string[];
  /** Called with the new list whenever a group is added or removed */
  onChange: (groups: string[]) => void;
  placeholder?: string;
  /** Disables all interaction (view-only mode) */
  readOnly?: boolean;
}

export function GroupPicker({
  groups,
  onChange,
  placeholder = 'Search groups…',
  readOnly = false,
}: GroupPickerProps) {
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [isOpen, setIsOpen] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Debounce input → LDAP query
  const handleQueryChange = (value: string) => {
    setQuery(value);
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setDebouncedQuery(value), 300);
  };

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => clearTimeout(timerRef.current);
  }, []);

  // Close dropdown on outside click
  useEffect(() => {
    function handleOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleOutside);
    return () => document.removeEventListener('mousedown', handleOutside);
  }, []);

  const { data: results, isLoading, isError } = useLdapGroupSearch(debouncedQuery);

  // Filter out already-selected groups
  const suggestions = (results ?? []).filter((g) => !groups.includes(g));

  /** Add a group by name (from dropdown or free-text). Clears the input. */
  const handleAdd = (group: string) => {
    const trimmed = group.trim();
    if (!trimmed || groups.includes(trimmed)) return;
    onChange([...groups, trimmed]);
    setQuery('');
    setDebouncedQuery('');
    setIsOpen(false);
    inputRef.current?.focus();
  };

  const handleRemove = (group: string) => {
    onChange(groups.filter((g) => g !== group));
  };

  /**
   * Enter key logic:
   *  - If exactly one LDAP suggestion → pick it (unambiguous).
   *  - If no suggestions (or LDAP error) and query is non-empty → free-text add.
   *  - Multiple suggestions → do nothing (user should click one).
   */
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (suggestions.length === 1) {
      handleAdd(suggestions[0]);
    } else if ((suggestions.length === 0 || isError) && query.trim()) {
      handleAdd(query);
    }
  };

  const showDropdown = isOpen && debouncedQuery.trim().length >= 2;

  /** True when we should offer the free-text fallback inside the dropdown. */
  const showFreeTextFallback =
    showDropdown && !isLoading && (isError || suggestions.length === 0);

  return (
    <div ref={containerRef} className="space-y-2">
      {/* Selected chips */}
      {groups.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {groups.map((group) => (
            <span
              key={group}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-primary-600/20 text-primary-300 border border-primary-600/30"
            >
              {group}
              {!readOnly && (
                <button
                  type="button"
                  onClick={() => handleRemove(group)}
                  className="ml-0.5 rounded-full hover:bg-primary-500/30 transition-colors p-0.5"
                  aria-label={`Remove ${group}`}
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}

      {/* Search input */}
      {!readOnly && (
        <div className="relative">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-surface-500 pointer-events-none" />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => handleQueryChange(e.target.value)}
              onFocus={() => setIsOpen(true)}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
              className="input w-full pl-8 pr-8 text-sm"
              autoComplete="off"
            />
            {isLoading ? (
              <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-surface-500 animate-spin pointer-events-none" />
            ) : query.trim() && !isLoading ? (
              /* Enter-to-add hint icon when free-text is applicable */
              showFreeTextFallback ? (
                <CornerDownLeft className="absolute right-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-surface-500 pointer-events-none" />
              ) : null
            ) : null}
          </div>

          {/* Dropdown */}
          {showDropdown && (
            <div className="absolute z-50 mt-1 w-full bg-surface-800 border border-surface-700 rounded-lg shadow-xl overflow-hidden">
              {isLoading ? (
                <div className="flex items-center gap-2 px-3 py-2.5 text-sm text-surface-400">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Searching…
                </div>
              ) : (
                <>
                  {/* LDAP error banner */}
                  {isError && (
                    <div className="flex items-center gap-2 px-3 py-2 text-xs text-amber-400 border-b border-surface-700 bg-amber-900/10">
                      <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                      LDAP unavailable
                    </div>
                  )}

                  {/* LDAP suggestions list */}
                  {suggestions.length > 0 && (
                    <ul className="max-h-48 overflow-y-auto py-1">
                      {suggestions.map((group) => (
                        <li key={group}>
                          <button
                            type="button"
                            onClick={() => handleAdd(group)}
                            className={clsx(
                              'w-full text-left px-3 py-2 text-sm transition-colors',
                              'text-surface-300 hover:bg-surface-700 hover:text-surface-100'
                            )}
                          >
                            {group}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  {/* Free-text fallback — shown when no LDAP results or error */}
                  {showFreeTextFallback && (
                    <div className="border-t border-surface-700/60 px-3 py-2.5 space-y-2">
                      {!isError && (
                        <p className="text-xs text-surface-500">
                          No groups found for &ldquo;{debouncedQuery}&rdquo;
                        </p>
                      )}
                      <p className="text-xs text-surface-400">
                        Group not found? You can still enter the exact name.
                      </p>
                      <button
                        type="button"
                        onClick={() => handleAdd(query)}
                        disabled={!query.trim() || groups.includes(query.trim())}
                        className={clsx(
                          'flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg transition-colors w-full',
                          'border border-surface-600 text-surface-300 hover:bg-surface-700 hover:text-surface-100',
                          'disabled:opacity-40 disabled:cursor-not-allowed'
                        )}
                      >
                        <CornerDownLeft className="w-3 h-3 flex-shrink-0" />
                        Add &ldquo;{query}&rdquo; as exact name
                      </button>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
