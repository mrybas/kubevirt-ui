/**
 * Quota, in one place.
 *
 * The numbers lived in three places that never met: the folder's own ceiling
 * showed as a small read-only panel on Overview and was edited through the
 * Edit Folder dialog, the environments' quotas were shown nowhere at all
 * (`PUT /folders/{name}/environments/{env}/quota` existed and had no caller),
 * and what a sub-folder reserves out of the parent was visible only by
 * opening it. So the one question this page has to answer — how much of my
 * ceiling is spoken for, and by whom — could not be asked.
 *
 * This tab is that answer: the ceiling with what is allocated under it, then
 * every claimant on it, each editable where it is shown.
 *
 * A folder quota is a budget the create-VM wizard reads, not a limit the
 * cluster enforces; an environment quota is a real ResourceQuota and binds
 * kubectl too. The tab says so rather than letting the two look alike.
 */

import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Gauge, Pencil, Check, X, FolderOpen, Layers, ChevronRight } from 'lucide-react';
import clsx from 'clsx';
import {
  useFolderQuotaHeadroom,
  useUpdateFolder,
  useSetEnvironmentQuota,
} from '../../hooks/useFolders';
import { formatBytes, parseQuantity, trimNumber } from '../../utils/quantity';
import type { Folder } from '../../types/folder';

type Dim = 'cpu' | 'memory' | 'storage';

const DIMS: { dim: Dim; label: string; hint: string }[] = [
  { dim: 'cpu', label: 'CPU', hint: 'e.g. 16' },
  { dim: 'memory', label: 'Memory', hint: 'e.g. 32Gi' },
  { dim: 'storage', label: 'Storage', hint: 'e.g. 200Gi' },
];

type Draft = Record<Dim, string>;

const EMPTY: Draft = { cpu: '', memory: '', storage: '' };

/** Cores read as numbers, the other two as bytes. */
function show(dim: Dim, value: number | null): string {
  if (value === null) return '—';
  return dim === 'cpu' ? trimNumber(value) : formatBytes(value);
}

