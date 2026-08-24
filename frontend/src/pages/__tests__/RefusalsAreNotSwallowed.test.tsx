/**
 * A refusal reaches the person who asked.
 *
 * UAT run 4, Q-3: the backend answers a disk that will not fit with "400Gi of
 * storage and 110Gi already used, so 290Gi is free — asks for 350Gi", which is
 * exactly the sentence somebody needs, and the dialog showed nothing. It
 * stayed open, empty, as if it had not heard. The silence I had just removed
 * from the backend had moved up one layer.
 *
 * Two things here. The dialog that was reported shows the message where the
 * person is looking. And the client grew a net under every other write —
 * forty-five submit handlers await a mutation without catching it, and
 * rewriting all of them identically would be worse than one place that
 * reports what nobody else claimed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MutationCache } from '@tanstack/react-query';
import { readFileSync } from 'fs';
import { join } from 'path';

import { createQueryClient } from '../../queryClient';

const errors: string[] = [];
vi.mock('../../store/notifications', () => ({
  notify: {
    error: (m: string) => errors.push(m),
    success: vi.fn(),
  },
}));

beforeEach(() => { errors.length = 0; });

function fire(client: ReturnType<typeof createQueryClient>, opts: any) {
  const cache = client.getMutationCache() as MutationCache;
  const mutation = cache.build(client, opts);
  return mutation.execute(undefined).catch(() => {});
}

describe('the net under every write', () => {
  it('reports a refusal nobody else claimed', async () => {
    const client = createQueryClient();
    await fire(client, {
      mutationFn: async () => { throw new Error('exceeded quota: 290Gi is free'); },
      retry: false,
    });
    expect(errors).toEqual(['exceeded quota: 290Gi is free']);
  });

  it('keeps quiet when the caller handles it itself', async () => {
    // Usually inline, next to the field that is wrong — better than a toast,
    // and two reports of one failure is noise.
    const client = createQueryClient();
    await fire(client, {
      mutationFn: async () => { throw new Error('handled elsewhere'); },
      onError: () => {},
      retry: false,
    });
    expect(errors).toEqual([]);
  });

  it('keeps quiet for a call site that shows the refusal itself', async () => {
    // The Storage dialogs render it inline, in the form that was refused.
    // Reporting the same 409 as a toast as well is the noise this net was
    // meant to replace.
    const client = createQueryClient();
    await fire(client, {
      mutationFn: async () => { throw new Error('shown in the dialog'); },
      meta: { handledLocally: true },
      retry: false,
    });
    expect(errors).toEqual([]);
  });

  it('says something even when the failure carries no message', async () => {
    const client = createQueryClient();
    await fire(client, {
      mutationFn: async () => { throw new Error(''); },
      retry: false,
    });
    expect(errors).toEqual(['The request was refused']);
  });

  it('stays out of the way of a write that works', async () => {
    const client = createQueryClient();
    await fire(client, { mutationFn: async () => 'fine', retry: false });
    expect(errors).toEqual([]);
  });
});

describe('the dialog that was reported', () => {
  const page = readFileSync(join(__dirname, '..', 'Storage.tsx'), 'utf8');

  it('claims the refusal, so it is not also toasted', () => {
    const hooks = readFileSync(
      join(__dirname, '..', '..', 'hooks', 'useTemplates.ts'), 'utf8');
    expect(hooks).toMatch(/meta: \{ handledLocally: true \}/);
  });

  it('keeps itself open and shows the reason', () => {
    // Both dialogs: importing an image and creating a disk.
    expect(page.match(/setSubmitError\(e instanceof Error/g)?.length).toBe(2);
    expect(page.match(/\{submitError && \(/g)?.length).toBe(2);
  });

  it('does not close on a refusal', () => {
    // The close calls live in the page-level handler, after the await, so a
    // rejection never reaches them.
    const handler = page.slice(page.indexOf('const handleCreate = async'),
                               page.indexOf('const handleDelete'));
    expect(handler.indexOf('mutateAsync')).toBeLessThan(
      handler.indexOf('setShowImportImageModal(false)'));
  });
});
