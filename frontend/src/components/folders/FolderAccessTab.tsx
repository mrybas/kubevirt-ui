/**
 * FolderAccessTab — Phase 2 LDAP-group-based authorization UI.
 *
 * Section 1: Folder-wide access (Admins / Members / Viewers)
 *   Groups listed here have that role in ALL environments of this folder.
 *
 * Section 2: Per-environment access (collapsible per env)
 *   Groups listed here get additional access for that specific env only.
 *   Additive to folder-wide access (union).
 *
 * Saves via PATCH /folders/{name}/access (partial body — omit = keep current).
 * Legacy folders (access === null) show an informational banner before first save.
 */

import { useState, useCallback } from 'react';
import {
  Shield,
  Pencil,
  Eye,
  ChevronDown,
  ChevronRight,
  Loader2,
  Save,
  Info,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import { GroupPicker } from './GroupPicker';
import { usePatchFolderAccess } from '@/hooks/useFolders';
import { notify } from '@/store/notifications';
import type { Folder, AccessBlock, EnvAccess } from '@/types/folder';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface RoleSection {
  key: keyof EnvAccess;
  label: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  chipColor: string;
  /** Show inline confirm before removing a group (admin lists) */
  requireConfirm: boolean;
}

const ROLE_SECTIONS: RoleSection[] = [
  {
    key: 'admins',
    label: 'Admins',
    description: 'Full access — can manage folder access and all resources',
    icon: Shield,
    color: 'text-red-400',
    chipColor: 'bg-red-900/20 text-red-300 border-red-800/40',
    requireConfirm: true,
  },
  {
    key: 'members',
    label: 'Members',
    description: 'Create, edit, delete VMs and storage',
    icon: Pencil,
    color: 'text-amber-400',
    chipColor: 'bg-amber-900/20 text-amber-300 border-amber-800/40',
    requireConfirm: false,
  },
  {
    key: 'viewers',
    label: 'Viewers',
    description: 'Read-only access to all resources',
    icon: Eye,
    color: 'text-surface-400',
    chipColor: 'bg-surface-700 text-surface-300 border-surface-600',
    requireConfirm: false,
  },
];

const EMPTY_ENV_ACCESS: EnvAccess = { admins: [], members: [], viewers: [] };

function initAccess(folder: Folder): AccessBlock {
  return {
    admins: folder.access?.admins ?? [],
    members: folder.access?.members ?? [],
    viewers: folder.access?.viewers ?? [],
    env_access: folder.access?.env_access ?? {},
  };
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/**
 * A single role row inside a GroupList section.
 * Wraps GroupPicker but with optional inline confirm-on-remove for admins.
 */
function RoleGroupEditor({
  role,
  groups,
  onChange,
}: {
  role: RoleSection;
  groups: string[];
  onChange: (groups: string[]) => void;
}) {
  const [pendingRemove, setPendingRemove] = useState<string | null>(null);

  const handleChange = useCallback(
    (newGroups: string[]) => {
      // If a group was removed and this role requires confirmation, intercept
      if (role.requireConfirm && newGroups.length < groups.length) {
        const removed = groups.find((g) => !newGroups.includes(g));
        if (removed) {
          setPendingRemove(removed);
          return; // don't propagate yet
        }
      }
      onChange(newGroups);
    },
    [role.requireConfirm, groups, onChange]
  );

  const confirmRemove = () => {
    if (pendingRemove) {
      onChange(groups.filter((g) => g !== pendingRemove));
      setPendingRemove(null);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <role.icon className={clsx('w-3.5 h-3.5', role.color)} />
        <span className="text-sm font-medium text-surface-200">{role.label}</span>
        <span className="text-xs text-surface-500">{role.description}</span>
      </div>

      <GroupPicker groups={groups} onChange={handleChange} placeholder={`Add ${role.label.toLowerCase()} group…`} />

      {/* Inline confirm dialog for admin removal */}
      {pendingRemove && (
        <div className="flex items-center gap-3 px-3 py-2 bg-red-900/20 border border-red-800/40 rounded-lg text-sm">
          <Shield className="w-4 h-4 text-red-400 flex-shrink-0" />
          <span className="flex-1 text-surface-300">
            Remove admin group <strong className="text-surface-100">{pendingRemove}</strong>?
          </span>
          <button
            type="button"
            onClick={confirmRemove}
            className="px-2.5 py-1 bg-red-600 hover:bg-red-500 text-white text-xs rounded transition-colors"
          >
            Remove
          </button>
          <button
            type="button"
            onClick={() => setPendingRemove(null)}
            className="px-2.5 py-1 text-surface-400 hover:text-surface-200 text-xs rounded transition-colors"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Three-role editor block (Admins / Members / Viewers) for a given scope.
 */
function GroupList({
  access,
  onChange,
}: {
  access: EnvAccess;
  onChange: (access: EnvAccess) => void;
}) {
  return (
    <div className="space-y-4">
      {ROLE_SECTIONS.map((role) => (
        <RoleGroupEditor
          key={role.key}
          role={role}
          groups={access[role.key]}
          onChange={(groups) => onChange({ ...access, [role.key]: groups })}
        />
      ))}
    </div>
  );
}

/** Collapsible per-environment section */
function EnvAccessSection({
  envName,
  access,
  onChange,
}: {
  envName: string;
  access: EnvAccess;
  onChange: (access: EnvAccess) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasGroups =
    access.admins.length > 0 || access.members.length > 0 || access.viewers.length > 0;

  return (
    <div className="border border-surface-700 rounded-xl overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 bg-surface-800/50 hover:bg-surface-800 transition-colors text-left"
      >
        <div className="flex items-center gap-3">
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-surface-400" />
          ) : (
            <ChevronRight className="w-4 h-4 text-surface-400" />
          )}
          <span className="text-sm font-medium text-surface-200 font-mono">{envName}</span>
          {hasGroups && (
            <span className="text-xs px-1.5 py-0.5 bg-primary-600/20 text-primary-400 rounded-full">
              {access.admins.length + access.members.length + access.viewers.length} group
              {access.admins.length + access.members.length + access.viewers.length !== 1
                ? 's'
                : ''}
            </span>
          )}
        </div>
        {!hasGroups && !expanded && (
          <span className="text-xs text-surface-500">No env-specific access</span>
        )}
      </button>

      {expanded && (
        <div className="p-4 border-t border-surface-700 bg-surface-900/30">
          <p className="text-xs text-surface-500 mb-4 flex items-start gap-1.5">
            <Info className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
            Groups listed here get <em>additional</em> access for the{' '}
            <strong className="font-mono text-surface-400">{envName}</strong> environment only
            (union with folder-wide access).
          </p>
          <GroupList access={access} onChange={onChange} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface FolderAccessTabProps {
  folder: Folder;
}

export function FolderAccessTab({ folder }: FolderAccessTabProps) {
  const [localAccess, setLocalAccess] = useState<AccessBlock>(() => initAccess(folder));
  const patchAccess = usePatchFolderAccess(folder.name);

  // Check if there are unsaved changes
  const original = JSON.stringify(initAccess(folder));
  const current = JSON.stringify(localAccess);
  const isDirty = original !== current;

  const handleFolderLevelChange = (access: EnvAccess) => {
    setLocalAccess((prev) => ({ ...prev, ...access }));
  };

  const handleEnvAccessChange = (env: string, access: EnvAccess) => {
    setLocalAccess((prev) => ({
      ...prev,
      env_access: { ...prev.env_access, [env]: access },
    }));
  };

  const handleSave = async () => {
    try {
      await patchAccess.mutateAsync(localAccess);
      notify.success('Access saved', 'Folder access groups updated successfully.');
    } catch (err) {
      notify.error(
        'Failed to save access',
        err instanceof Error ? err.message : 'Unknown error'
      );
    }
  };

  const handleReset = () => {
    setLocalAccess(initAccess(folder));
  };

  const folderWide: EnvAccess = {
    admins: localAccess.admins,
    members: localAccess.members,
    viewers: localAccess.viewers,
  };

  const isLegacy = folder.access === null;

  return (
    <div className="space-y-8">
      {/* Legacy folder banner */}
      {isLegacy && (
        <div className="flex items-start gap-3 px-4 py-3 bg-amber-900/15 border border-amber-700/40 rounded-xl text-sm">
          <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-amber-300 font-medium">Legacy folder — no access block configured</p>
            <p className="text-amber-400/70 mt-0.5">
              Only global admins can act on this folder. Add groups below and save to enable
              granular access control.
            </p>
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Section 1: Folder-wide access                                        */}
      {/* ------------------------------------------------------------------ */}
      <section>
        <div className="mb-4">
          <h3 className="text-base font-semibold text-surface-100 flex items-center gap-2">
            <Shield className="w-4 h-4 text-primary-400" />
            Folder-wide access
          </h3>
          <p className="text-sm text-surface-500 mt-1">
            Groups listed here have the specified role in{' '}
            <strong className="text-surface-400">all environments</strong> in this folder.
          </p>
        </div>

        <div className="card">
          <div className="card-body">
            <GroupList access={folderWide} onChange={handleFolderLevelChange} />
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Section 2: Per-environment access                                    */}
      {/* ------------------------------------------------------------------ */}
      <section>
        <div className="mb-4">
          <h3 className="text-base font-semibold text-surface-100 flex items-center gap-2">
            <Eye className="w-4 h-4 text-surface-400" />
            Per-environment access
          </h3>
          <p className="text-sm text-surface-500 mt-1">
            Grant additional access for specific environments. Env-specific groups are unioned
            with folder-wide groups — they cannot reduce access.
          </p>
        </div>

        {folder.environments.length === 0 ? (
          <div className="text-center py-8 text-surface-500 text-sm border border-dashed border-surface-700 rounded-xl">
            No environments in this folder yet.
          </div>
        ) : (
          <div className="space-y-2">
            {folder.environments.map((env) => (
              <EnvAccessSection
                key={env.environment}
                envName={env.environment}
                access={localAccess.env_access[env.environment] ?? { ...EMPTY_ENV_ACCESS }}
                onChange={(access) => handleEnvAccessChange(env.environment, access)}
              />
            ))}
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Save / Reset bar                                                     */}
      {/* ------------------------------------------------------------------ */}
      <div
        className={clsx(
          'flex items-center justify-between px-4 py-3 rounded-xl border transition-all',
          isDirty
            ? 'border-primary-600/50 bg-primary-900/10'
            : 'border-surface-700 bg-surface-800/30'
        )}
      >
        <span className="text-sm text-surface-400">
          {isDirty ? 'You have unsaved changes.' : 'All changes saved.'}
        </span>
        <div className="flex items-center gap-2">
          {isDirty && (
            <button
              type="button"
              onClick={handleReset}
              disabled={patchAccess.isPending}
              className="btn-secondary text-sm"
            >
              Reset
            </button>
          )}
          <button
            type="button"
            onClick={handleSave}
            disabled={!isDirty || patchAccess.isPending}
            className="btn-primary text-sm"
          >
            {patchAccess.isPending ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Saving…
              </>
            ) : (
              <>
                <Save className="w-4 h-4" />
                Save access
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
