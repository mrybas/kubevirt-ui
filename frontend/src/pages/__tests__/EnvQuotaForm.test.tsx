/**
 * An environment quota must be settable where environments are created.
 *
 * The backend has accepted `quota_cpu/memory/storage` on
 * `POST /folders/{name}/environments` all along and turns them into a real
 * ResourceQuota — the only enforcement that also binds kubectl. The form sent
 * `{environment}` and nothing else, so the only way to set one was the API.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const SRC = readFileSync(join(__dirname, '..', 'FolderDetail.tsx'), 'utf8');
const TAB = SRC.slice(SRC.indexOf('function EnvironmentsTab'), SRC.indexOf('function RoleBadge'));

describe('environment creation', () => {
  it('offers all three quota fields', () => {
    for (const label of [
      'Environment CPU quota', 'Environment memory quota', 'Environment storage quota',
    ]) {
      expect(TAB).toContain(label);
    }
  });

  it('sends them with the request', () => {
    expect(TAB).toMatch(/quota_cpu: quotaCpu \|\| undefined/);
    expect(TAB).toMatch(/quota_memory: quotaMemory \|\| undefined/);
    expect(TAB).toMatch(/quota_storage: quotaStorage \|\| undefined/);
  });

  it('shows what the folder ceiling has left', () => {
    // Without it you type a number and learn from a 409 that it was too big.
    expect(TAB).toContain('useFolderQuotaHeadroom');
    expect(TAB).toContain('Folder ceiling');
    expect(TAB).toMatch(/headroom\.free\.cpu/);
    expect(TAB).toMatch(/headroom\.free\.memory/);
  });

  it('clears the fields after adding, so the next one does not inherit them', () => {
    expect(TAB).toMatch(/setQuotaCpu\(''\); setQuotaMemory\(''\); setQuotaStorage\(''\);/);
  });

  it('says the quota is enforced by Kubernetes, not by the page', () => {
    expect(TAB).toMatch(/applies to kubectl as well/);
  });
});
