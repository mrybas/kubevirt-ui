/**
 * Unit tests for tenant wizard canNext logic.
 * Tests the pure validation rules extracted from the wizard.
 */
import { describe, it, expect } from 'vitest';

// Mirror of WizardState from Tenants.tsx
interface WizardState {
  name: string;
  display_name: string;
  worker_type: 'vm' | 'bare_metal';
  worker_count: number;
}

// Mirror of canNext logic from CreateTenantWizard
function canNext(step: number, form: WizardState): boolean {
  if (step === 0) return form.name.length > 0 && form.display_name.length > 0;
  if (step === 1) {
    if (form.worker_count <= 0) return false;
    return true;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Step 0: Basics
// ---------------------------------------------------------------------------

describe('canNext - step 0 (Basics)', () => {
  it('requires both name and display_name', () => {
    expect(canNext(0, { name: '', display_name: '', worker_type: 'vm', worker_count: 2 })).toBe(false);
    expect(canNext(0, { name: 'foo', display_name: '', worker_type: 'vm', worker_count: 2 })).toBe(false);
    expect(canNext(0, { name: '', display_name: 'Foo', worker_type: 'vm', worker_count: 2 })).toBe(false);
    expect(canNext(0, { name: 'foo', display_name: 'Foo', worker_type: 'vm', worker_count: 2 })).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Step 1: Workers
// ---------------------------------------------------------------------------

describe('canNext - step 1 (Workers) with VM type', () => {
  it('returns false when worker_count is 0', () => {
    const form = { name: 'x', display_name: 'X', worker_type: 'vm' as const, worker_count: 0 };
    expect(canNext(1, form)).toBe(false);
  });

  it('returns true when VM type has valid worker count', () => {
    const form = { name: 'x', display_name: 'X', worker_type: 'vm' as const, worker_count: 2 };
    expect(canNext(1, form)).toBe(true);
  });
});

describe('canNext - step 1 (Workers) with bare_metal type', () => {
  it('returns true for bare_metal with valid count', () => {
    const form = { name: 'x', display_name: 'X', worker_type: 'bare_metal' as const, worker_count: 3 };
    expect(canNext(1, form)).toBe(true);
  });

  it('returns false when worker_count is 0 even for bare_metal', () => {
    const form = { name: 'x', display_name: 'X', worker_type: 'bare_metal' as const, worker_count: 0 };
    expect(canNext(1, form)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Steps 2+: always true
// ---------------------------------------------------------------------------

describe('canNext - steps 2 and 3', () => {
  const form = { name: 'x', display_name: 'X', worker_type: 'vm' as const, worker_count: 0 };

  it('returns true for step 2 (Addons)', () => {
    expect(canNext(2, form)).toBe(true);
  });

  it('returns true for step 3 (Network)', () => {
    expect(canNext(3, form)).toBe(true);
  });
});
