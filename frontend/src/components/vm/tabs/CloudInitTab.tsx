import { useEffect, useState } from 'react';
import { AlertCircle, Eye, EyeOff, FileText, Lock, Plus, RefreshCw, Trash2, UserPlus } from 'lucide-react';
import clsx from 'clsx';
import {
  useVMCloudInit,
  useUpdateVMCloudInit,
  useVMCloudInitForm,
  useUpdateVMCloudInitForm,
} from '@/hooks/useVMs';
import { notify } from '@/store/notifications';
import type { VMCloudInitFormUser, VMCloudInitSudoMode } from '@/api/vms';

interface ApiErrorLike extends Error {
  status?: number;
}

type SubTab = 'form' | 'yaml';

export function CloudInitTab({ namespace, name }: { namespace: string; name: string }) {
  const ci = useVMCloudInit(namespace, name);
  const form = useVMCloudInitForm(namespace, name);

  const [subTab, setSubTab] = useState<SubTab>('form');

  // 404 / no cloud-init volume
  const ciStatus = (ci.error as ApiErrorLike | undefined)?.status;
  if (ciStatus === 404) {
    return (
      <div className="card">
        <div className="card-body flex flex-col items-center justify-center py-12 text-surface-400">
          <FileText className="w-10 h-10 mb-3 opacity-50" />
          <p className="text-sm mb-1">No cloud-init volume on this VM</p>
          <p className="text-xs text-surface-500 text-center max-w-md">
            Cloud-init userData can be edited only for VMs created with an inline
            <code className="mx-1">cloudInitNoCloud</code>
            or
            <code className="mx-1">cloudInitConfigDrive</code>
            volume.
          </p>
        </div>
      </div>
    );
  }

  if (ci.isLoading || form.isLoading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (ci.error) {
    return (
      <div className="card">
        <div className="card-body flex flex-col items-center justify-center py-12 text-surface-400">
          <AlertCircle className="w-10 h-10 mb-3 text-red-400 opacity-70" />
          <p className="text-sm">Failed to load cloud-init</p>
          <p className="text-xs text-red-400 mt-1">{(ci.error as Error).message}</p>
        </div>
      </div>
    );
  }

  if (!ci.data) return null;
  const ciData = ci.data;

  if (!ciData.editable) {
    return (
      <div className="card">
        <div className="card-body space-y-2">
          <div className="flex items-center gap-2 text-surface-100">
            <Lock className="w-4 h-4" />
            <h3 className="font-medium">Cloud-init not editable</h3>
          </div>
          <p className="text-sm text-surface-400">{ciData.note}</p>
          <p className="text-xs text-surface-500">Volume: <code>{ciData.volume_name}</code> · Source: {ciData.source}</p>
        </div>
      </div>
    );
  }

  const formCompatible = form.data?.form_compatible ?? false;
  const effectiveSubTab: SubTab = formCompatible ? subTab : 'yaml';

  return (
    <div className="space-y-3">
      {/* Header card with sub-tabs */}
      <div className="card">
        <div className="card-body space-y-2">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="font-medium text-surface-100">Cloud-init userData</h3>
              <p className="text-xs text-surface-500 mt-0.5">
                Volume <code className="text-surface-400">{ciData.volume_name}</code> · {ciData.note}
              </p>
            </div>
            <div className="flex items-center gap-1 rounded-lg border border-surface-700 bg-surface-800 p-0.5">
              <button
                onClick={() => setSubTab('form')}
                disabled={!formCompatible}
                title={!formCompatible ? 'Cloud-init uses advanced features — edit as YAML' : undefined}
                className={clsx(
                  'px-3 py-1 text-xs rounded-md transition-colors',
                  effectiveSubTab === 'form'
                    ? 'bg-primary-500/20 text-primary-300'
                    : 'text-surface-400 hover:text-surface-200',
                  !formCompatible && 'opacity-50 cursor-not-allowed',
                )}
              >
                Form
              </button>
              <button
                onClick={() => setSubTab('yaml')}
                className={clsx(
                  'px-3 py-1 text-xs rounded-md transition-colors',
                  effectiveSubTab === 'yaml'
                    ? 'bg-primary-500/20 text-primary-300'
                    : 'text-surface-400 hover:text-surface-200',
                )}
              >
                YAML
              </button>
            </div>
          </div>

          {!formCompatible && (form.data?.incompatible_keys?.length ?? 0) > 0 && (
            <div className="flex items-start gap-2 p-2.5 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-amber-300">
              <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
              <span>
                This cloud-init uses features outside the form's subset
                ({form.data!.incompatible_keys.join(', ')}). Edit as raw YAML.
              </span>
            </div>
          )}
        </div>
      </div>

      {effectiveSubTab === 'form' && form.data && (
        <FormEditor
          namespace={namespace}
          name={name}
          initial={form.data}
        />
      )}

      {effectiveSubTab === 'yaml' && (
        <YamlEditor
          namespace={namespace}
          name={name}
          initialUserData={ciData.user_data ?? ''}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Form editor — structured users + ssh keys + sudo
// ---------------------------------------------------------------------------

interface FormState {
  global_ssh_keys: string[];
  users: VMCloudInitFormUser[];
}

function FormEditor({
  namespace, name, initial,
}: {
  namespace: string;
  name: string;
  initial: { global_ssh_keys: string[]; users: VMCloudInitFormUser[] };
}) {
  const update = useUpdateVMCloudInitForm();
  const [state, setState] = useState<FormState>({
    global_ssh_keys: initial.global_ssh_keys,
    users: initial.users,
  });

  useEffect(() => {
    setState({ global_ssh_keys: initial.global_ssh_keys, users: initial.users });
  }, [initial.global_ssh_keys, initial.users]);

  const updateUser = (idx: number, patch: Partial<VMCloudInitFormUser>) => {
    setState(s => ({
      ...s,
      users: s.users.map((u, i) => (i === idx ? { ...u, ...patch } : u)),
    }));
  };

  const addUser = () => {
    setState(s => ({
      ...s,
      users: [
        ...s.users,
        { name: '', password: '', ssh_keys: [], sudo: 'none', has_existing_password: false },
      ],
    }));
  };

  const removeUser = (idx: number) => {
    setState(s => ({ ...s, users: s.users.filter((_, i) => i !== idx) }));
  };

  const handleSave = async () => {
    try {
      await update.mutateAsync({ namespace, name, body: state });
      notify.success('Cloud-init saved — restart VM to take effect');
    } catch (e) {
      notify.error('Failed to save', e instanceof Error ? e.message : String(e));
    }
  };

  const handleReset = () => {
    setState({ global_ssh_keys: initial.global_ssh_keys, users: initial.users });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end gap-2">
        <button onClick={handleReset} className="btn-secondary text-sm" disabled={update.isPending}>
          Reset
        </button>
        <button onClick={handleSave} className="btn-primary text-sm" disabled={update.isPending}>
          {update.isPending ? 'Saving...' : 'Save'}
        </button>
      </div>

      {/* Global SSH keys */}
      <div className="card">
        <div className="card-body space-y-2">
          <div>
            <h4 className="text-sm font-medium text-surface-100">Default user SSH keys</h4>
            <p className="text-xs text-surface-500 mt-0.5">
              Added to the image's default user (e.g. <code>ubuntu</code>, <code>cloud-user</code>).
            </p>
          </div>
          <SshKeysEditor
            keys={state.global_ssh_keys}
            onChange={(keys) => setState(s => ({ ...s, global_ssh_keys: keys }))}
          />
        </div>
      </div>

      {/* Additional users */}
      <div className="card">
        <div className="card-body space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <h4 className="text-sm font-medium text-surface-100">Additional users</h4>
              <p className="text-xs text-surface-500 mt-0.5">
                Created on first boot by cloud-init.
              </p>
            </div>
            <button onClick={addUser} className="btn-secondary text-xs">
              <UserPlus className="w-3.5 h-3.5" />
              Add user
            </button>
          </div>

          {state.users.length === 0 && (
            <p className="text-xs text-surface-500 italic">No additional users defined.</p>
          )}

          {state.users.map((u, i) => (
            <UserCard
              key={i}
              user={u}
              onChange={(patch) => updateUser(i, patch)}
              onRemove={() => removeUser(i)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// UserCard
// ---------------------------------------------------------------------------

function UserCard({
  user, onChange, onRemove,
}: {
  user: VMCloudInitFormUser;
  onChange: (patch: Partial<VMCloudInitFormUser>) => void;
  onRemove: () => void;
}) {
  const [showPw, setShowPw] = useState(false);
  const hasPassword = user.password.length > 0 || user.has_existing_password;

  // If user clears password and didn't have existing one → force sudo off "with-password"
  useEffect(() => {
    if (!hasPassword && user.sudo === 'with-password') {
      onChange({ sudo: 'none' });
    }
  }, [hasPassword, user.sudo, onChange]);

  return (
    <div className="rounded-lg border border-surface-700 bg-surface-800/40 p-3 space-y-3">
      <div className="flex items-start gap-2">
        <input
          value={user.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="username"
          className="input text-sm flex-1 font-mono"
        />
        <button
          onClick={onRemove}
          className="p-2 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-md transition-colors"
          title="Remove user"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Password */}
      <div>
        <label className="text-xs text-surface-400 mb-1 block">
          Password
          {user.has_existing_password && user.password === '' && (
            <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-surface-700 text-surface-400">
              Current password preserved
            </span>
          )}
        </label>
        <div className="relative">
          <input
            type={showPw ? 'text' : 'password'}
            value={user.password}
            onChange={(e) => onChange({ password: e.target.value })}
            placeholder={user.has_existing_password ? '●●●●●●●●' : 'Leave empty for no password'}
            className="input text-sm w-full pr-9"
          />
          <button
            onClick={() => setShowPw(!showPw)}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-surface-500 hover:text-surface-300"
            tabIndex={-1}
          >
            {showPw ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
          </button>
        </div>
      </div>

      {/* Sudo */}
      <div>
        <label className="text-xs text-surface-400 mb-1 block">Sudo</label>
        <div className="grid grid-cols-3 gap-1.5">
          {(['none', 'passwordless', 'with-password'] as VMCloudInitSudoMode[]).map(mode => {
            const disabled = mode === 'with-password' && !hasPassword;
            const checked = user.sudo === mode;
            return (
              <button
                key={mode}
                onClick={() => !disabled && onChange({ sudo: mode })}
                disabled={disabled}
                title={disabled ? 'Set a password first to allow password-based sudo' : undefined}
                className={clsx(
                  'px-2 py-1.5 text-xs rounded-md border transition-colors',
                  checked
                    ? 'border-primary-500 bg-primary-500/10 text-primary-300'
                    : 'border-surface-700 bg-surface-800 text-surface-400 hover:text-surface-200',
                  disabled && 'opacity-40 cursor-not-allowed hover:text-surface-400',
                )}
              >
                {mode === 'none' ? 'None' : mode === 'passwordless' ? 'Passwordless' : 'With password'}
              </button>
            );
          })}
        </div>
      </div>

      {/* SSH keys */}
      <div>
        <label className="text-xs text-surface-400 mb-1 block">SSH authorized keys</label>
        <SshKeysEditor
          keys={user.ssh_keys}
          onChange={(keys) => onChange({ ssh_keys: keys })}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SshKeysEditor — list of keys + add textarea
// ---------------------------------------------------------------------------

function SshKeysEditor({
  keys, onChange,
}: {
  keys: string[];
  onChange: (keys: string[]) => void;
}) {
  const [draft, setDraft] = useState('');

  const addKey = () => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    onChange([...keys, trimmed]);
    setDraft('');
  };

  const removeKey = (idx: number) => {
    onChange(keys.filter((_, i) => i !== idx));
  };

  const looksLikeKey = (k: string) =>
    /^(ssh-rsa|ssh-ed25519|ecdsa-sha2-|sk-(ssh-ed25519|ecdsa-sha2-)) /.test(k);

  return (
    <div className="space-y-2">
      {keys.length === 0 && (
        <p className="text-xs text-surface-500 italic">No SSH keys.</p>
      )}
      {keys.map((k, i) => (
        <div key={i} className="flex items-start gap-2">
          <code className={clsx(
            'flex-1 text-[10px] p-2 rounded bg-surface-900 break-all',
            looksLikeKey(k) ? 'text-surface-300' : 'text-amber-400',
          )}>
            {k}
          </code>
          <button
            onClick={() => removeKey(i)}
            className="p-1.5 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-md transition-colors"
            title="Remove key"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
      <div className="flex items-start gap-2">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Paste SSH key (ssh-rsa AAAA... / ssh-ed25519 AAAA...)"
          rows={2}
          className="input text-xs flex-1 font-mono"
        />
        <button
          onClick={addKey}
          disabled={!draft.trim()}
          className="btn-secondary text-xs whitespace-nowrap"
        >
          <Plus className="w-3.5 h-3.5" />
          Add
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// YamlEditor — raw editor (the original implementation)
// ---------------------------------------------------------------------------

function YamlEditor({
  namespace, name, initialUserData,
}: {
  namespace: string;
  name: string;
  initialUserData: string;
}) {
  const update = useUpdateVMCloudInit();
  const [draft, setDraft] = useState(initialUserData);
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    setDraft(initialUserData);
  }, [initialUserData]);

  const handleSave = async () => {
    try {
      await update.mutateAsync({ namespace, name, user_data: draft });
      notify.success('Cloud-init saved — restart VM to take effect');
      setIsEditing(false);
    } catch (e) {
      notify.error('Failed to save', e instanceof Error ? e.message : String(e));
    }
  };

  const handleCancel = () => {
    setDraft(initialUserData);
    setIsEditing(false);
  };

  return (
    <div className="card">
      <div className="card-body space-y-3">
        <div className="flex items-center justify-end gap-2">
          {!isEditing && (
            <button onClick={() => setIsEditing(true)} className="btn-secondary text-sm">
              Edit
            </button>
          )}
          {isEditing && (
            <>
              <button onClick={handleCancel} className="btn-secondary text-sm" disabled={update.isPending}>
                Cancel
              </button>
              <button onClick={handleSave} className="btn-primary text-sm" disabled={update.isPending}>
                {update.isPending ? 'Saving...' : 'Save'}
              </button>
            </>
          )}
        </div>

        {isEditing && (
          <div className="flex items-start gap-2 p-2.5 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-amber-300">
            <RefreshCw className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
            <span>
              Cloud-init runs at first boot. Changes apply to the next freshly-provisioned
              VMI; an existing running VM will not re-run cloud-init on simple restart.
            </span>
          </div>
        )}

        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          readOnly={!isEditing}
          spellCheck={false}
          placeholder="#cloud-config&#10;..."
          className={clsx(
            'w-full h-[500px] font-mono text-xs p-3 rounded-lg border resize-y',
            isEditing
              ? 'bg-surface-900 border-surface-700 text-surface-100 focus:border-primary-500 focus:outline-none'
              : 'bg-surface-950 border-surface-800 text-surface-300 cursor-default',
          )}
        />
      </div>
    </div>
  );
}