function reason(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Only the dimensions that were filled in; all empty means no quota. */
function quotaOf(draft: Draft): { cpu?: string; memory?: string; storage?: string } {
  const out: Record<string, string> = {};
  for (const { dim } of DIMS) if (draft[dim].trim()) out[dim] = draft[dim].trim();
  return out;
}

export function FolderQuotaTab({ folder }: { folder: Folder }) {
  const { data: headroom } = useFolderQuotaHeadroom(folder.name);
  const updateFolder = useUpdateFolder();
  const setEnvQuota = useSetEnvironmentQuota(folder.name);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [error, setError] = useState<string | null>(null);

  const [editingEnv, setEditingEnv] = useState<string | null>(null);
  const [envDraft, setEnvDraft] = useState<Draft>(EMPTY);
  const [envError, setEnvError] = useState<string | null>(null);

  const limit = (dim: Dim) => parseQuantity(folder.quota?.[dim] ?? null);
  const allocated = (dim: Dim) => headroom?.allocated?.[dim] ?? null;
  const free = (dim: Dim) => headroom?.free?.[dim] ?? null;

  const startEdit = () => {
    setDraft({
      cpu: folder.quota?.cpu ?? '',
      memory: folder.quota?.memory ?? '',
      storage: folder.quota?.storage ?? '',
    });
    setError(null);
    setEditing(true);
  };

  const save = async () => {
    setError(null);
    try {
      await updateFolder.mutateAsync({
        name: folder.name,
        request: { quota: quotaOf(draft) },
      });
      setEditing(false);
    } catch (e) {
      setError(reason(e));
    }
  };

  const startEnvEdit = (env: Folder['environments'][number]) => {
    setEnvDraft({
      cpu: env.quota_cpu ?? '',
      memory: env.quota_memory ?? '',
      storage: env.quota_storage ?? '',
    });
    setEnvError(null);
    setEditingEnv(env.environment);
  };

  const saveEnv = async (environment: string) => {
    setEnvError(null);
    try {
      await setEnvQuota.mutateAsync({ environment, quota: quotaOf(envDraft) });
      setEditingEnv(null);
    } catch (e) {
      setEnvError(reason(e));
    }
  };

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------ the ceiling */}
      <div className="card">
        <div className="card-body space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="font-medium text-surface-100 flex items-center gap-2">
              <Gauge className="h-4 w-4 text-surface-400" />
              Folder quota
            </h3>
            {editing ? (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setEditing(false)}
                  className="btn-secondary text-sm py-1"
                >
                  <X className="h-3.5 w-3.5" /> Cancel
                </button>
                <button
                  onClick={save}
                  disabled={updateFolder.isPending}
                  className="btn-primary text-sm py-1"
                >
                  <Check className="h-3.5 w-3.5" />
                  {updateFolder.isPending ? 'Saving…' : 'Save'}
                </button>
              </div>
            ) : (
              <button
                onClick={startEdit}
                className="flex items-center gap-1.5 text-sm text-surface-400 hover:text-primary-400 transition-colors"
              >
                <Pencil className="h-3.5 w-3.5" />
                Edit quota
              </button>
            )}
          </div>

          {editing ? (
            <div className="space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {DIMS.map(({ dim, label, hint }) => (
                  <div key={dim}>
                    <label className="block text-xs text-surface-400 mb-1">{label}</label>
                    <input
                      type="text"
                      className="input w-full"
                      aria-label={`Quota ${label.toLowerCase() === 'cpu' ? 'CPU' : label.toLowerCase()}`}
                      placeholder={hint}
                      value={draft[dim]}
                      onChange={(e) => setDraft({ ...draft, [dim]: e.target.value })}
                    />
                  </div>
                ))}
              </div>
              <p className="text-xs text-surface-500">
                A budget shown when creating VMs in this folder, not a limit the
                cluster enforces. Leave all three empty for no quota.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {DIMS.map(({ dim, label }) => {
                const cap = limit(dim);
                const used = allocated(dim) ?? 0;
                const pct = cap && cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
                const over = cap !== null && used > cap;
                return (
                  <div key={dim} className="space-y-1.5">
                    <div className="flex items-baseline justify-between text-sm">
                      <span className="text-surface-400">{label}</span>
                      <span className="text-surface-200">
                        {cap === null ? (
                          <span className="text-surface-500">not capped</span>
                        ) : (
                          <>
                            <span className={clsx(over && 'text-amber-400')}>
                              {show(dim, used)}
                            </span>
                            <span className="text-surface-500"> of </span>
                            {show(dim, cap)}
                            <span className="text-surface-500">
                              {' '}· {show(dim, free(dim))} free
                            </span>
                          </>
                        )}
                      </span>
                    </div>
                    <div className="h-1.5 rounded-full bg-surface-700 overflow-hidden">
                      <div
                        className={clsx(
                          'h-full rounded-full transition-all',
                          over ? 'bg-amber-500' : 'bg-primary-500',
                        )}
                        style={{ width: `${cap === null ? 0 : pct}%` }}
                      />
                    </div>
                  </div>
                );
              })}
              {!folder.quota && (
                <p className="text-sm text-surface-500">
                  No quota configured — this folder takes as much as its parent allows.
                </p>
              )}
            </div>
          )}

          {error && <p className="text-sm text-red-400">{error}</p>}
        </div>
      </div>

      {/* ------------------------------------------------ who holds it */}
      <div className="card">
        <div className="card-body space-y-3">
          <h3 className="font-medium text-surface-100">Allocated to</h3>

          {folder.environments.length === 0 && (folder.children ?? []).length === 0 ? (
            <p className="text-sm text-surface-500">
              Nothing claims this quota yet — add an environment or a sub-folder.
            </p>
          ) : (
            <div className="divide-y divide-surface-700 border border-surface-700 rounded-xl overflow-hidden">
              {folder.environments.map((env) => {
                const isEditing = editingEnv === env.environment;
                return (
                  <div key={env.name} className="px-4 py-3 bg-surface-800/50 space-y-2">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex items-center gap-2 min-w-0">
                        <Layers className="h-3.5 w-3.5 text-surface-500 shrink-0" />
                        <span className="text-sm text-surface-200">{env.environment}</span>
                        <span className="text-xs text-surface-500 font-mono truncate">
                          {env.name}
                        </span>
                      </div>
                      {isEditing ? (
                        <div className="flex items-center gap-2 shrink-0">
                          <button
                            onClick={() => setEditingEnv(null)}
                            className="btn-secondary text-xs py-1"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => saveEnv(env.environment)}
                            disabled={setEnvQuota.isPending}
                            className="btn-primary text-xs py-1"
                          >
                            {setEnvQuota.isPending ? 'Saving…' : 'Save'}
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => startEnvEdit(env)}
                          className="p-1 text-surface-500 hover:text-primary-400 rounded transition-colors shrink-0"
                          title={`Edit ${env.environment} quota`}
                          aria-label={`Edit ${env.environment} quota`}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>

                    {isEditing ? (
                      <div className="grid grid-cols-3 gap-2">
                        {DIMS.map(({ dim, label, hint }) => (
                          <input
                            key={dim}
                            type="text"
                            className="input w-full text-sm"
                            aria-label={`${env.environment} ${label.toLowerCase() === 'cpu' ? 'CPU' : label.toLowerCase()} quota`}
                            placeholder={hint}
                            value={envDraft[dim]}
                            onChange={(e) => setEnvDraft({ ...envDraft, [dim]: e.target.value })}
                          />
                        ))}
                      </div>
                    ) : (
                      <div className="grid grid-cols-3 gap-2 text-xs">
                        {DIMS.map(({ dim, label }) => {
                          const cap = parseQuantity(
                            env[`quota_${dim}` as 'quota_cpu' | 'quota_memory' | 'quota_storage'],
                          );
                          const used = parseQuantity(
                            env[`used_${dim}` as 'used_cpu' | 'used_memory' | 'used_storage'] ?? null,
                          );
                          return (
                            <div key={dim} className="flex items-baseline gap-1.5">
                              <span className="text-surface-500">{label}</span>
                              <span className="text-surface-200">
                                {cap === null ? 'none' : show(dim, cap)}
                              </span>
                              {used !== null && (
                                <span className="text-surface-500">({show(dim, used)} used)</span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}

              {(folder.children ?? []).map((child) => (
                <div
                  key={child.name}
                  className="px-4 py-3 bg-surface-800/50 flex items-center justify-between gap-3"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FolderOpen className="h-3.5 w-3.5 text-surface-500 shrink-0" />
                    <span className="text-sm text-surface-200 truncate">
                      {child.display_name || child.name}
                    </span>
                    <span className="text-xs text-surface-500">sub-folder</span>
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="hidden sm:flex items-center gap-3 text-xs">
                      {DIMS.map(({ dim, label }) => {
                        const cap = parseQuantity(child.quota?.[dim] ?? null);
                        return (
                          <span key={dim} className="text-surface-500">
                            {label}{' '}
                            <span className="text-surface-200">
                              {cap === null ? 'none' : show(dim, cap)}
                            </span>
                          </span>
                        );
                      })}
                    </div>
                    <NavLink
                      to={`/folders/${child.name}`}
                      className="text-xs text-primary-400 hover:text-primary-300 flex items-center gap-1 shrink-0"
                    >
                      Edit there <ChevronRight className="h-3 w-3" />
                    </NavLink>
                  </div>
                </div>
              ))}
            </div>
          )}

          {envError && <p className="text-sm text-red-400">{envError}</p>}

          <p className="text-xs text-surface-500">
            An environment quota is a Kubernetes ResourceQuota — it binds
            everything in that namespace, including kubectl. A sub-folder's
            quota is reserved out of this folder whether or not anything runs
            in it; change it on its own page.
          </p>
        </div>
      </div>
    </div>
  );
}
