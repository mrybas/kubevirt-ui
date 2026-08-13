/**
 * Creating an environment: quota fields, headroom, and taking room back.
 *
 * The backend has accepted `quota_cpu/memory/storage` on
 * `POST /folders/{name}/environments` all along and turns them into a real
 * ResourceQuota — the only enforcement that also binds kubectl — while the
 * form sent the name alone. And once a folder is fully allocated there was no
 * way to free room at all: environments could be created and deleted, never
 * re-sized.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const SRC = readFileSync(join(__dirname, '..', 'FolderDetail.tsx'), 'utf8');
const MODAL = SRC.slice(
  SRC.indexOf('function AddEnvironmentModal'),
  SRC.indexOf('function RoleBadge'),
);
const TAB = SRC.slice(
  SRC.indexOf('function EnvironmentsTab'),
  SRC.indexOf('function AddEnvironmentModal'),
);

describe('the environment form is a dialog', () => {
  it('the tab only opens it', () => {
    // The row had to carry a name, three quota fields, the headroom and a
    // slider per sibling; that is a form, not a row.
    expect(TAB).toContain('AddEnvironmentModal');
    expect(TAB).not.toContain('aria-label="Environment CPU quota"');
  });

  it('closing it drops the typed values with the component', () => {
    expect(TAB).toMatch(/\{showAdd && \(/);
  });
});

describe('quota fields', () => {
  it('offers all three', () => {
    for (const label of [
      'Environment CPU quota', 'Environment memory quota', 'Environment storage quota',
    ]) {
      expect(MODAL).toContain(label);
    }
  });

  it('sends them with the request', () => {
    expect(MODAL).toMatch(/quota_cpu: cpu \|\| undefined/);
    expect(MODAL).toMatch(/quota_memory: memory \|\| undefined/);
    expect(MODAL).toMatch(/quota_storage: storage \|\| undefined/);
  });

  it('shows what the folder ceiling has left', () => {
    expect(MODAL).toContain('useFolderQuotaHeadroom');
    expect(MODAL).toContain('Folder ceiling');
  });

  it('says the quota is enforced by Kubernetes, not by the page', () => {
    expect(MODAL).toMatch(/applies to kubectl as well/);
  });
});

describe('taking room from a sibling', () => {
  it('offers a slider per environment that holds CPU', () => {
    expect(MODAL).toMatch(/type="range"/);
    expect(MODAL).toMatch(/Take CPU from \$\{d\.name\}/);
    // Only environments with something to give.
    expect(MODAL).toMatch(/\.filter\(d => d\.cpu > 0\)/);
  });

  it('a slider cannot take more than the sibling holds', () => {
    expect(MODAL).toMatch(/min=\{0\} max=\{d\.cpu\}/);
  });

  it('shows what the donor is left with', () => {
    expect(MODAL).toMatch(/leaves \{d\.cpu - \(takeFrom\[d\.name\] \?\? 0\)\}/);
  });

  it('sends the sibling its new total, not a delta', () => {
    // A delta is applied to a number read a moment ago; two admins
    // rebalancing at once would each subtract from a stale total.
    expect(MODAL).toMatch(/cpu: String\(d\.cpu - \(takeFrom\[d\.name\] \?\? 0\)\)/);
  });

  it('reallocation travels with the create, in one request', () => {
    expect(MODAL).toMatch(/\.\.\.\(reallocate\.length \? \{ reallocate \} : \{\}\)/);
  });

  it('refuses to submit while short', () => {
    expect(MODAL).toMatch(/shortfall === 0/);
  });

  it('says so when no sibling can help', () => {
    expect(MODAL).toMatch(/no other environment holds a/);
  });
});
