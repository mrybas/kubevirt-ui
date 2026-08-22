/**
 * Waiting is bounded by the thing being waited on, not by a guess.
 *
 * UAT run 4, U-2: I replaced a single refetch with a ladder of 1/3/8 seconds,
 * which is a guess about how long a controller takes. The guess was wrong by
 * a factor of four — a measured migration ran past forty-five seconds — so
 * the page stopped looking just before the answer arrived and went on naming
 * the node the VM had left. The earlier migration took nine seconds, which is
 * why the ladder looked like a fix.
 *
 * V-2 is the same defect with a different clock: a disk snapshot is
 * ReadyToUse a few seconds after it is created and the list was fetched once,
 * so the row said Pending for ever. Every action on a snapshot is behind
 * `ready`, so the rollback button — which exists, and which the report
 * recorded as missing — never appeared.
 *
 * Both are now bounded by a fact the data already carries.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { pollWhile } from '../settle';
import { join } from 'path';

const HOOKS = join(__dirname, '..');
const vms = readFileSync(join(HOOKS, 'useVMs.ts'), 'utf8');

/** The refetchInterval callback of a named query, as source. */
function intervalOf(source: string, hook: string): string {
  // `useVM(` and not `useVM` — the file also exports `useVMs`, and a prefix
  // match read the wrong function.
  const start = source.indexOf(`export function ${hook}(`);
  const body = source.slice(start, source.indexOf('\nexport function', start + 1));
  const at = body.indexOf('refetchInterval');
  expect(at, `${hook} has no refetchInterval`).toBeGreaterThan(-1);
  return body.slice(at, at + 1200);
}

describe('a migrating VM', () => {
  it('is waited on until the migration says it is done', () => {
    const interval = intervalOf(vms, 'useVM');
    expect(interval).toContain('vm.migration_phase');
  });

  it('is not covered by the transitional phases alone', () => {
    // The VMI stays Running the whole way across, which is why the existing
    // poll never engaged for the one case that changes without being touched.
    expect(vms).toMatch(/TRANSITIONAL_VALUES = \[[^\]]*\]/);
    const values = vms.match(/TRANSITIONAL_VALUES = \[([^\]]*)\]/)![1];
    expect(values).not.toContain("'Running'");
  });

  it('no longer waits on a hand-picked schedule', () => {
    const migrate = vms.slice(vms.indexOf('export function useMigrateVM'));
    expect(migrate).not.toMatch(/\[1000, 3000, 8000\]/);
    expect(migrate).toMatch(/settle\(queryClient,[\s\S]*?\], \[\]\)/);
  });
});

describe('a snapshot that is not usable yet', () => {
  it('is polled until it is, and then not', () => {
    // The rule itself, run rather than read: the source-grep version of this
    // passed while a mutant turned the poll off.
    const rule = pollWhile<{ ready?: boolean }[]>(
      (snaps) => (snaps ?? []).some((s) => !s.ready));

    expect(rule({ state: { data: [{ ready: false }] } })).toBeGreaterThan(0);
    expect(rule({ state: { data: [{ ready: true }, { ready: false }] } }))
      .toBeGreaterThan(0);
    expect(rule({ state: { data: [{ ready: true }] } })).toBe(false);
    expect(rule({ state: { data: [] } })).toBe(false);
    // Nothing fetched yet is not something to poll about.
    expect(rule({ state: { data: undefined } })).toBe(false);
  });

  it('is what the hook uses', () => {
    expect(intervalOf(vms, 'useDiskSnapshots')).toContain('pollWhile');
    expect(intervalOf(vms, 'useDiskSnapshots')).toContain('!snap.ready');
  });
});
