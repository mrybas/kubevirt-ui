import { useState, useEffect } from 'react';
import {
  Key,
  Loader2,
  CheckCircle,
  AlertTriangle,
  Info,
  Plus,
  Trash2,
  Copy,
  Check,
} from 'lucide-react';
import { useProfile, useUpdateSSHKeys } from '../hooks/useProfile';
import { useAuthStore } from '../store/auth';
import { PageTitle } from '@/components/common/PageTitle';

const SSH_KEY_PREFIXES = [
  'ssh-rsa',
  'ssh-ed25519',
  'ssh-dss',
  'ecdsa-sha2-',
  'sk-ssh-ed25519',
  'sk-ecdsa-sha2-',
];

function isValidSSHKey(key: string): boolean {
  const trimmed = key.trim();
  if (!trimmed) return true;
  return SSH_KEY_PREFIXES.some((prefix) => trimmed.startsWith(prefix));
}

function getKeyType(key: string): string {
  if (key.startsWith('ssh-ed25519')) return 'ED25519';
  if (key.startsWith('ssh-rsa')) return 'RSA';
  if (key.startsWith('ecdsa-sha2-')) return 'ECDSA';
  if (key.startsWith('sk-ssh-ed25519')) return 'SK-ED25519';
  if (key.startsWith('sk-ecdsa-sha2-')) return 'SK-ECDSA';
  if (key.startsWith('ssh-dss')) return 'DSA';
  return 'Unknown';
}

function getKeyComment(key: string): string {
  const parts = key.trim().split(/\s+/);
  return parts.length >= 3 ? parts.slice(2).join(' ') : '';
}

function getKeyFingerprint(key: string): string {
  const parts = key.trim().split(/\s+/);
  if (parts.length >= 2) {
    const data = parts[1];
    return data && data.length > 12 ? `${data.slice(0, 6)}...${data.slice(-6)}` : (data || '');
  }
  return '';
}

