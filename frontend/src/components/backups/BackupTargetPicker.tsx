/**
 * BackupTargetPicker — shared folder → environment → VM selector for the
 * Velero backup & schedule wizards.
 *
 * Mirrors the rest of the app's navigation: pick a folder, then one of its
 * environments (a namespace), then either back up every VM in that env or
 * cherry-pick individual VMs. The selection maps to the backend's
 * folder/environment/all_vms/vm_names fields, which resolve to a Velero
 * spec (includedNamespaces + per-VM orLabelSelectors on kubevirt-ui.io/vm-name).
 */

import { Loader2, Server } from 'lucide-react';
import clsx from 'clsx';

import { CustomSelect } from '../common/CustomSelect';
import { useFoldersFlat } from '../../hooks/useFolders';
import { useVMs } from '../../hooks/useVMs';

export interface BackupTarget {
  folder: string;
  environment: string;
  all_vms: boolean;
  vm_names: string[];
}

export const EMPTY_TARGET: BackupTarget = {
  folder: '',
  environment: '',
  all_vms: true,
  vm_names: [],
};

/** A target is usable once a folder+env are chosen and, when not all-VMs, ≥1 VM is picked. */
export function isTargetValid(t: BackupTarget): boolean {
  if (!t.folder || !t.environment) return false;
  if (!t.all_vms && t.vm_names.length === 0) return false;
  return true;
}

export function BackupTargetPicker({
  value,
  onChange,
}: {
  value: BackupTarget;
  onChange: (t: BackupTarget) => void;
}) {
  const { data: foldersResp, isLoading: foldersLoading } = useFoldersFlat();
  const folders = foldersResp?.items ?? [];

  const selectedFolder = folders.find((f) => f.name === value.folder);
  const envs = selectedFolder?.environments ?? [];
  const selectedEnv = envs.find((e) => e.environment === value.environment);

  // Resolve the namespace ({folder}-{environment}) to list its VMs.
  const namespace = selectedEnv?.name ?? '';
  const { data: vmsResp, isLoading: vmsLoading } = useVMs(namespace || undefined);
  const vms = namespace ? (vmsResp?.items ?? []) : [];

  const set = (patch: Partial<BackupTarget>) => onChange({ ...value, ...patch });

  const toggleVM = (name: string) => {
    const has = value.vm_names.includes(name);
    set({
      vm_names: has
        ? value.vm_names.filter((n) => n !== name)
        : [...value.vm_names, name],
    });
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-surface-300 mb-1">Folder *</label>
          <CustomSelect
            value={value.folder}
            onChange={(folder) =>
              // Reset downstream selections when the folder changes.
              set({ folder, environment: '', vm_names: [] })
            }
            disabled={foldersLoading}
            placeholder={foldersLoading ? 'Loading…' : 'Select folder'}
            options={folders.map((f) => ({ value: f.name, label: f.display_name || f.name }))}
          />
        </div>
        <div>
          <label className="block text-xs text-surface-300 mb-1">Environment *</label>
          <CustomSelect
            value={value.environment}
            onChange={(environment) => set({ environment, vm_names: [] })}
            disabled={!selectedFolder}
            placeholder={selectedFolder ? 'Select environment' : 'Pick a folder first'}
            options={envs.map((e) => ({
              value: e.environment,
              label: `${e.environment} (${e.vm_count} VMs)`,
            }))}
          />
        </div>
      </div>

      {value.folder && value.environment && (
        <>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => set({ all_vms: true, vm_names: [] })}
              className={clsx(
                'flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors',
                value.all_vms
                  ? 'bg-primary-500/20 border-primary-500/50 text-primary-300'
                  : 'bg-surface-800 border-surface-700 text-surface-400 hover:border-surface-600',
              )}
            >
              All VMs in environment
            </button>
            <button
              type="button"
              onClick={() => set({ all_vms: false })}
              className={clsx(
                'flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors',
                !value.all_vms
                  ? 'bg-primary-500/20 border-primary-500/50 text-primary-300'
                  : 'bg-surface-800 border-surface-700 text-surface-400 hover:border-surface-600',
              )}
            >
              Select VMs
            </button>
          </div>

          {!value.all_vms && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="block text-xs text-surface-300">
                  Virtual Machines{' '}
                  {value.vm_names.length > 0 && (
                    <span className="text-primary-400">({value.vm_names.length} selected)</span>
                  )}
                </label>
                {vms.length > 0 && (
                  <button
                    type="button"
                    onClick={() =>
                      set({
                        vm_names:
                          value.vm_names.length === vms.length ? [] : vms.map((v) => v.name),
                      })
                    }
                    className="text-xs text-primary-400 hover:text-primary-300"
                  >
                    {value.vm_names.length === vms.length ? 'Clear all' : 'Select all'}
                  </button>
                )}
              </div>
              <div className="max-h-56 overflow-y-auto rounded-lg border border-surface-700 bg-surface-900 divide-y divide-surface-800">
                {vmsLoading ? (
                  <div className="flex items-center justify-center py-6 text-surface-500">
                    <Loader2 className="w-4 h-4 animate-spin" />
                  </div>
                ) : vms.length === 0 ? (
                  <div className="py-6 text-center text-sm text-surface-500">
                    No VMs in this environment.
                  </div>
                ) : (
                  vms.map((vm) => {
                    const checked = value.vm_names.includes(vm.name);
                    return (
                      <label
                        key={vm.name}
                        className="flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-surface-800/60"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleVM(vm.name)}
                          className="w-4 h-4 rounded border-surface-600"
                        />
                        <Server className="w-3.5 h-3.5 text-surface-500 shrink-0" />
                        <span className="text-sm text-surface-200 truncate">
                          {vm.display_name || vm.name}
                        </span>
                        <span className="ml-auto text-xs text-surface-500">{vm.status}</span>
                      </label>
                    );
                  })
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
