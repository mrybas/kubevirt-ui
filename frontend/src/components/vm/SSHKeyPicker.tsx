/**
 * SSHKeyPicker — pick from existing profile keys or add a new one inline.
 */

import { useState } from 'react';
import { Key, Plus, AlertTriangle } from 'lucide-react';
import { useProfile } from '../../hooks/useProfile';

const SSH_KEY_PREFIXES = ['ssh-rsa', 'ssh-ed25519', 'ssh-dss', 'ecdsa-sha2-', 'sk-ssh-ed25519', 'sk-ecdsa-sha2-'];

function getKeyLabel(key: string): string {
  const parts = key.trim().split(/\s+/);
  const type = parts[0]?.replace('ssh-', '').toUpperCase() || 'KEY';
  const comment = parts.length >= 3 ? parts.slice(2).join(' ') : '';
  return comment ? `${type}: ${comment}` : type;
}

function isValidSSHKey(key: string): boolean {
  const trimmed = key.trim();
  if (!trimmed) return true;
  return SSH_KEY_PREFIXES.some((p) => trimmed.startsWith(p));
}

interface Props {
  value: string;
  onChange: (key: string) => void;
}

export function SSHKeyPicker({ value, onChange }: Props) {
  const { data: profile, isLoading } = useProfile();
  const [showInline, setShowInline] = useState(false);
  const [newKey, setNewKey] = useState('');
  const [error, setError] = useState('');

  const profileKeys = profile?.ssh_public_keys ?? [];

  const handleSelect = (key: string) => {
    onChange(value === key ? '' : key);
  };

  const handleAddInline = () => {
    const trimmed = newKey.trim();
    if (!trimmed) return;
    if (!isValidSSHKey(trimmed)) {
      setError('Invalid SSH key format');
      return;
    }
    onChange(trimmed);
    setNewKey('');
    setShowInline(false);
    setError('');
  };

  return (
    <div>
      <label className="block text-sm font-medium text-surface-300 mb-1">
        <Key className="w-4 h-4 inline mr-1" />
        SSH Public Key
      </label>

      {/* Profile keys */}
      {isLoading ? (
        <div className="text-xs text-surface-500 py-2">Loading keys...</div>
      ) : profileKeys.length > 0 ? (
        <div className="space-y-1.5 mb-2">
          {profileKeys.map((key, idx) => {
            const selected = value === key;
            return (
              <button
                key={idx}
                type="button"
                onClick={() => handleSelect(key)}
                className={`w-full text-left px-3 py-2 rounded-lg border text-sm transition-colors ${
                  selected
                    ? 'border-primary-500 bg-primary-500/10 text-primary-300'
                    : 'border-surface-700 bg-surface-800 text-surface-300 hover:border-surface-600'
                }`}
              >
                <div className="flex items-center gap-2">
                  <div className={`w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 ${
                    selected ? 'border-primary-500' : 'border-surface-600'
                  }`}>
                    {selected && <div className="w-2 h-2 rounded-full bg-primary-500" />}
                  </div>
                  <span className="truncate">{getKeyLabel(key)}</span>
                </div>
                <p className="text-xs font-mono text-surface-500 truncate mt-0.5 ml-6">
                  {key.slice(0, 60)}...
                </p>
              </button>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-surface-500 mb-2">
          No SSH keys in your profile. Add one below or configure them in Profile settings.
        </p>
      )}

      {/* Add inline or paste directly */}
      {showInline ? (
        <div className="space-y-2">
          <textarea
            value={newKey}
            onChange={(e) => { setNewKey(e.target.value); setError(''); }}
            placeholder="ssh-ed25519 AAAA... user@host"
            rows={3}
            className={`w-full px-3 py-2 bg-surface-800 border rounded-md text-surface-100 placeholder-surface-500 focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono text-xs resize-none ${
              error ? 'border-red-500' : 'border-surface-700'
            }`}
          />
          {error && (
            <div className="flex items-center gap-1 text-xs text-red-400">
              <AlertTriangle className="w-3 h-3" />
              {error}
            </div>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleAddInline}
              disabled={!newKey.trim()}
              className="btn-primary text-xs px-3 py-1.5"
            >
              Use this key
            </button>
            <button
              type="button"
              onClick={() => { setShowInline(false); setNewKey(''); setError(''); }}
              className="btn-secondary text-xs px-3 py-1.5"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setShowInline(true)}
          className="flex items-center gap-1.5 text-xs text-primary-400 hover:text-primary-300 transition-colors"
        >
          <Plus className="w-3.5 h-3.5" />
          Paste a new key
        </button>
      )}

      {!value && (
        <p className="text-xs text-surface-500 mt-1">
          Optional: select an SSH key for passwordless login
        </p>
      )}
    </div>
  );
}