export default function Profile() {
  const { user } = useAuthStore();
  const { data: profile, isLoading, error } = useProfile();
  const updateSSHKeys = useUpdateSSHKeys();

  const [keys, setKeys] = useState<string[]>([]);
  const [newKey, setNewKey] = useState('');
  const [newKeyError, setNewKeyError] = useState('');
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);

  // Sync keys from server
  useEffect(() => {
    if (profile?.ssh_public_keys) {
      setKeys(profile.ssh_public_keys);
    }
  }, [profile]);

  const saveKeys = async (updatedKeys: string[]) => {
    setSaveSuccess(false);
    try {
      await updateSSHKeys.mutateAsync({ ssh_public_keys: updatedKeys });
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch {
      // Error handled by mutation
    }
  };

  const handleAddKey = () => {
    const trimmed = newKey.trim();
    if (!trimmed) return;

    if (!isValidSSHKey(trimmed)) {
      setNewKeyError(
        `Invalid SSH key. Must start with: ${SSH_KEY_PREFIXES.join(', ')}`
      );
      return;
    }

    if (keys.includes(trimmed)) {
      setNewKeyError('This key is already added');
      return;
    }

    const updatedKeys = [...keys, trimmed];
    setKeys(updatedKeys);
    setNewKey('');
    setNewKeyError('');
    saveKeys(updatedKeys);
  };

  const handleRemoveKey = (index: number) => {
    const updatedKeys = keys.filter((_, i) => i !== index);
    setKeys(updatedKeys);
    saveKeys(updatedKeys);
  };

  const handleCopy = (key: string, idx: number) => {
    navigator.clipboard.writeText(key);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 2000);
  };

  const handlePaste = async () => {
    try {
      const text = await navigator.clipboard.readText();
      // Support pasting multiple keys (one per line)
      const lines = text
        .split('\n')
        .map((l) => l.trim())
        .filter((l) => l && !l.startsWith('#'));

      if (lines.length > 1) {
        // Multiple keys pasted — auto-save
        const validKeys: string[] = [];
        for (const line of lines) {
          if (isValidSSHKey(line) && !keys.includes(line)) {
            validKeys.push(line);
          }
        }
        if (validKeys.length > 0) {
          const updatedKeys = [...keys, ...validKeys];
          setKeys(updatedKeys);
          setNewKey('');
          saveKeys(updatedKeys);
        }
      } else if (lines.length === 1) {
        setNewKey(lines[0] || '');
      }
    } catch {
      // Clipboard API not available
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 text-primary-400 animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-8 max-w-3xl">
      <PageTitle title="Profile" subtitle="Manage your personal settings and SSH keys" />

      {/* User Info */}
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-surface-100 mb-4">
          Account
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-surface-400">Email</span>
            <p className="text-surface-100 font-medium mt-1">
              {user?.email || profile?.email || '—'}
            </p>
          </div>
          <div>
            <span className="text-surface-400">Username</span>
            <p className="text-surface-100 font-medium mt-1">
              {user?.username || '—'}
            </p>
          </div>
          {user?.groups && user.groups.length > 0 && (
            <div className="col-span-2">
              <span className="text-surface-400">Groups</span>
              <div className="flex flex-wrap gap-1.5 mt-1">
                {user.groups.map((g) => (
                  <span
                    key={g}
                    className="px-2 py-0.5 text-xs rounded bg-surface-800 text-surface-300"
                  >
                    {g}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* SSH Keys */}
      <div className="card p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Key className="h-5 w-5 text-primary-400" />
            <h2 className="text-lg font-semibold text-surface-100">
              SSH Public Keys
            </h2>
          </div>
          <div className="flex items-center gap-2">
            {updateSSHKeys.isPending && (
              <Loader2 className="h-4 w-4 text-primary-400 animate-spin" />
            )}
            {saveSuccess && (
              <span className="flex items-center gap-1.5 text-sm text-emerald-400">
                <CheckCircle className="h-4 w-4" />
                Saved
              </span>
            )}
          </div>
        </div>

        {/* Info banner */}
        <div className="bg-primary-500/10 border border-primary-500/20 rounded-lg p-3 mb-5">
          <div className="flex items-start gap-2">
            <Info className="h-4 w-4 text-primary-400 mt-0.5 shrink-0" />
            <p className="text-xs text-primary-300">
              Your SSH keys will be automatically injected into every VM you
              create. They are merged with any keys defined in the VM template
              — nothing gets overwritten.
            </p>
          </div>
        </div>

        {/* Add new key */}
        <div className="space-y-2 mb-5">
          <label className="block text-sm font-medium text-surface-200">
            Add SSH Public Key
          </label>
          <div className="flex gap-2">
            <textarea
              value={newKey}
              onChange={(e) => {
                setNewKey(e.target.value);
                setNewKeyError('');
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleAddKey();
                }
              }}
              className={`input flex-1 min-h-[80px] font-mono text-xs resize-none ${
                newKeyError ? 'border-red-500 focus:ring-red-500' : ''
              }`}
              placeholder="ssh-ed25519 AAAA... user@hostname"
            />
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleAddKey}
              disabled={updateSSHKeys.isPending}
              className="btn-primary text-sm flex items-center gap-2"
            >
              <Plus className="h-4 w-4" />
              Add Key
            </button>
            <button onClick={handlePaste} className="btn-secondary text-sm">
              Paste from clipboard
            </button>
          </div>
          {newKeyError && (
            <div className="flex items-center gap-1.5 text-xs text-red-400">
              <AlertTriangle className="h-3.5 w-3.5" />
              {newKeyError}
            </div>
          )}
        </div>

        {/* Existing keys */}
        <div className="space-y-2">
          {keys.length === 0 ? (
            <div className="text-center py-8 bg-surface-800/30 rounded-lg border border-dashed border-surface-700">
              <Key className="h-10 w-10 mx-auto text-surface-600 mb-2" />
              <p className="text-surface-400 text-sm mb-1">
                No SSH keys configured
              </p>
              <p className="text-surface-500 text-xs">
                Add your public key above to enable SSH access to VMs
              </p>
            </div>
          ) : (
            keys.map((key, idx) => (
              <div
                key={idx}
                className="flex items-center gap-3 p-3 bg-surface-800/50 rounded-lg border border-surface-700 group"
              >
                <Key className="h-4 w-4 text-surface-500 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="px-1.5 py-0.5 text-[10px] rounded bg-surface-700 text-surface-400 font-mono">
                      {getKeyType(key)}
                    </span>
                    <span className="text-sm text-surface-200 truncate">
                      {getKeyComment(key) || getKeyFingerprint(key)}
                    </span>
                  </div>
                  <p className="text-xs text-surface-500 font-mono truncate mt-0.5">
                    {key.slice(0, 80)}...
                  </p>
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => handleCopy(key, idx)}
                    className="p-1.5 hover:bg-surface-700 rounded text-surface-400 hover:text-surface-200"
                    title="Copy key"
                  >
                    {copiedIdx === idx ? (
                      <Check className="h-3.5 w-3.5 text-emerald-400" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </button>
                  <button
                    onClick={() => handleRemoveKey(idx)}
                    className="p-1.5 hover:bg-surface-700 rounded text-surface-400 hover:text-red-400"
                    title="Remove key"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        {/* Error */}
        {(error || updateSSHKeys.error) && (
          <div className="mt-4 bg-red-500/10 border border-red-500/30 rounded-lg p-3">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-red-400" />
              <p className="text-sm text-red-300">
                {(error as Error)?.message ||
                  (updateSSHKeys.error as Error)?.message ||
                  'Failed to load profile'}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
